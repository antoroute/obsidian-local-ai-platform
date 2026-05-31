from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from whisper_worker.config import WorkerSettings
from whisper_worker.diarization import apply_diarization_to_segments, create_diarization_engine
from whisper_worker.diarization import DiarizationEngine
from whisper_worker.repositories import TranscriptResult, TranscriptSegment

logger = logging.getLogger(__name__)


class TranscriptionEngine:
    def transcribe(self, input_path: Path, *, transcription_language: str | None = None) -> TranscriptResult:
        raise NotImplementedError


class FakeTranscriptionEngine(TranscriptionEngine):
    def transcribe(self, input_path: Path, *, transcription_language: str | None = None) -> TranscriptResult:
        del input_path
        language = _resolve_fake_language(transcription_language)
        return TranscriptResult(
            text="Fake transcript for testing.",
            language=language,
            duration=0,
            segments=[TranscriptSegment(start=0, end=1, text="Fake transcript for testing.")],
        )


class DiarizingTranscriptionEngine(TranscriptionEngine):
    def __init__(self, base_engine: TranscriptionEngine, settings: WorkerSettings) -> None:
        self._base_engine = base_engine
        self._settings = settings
        self._diarization_engine: DiarizationEngine | None = None
        self._diarization_engine_loaded = False

    def transcribe(self, input_path: Path, *, transcription_language: str | None = None) -> TranscriptResult:
        transcript = self._base_engine.transcribe(input_path, transcription_language=transcription_language)
        try:
            diarization_engine = self._get_diarization_engine()
            if diarization_engine is None:
                return transcript
            logger.info("Running diarization for audio duration %.2fs", transcript.duration)
            turns = diarization_engine.diarize(input_path)
            logger.info("Diarization completed with %s speaker turns", len(turns))
            return TranscriptResult(
                text=transcript.text,
                language=transcript.language,
                duration=transcript.duration,
                segments=apply_diarization_to_segments(transcript.segments, turns),
                diarization_enabled=True,
                diarization_status="completed",
            )
        except Exception as exc:
            logger.warning("Diarization failed; keeping Whisper transcript without speaker labels: %s", exc)
            return TranscriptResult(
                text=transcript.text,
                language=transcript.language,
                duration=transcript.duration,
                segments=transcript.segments,
                diarization_enabled=True,
                diarization_status="failed",
            )

    def _get_diarization_engine(self) -> DiarizationEngine | None:
        if not self._diarization_engine_loaded:
            self._diarization_engine = create_diarization_engine(self._settings)
            self._diarization_engine_loaded = True
        return self._diarization_engine


@dataclass(frozen=True)
class EngineCheckResult:
    engine_name: str
    model_size: str
    device: str
    compute_type: str
    cache_dir: str
    message: str


class FasterWhisperEngine(TranscriptionEngine):
    def __init__(self, model: Any, *, default_language: str | None, beam_size: int) -> None:
        self._model = model
        self._default_language = default_language
        self._beam_size = beam_size

    def transcribe(self, input_path: Path, *, transcription_language: str | None = None) -> TranscriptResult:
        if not input_path.exists():
            raise FileNotFoundError("Audio input file is missing.")

        language = _resolve_faster_whisper_language(transcription_language, self._default_language)
        segments_iterable, info = self._model.transcribe(
            str(input_path),
            language=language,
            beam_size=self._beam_size,
        )
        segments = list(segments_iterable)
        text = " ".join(segment.text.strip() for segment in segments if getattr(segment, "text", "").strip()).strip()
        result_language = getattr(info, "language", None) or language or "unknown"
        duration = float(getattr(info, "duration", 0) or 0)

        return TranscriptResult(
            text=text,
            language=str(result_language),
            duration=duration,
            segments=[
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=str(segment.text).strip(),
                )
                for segment in segments
            ],
        )


def _resolve_fake_language(transcription_language: str | None) -> str:
    if transcription_language in {"fr", "en"}:
        return transcription_language
    return "fr"


def _resolve_faster_whisper_language(transcription_language: str | None, default_language: str | None) -> str | None:
    if transcription_language == "auto":
        return None
    if transcription_language in {"fr", "en"}:
        return transcription_language
    if default_language in {None, "", "auto"}:
        return None
    return default_language


