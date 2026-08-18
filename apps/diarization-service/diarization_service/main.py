from __future__ import annotations

import asyncio
import gc
import secrets
import tempfile
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    service_token: str = ""
    model: str = "pyannote/speaker-diarization-3.1"
    hf_token: str = ""
    ollama_base_url: str = "http://127.0.0.1:11435"
    model_cache_dir: str = "/models/pyannote"
    max_upload_mb: int = 500
    ollama_timeout_seconds: int = 30


class SpeakerTurnResponse(BaseModel):
    start: float
    end: float
    speaker: str


class DiarizationResponse(BaseModel):
    model: str
    turns: list[SpeakerTurnResponse]


settings = Settings()
gpu_lock = asyncio.Lock()
app = FastAPI(title="Obsidian GPU coordinator", version="0.1.0")


def require_service_token(authorization: str | None) -> None:
    expected = settings.service_token.strip()
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Diarization authentication is not configured.")
    scheme, separator, supplied = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid diarization token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/v1/health")
def health() -> dict[str, object]:
    cuda_available = False
    try:
        import torch

        cuda_available = torch.cuda.is_available()
    except ImportError:
        pass
    return {
        "status": "ok" if cuda_available and bool(settings.service_token) else "degraded",
        "cuda": cuda_available,
        "gpu_busy": gpu_lock.locked(),
        "model": settings.model,
    }


@app.get("/v1/ollama-health")
def ollama_health() -> dict[str, object]:
    try:
        response = httpx.get(
            f"{settings.ollama_base_url.rstrip('/')}/api/version",
            timeout=settings.ollama_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return {"status": "degraded", "gpu_busy": gpu_lock.locked()}
    return {"status": "ok", "gpu_busy": gpu_lock.locked()}


@app.post("/v1/diarize", response_model=DiarizationResponse)
async def diarize(
    file: Annotated[UploadFile, File(...)],
    authorization: Annotated[str | None, Header()] = None,
    min_speakers: Annotated[int | None, Form(ge=1, le=20)] = None,
    max_speakers: Annotated[int | None, Form(ge=1, le=20)] = None,
) -> DiarizationResponse:
    require_service_token(authorization)
    if min_speakers and max_speakers and min_speakers > max_speakers:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="min_speakers must not exceed max_speakers.")

    suffix = Path(file.filename or "audio.wav").suffix[:10] or ".wav"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="obsidian-diarization-", suffix=suffix, delete=False) as temporary:
            temp_path = Path(temporary.name)
            total_bytes = 0
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > settings.max_upload_mb * 1024 * 1024:
                    raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Audio file is too large.")
                temporary.write(chunk)
        if total_bytes == 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Audio file is empty.")

        async with gpu_lock:
            await unload_ollama_models()
            turns = await asyncio.to_thread(run_diarization_pipeline, temp_path, min_speakers, max_speakers)
        return DiarizationResponse(model=settings.model, turns=turns)
    finally:
        await file.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@app.get("/api/version")
@app.get("/api/tags")
async def proxy_ollama_status(request: Request) -> Response:
    # These endpoints only inspect the native Ollama server and do not load a
    # model onto the GPU. Keeping them outside the GPU lock prevents a long
    # generation from looking like an Ollama outage in Gatus or Prometheus.
    return await forward_ollama_request(request)


@app.api_route("/ollama/{upstream_path:path}", methods=["GET", "POST", "DELETE"])
@app.api_route("/api/{upstream_path:path}", methods=["GET", "POST", "DELETE"])
@app.api_route("/v1/{upstream_path:path}", methods=["GET", "POST", "DELETE"])
async def proxy_ollama(upstream_path: str, request: Request) -> Response:
    del upstream_path
    # All Ollama traffic goes through the same lock. A diarization therefore
    # cannot race a model reload on the 8 GB GPU.
    async with gpu_lock:
        return await forward_ollama_request(request)


async def forward_ollama_request(request: Request) -> Response:
    body = await request.body()
    headers = {"Content-Type": request.headers.get("content-type", "application/json")}
    async with httpx.AsyncClient(
        base_url=settings.ollama_base_url.rstrip("/"),
        timeout=None,
    ) as client:
        upstream_path = request.url.path
        if upstream_path.startswith("/ollama/"):
            upstream_path = upstream_path.removeprefix("/ollama")
        upstream = await client.request(
            request.method,
            upstream_path,
            params=request.query_params,
            content=body,
            headers=headers,
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


async def unload_ollama_models() -> None:
    try:
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url.rstrip("/"),
            timeout=settings.ollama_timeout_seconds,
        ) as client:
            running = await client.get("/api/ps")
            running.raise_for_status()
            payload = running.json()
            models = payload.get("models", []) if isinstance(payload, dict) else []
            for model in models:
                name = model.get("name") if isinstance(model, dict) else None
                if not isinstance(name, str) or not name:
                    continue
                response = await client.post(
                    "/api/generate",
                    json={"model": name, "prompt": "", "stream": False, "keep_alive": 0},
                )
                response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ollama could not be unloaded safely before diarization.",
        ) from exc


def run_diarization_pipeline(audio_path: Path, min_speakers: int | None, max_speakers: int | None) -> list[SpeakerTurnResponse]:
    import os

    os.environ.setdefault("HF_HOME", settings.model_cache_dir)
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(Path(settings.model_cache_dir) / "hub"))
    import torch
    from pyannote.audio import Pipeline

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available for diarization.")
    pipeline: Any | None = None
    try:
        pipeline = Pipeline.from_pretrained(settings.model, use_auth_token=settings.hf_token or None)
        pipeline.to(torch.device("cuda"))
        options = {key: value for key, value in {"min_speakers": min_speakers, "max_speakers": max_speakers}.items() if value is not None}
        output = pipeline(str(audio_path), **options)
        return normalize_speaker_turns(output)
    finally:
        del pipeline
        gc.collect()
        torch.cuda.empty_cache()


def normalize_speaker_turns(output: Any) -> list[SpeakerTurnResponse]:
    annotation = getattr(output, "exclusive_speaker_diarization", None)
    if annotation is None:
        annotation = getattr(output, "speaker_diarization", None)
    if annotation is None:
        annotation = output

    raw_turns: list[tuple[float, float, str]] = []
    if hasattr(annotation, "itertracks"):
        for turn, _track, speaker in annotation.itertracks(yield_label=True):
            raw_turns.append((float(turn.start), float(turn.end), str(speaker)))
    else:
        for turn, speaker in annotation:
            raw_turns.append((float(turn.start), float(turn.end), str(speaker)))
    raw_turns.sort(key=lambda item: (item[0], item[1]))

    speaker_names: dict[str, str] = {}
    normalized: list[SpeakerTurnResponse] = []
    for start, end, raw_speaker in raw_turns:
        if end <= start:
            continue
        speaker_names.setdefault(raw_speaker, f"Speaker {len(speaker_names) + 1}")
        normalized.append(SpeakerTurnResponse(start=start, end=end, speaker=speaker_names[raw_speaker]))
    if not normalized:
        raise RuntimeError("Diarization produced no speaker turns.")
    return normalized