def create_engine(settings: WorkerSettings) -> TranscriptionEngine:
    if settings.transcription_engine == "fake":
        return _with_optional_diarization(FakeTranscriptionEngine(), settings)

    if settings.transcription_engine == "faster_whisper":
        model = _build_faster_whisper_model(settings)
        return _with_optional_diarization(FasterWhisperEngine(
            model,
            default_language=settings.whisper_language,
            beam_size=settings.whisper_beam_size,
        ), settings)

    raise ValueError(f"Unsupported transcription engine: {settings.transcription_engine}")


def _with_optional_diarization(engine: TranscriptionEngine, settings: WorkerSettings) -> TranscriptionEngine:
    if not settings.diarization_enabled:
        return engine
    return DiarizingTranscriptionEngine(engine, settings)


def check_engine(settings: WorkerSettings) -> EngineCheckResult:
    engine = create_engine(settings)
    del engine
    return EngineCheckResult(
        engine_name=settings.transcription_engine,
        model_size=settings.whisper_model_size,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        cache_dir=settings.whisper_model_cache_dir,
        message="Transcription engine loaded successfully.",
    )


def prepare_model(settings: WorkerSettings, *, model_size: str | None = None) -> Path:
    selected_model = model_size or settings.whisper_model_size
    cache_dir = Path(settings.whisper_model_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if settings.transcription_engine != "faster_whisper":
        raise RuntimeError("Model preparation is only applicable to TRANSCRIPTION_ENGINE=faster_whisper.")

    try:
        from faster_whisper.utils import download_model
    except ImportError as exc:
        raise RuntimeError(
            "Preparing a faster-whisper model requires the faster-whisper package to be installed."
        ) from exc

    try:
        model_path = download_model(selected_model, cache_dir=str(cache_dir))
    except Exception as exc:
        raise normalize_faster_whisper_error(exc) from exc

    return Path(model_path)


def _build_faster_whisper_model(settings: WorkerSettings) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "TRANSCRIPTION_ENGINE=faster_whisper requires the faster-whisper package to be installed."
        ) from exc

    if settings.whisper_device == "cuda":
        _ensure_cuda_available()

    cache_dir = Path(settings.whisper_model_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        try:
            return WhisperModel(
                settings.whisper_model_size,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
                download_root=str(cache_dir),
            )
        except TypeError as exc:
            if "download_root" not in str(exc):
                raise
            return WhisperModel(
                settings.whisper_model_size,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
    except Exception as exc:
        raise normalize_faster_whisper_error(exc) from exc


def _ensure_cuda_available() -> None:
    try:
        import ctranslate2
    except ImportError as exc:
        raise RuntimeError(
            "CUDA transcription requires ctranslate2, which is normally installed with faster-whisper."
        ) from exc

    if ctranslate2.get_cuda_device_count() < 1:
        raise RuntimeError("WHISPER_DEVICE=cuda was requested but no CUDA device is available.")


def normalize_faster_whisper_error(exc: Exception) -> RuntimeError:
    message = collect_exception_messages(exc)

    if "localentrynotfounderror" in message or "whisper model is not available locally" in message:
        return RuntimeError(
            "Whisper model is not available locally. Run scripts/prod/prepare-whisper-model.ps1 or allow worker network access to download it."
        )
    if "temporary failure in name resolution" in message or "name or service not known" in message:
        return RuntimeError(
            "Whisper model download failed because Hugging Face could not be resolved. Run scripts/prod/prepare-whisper-model.ps1 with worker network access, or fix container DNS/network access."
        )
    if "connection refused" in message:
        return RuntimeError(
            "Whisper model download failed because the remote service refused the connection. Check outbound connectivity from the worker container."
        )
    if "cuda" in message and "available" in message:
        return RuntimeError("WHISPER_DEVICE=cuda was requested but no CUDA device is available.")

    return RuntimeError(str(exc))


def collect_exception_messages(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = f"{type(current).__name__}: {current}".strip().lower()
        if text:
            parts.append(text)
        current = current.__cause__ or current.__context__
    return " | ".join(parts)
