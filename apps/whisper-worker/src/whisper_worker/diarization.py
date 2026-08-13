from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
from collections.abc import Iterator
from typing import Any
import yaml

from whisper_worker.config import WorkerSettings
from whisper_worker.repositories import TranscriptSegment


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str


class DiarizationEngine:
    def diarize(self, input_path: Path) -> list[SpeakerTurn]:
        raise NotImplementedError


class PyannoteDiarizationEngine(DiarizationEngine):
    def __init__(self, pipeline: Any, *, min_speakers: int | None, max_speakers: int | None) -> None:
        self._pipeline = pipeline
        self._min_speakers = min_speakers
        self._max_speakers = max_speakers

    def diarize(self, input_path: Path) -> list[SpeakerTurn]:
        kwargs: dict[str, int] = {}
        if self._min_speakers is not None:
            kwargs["min_speakers"] = self._min_speakers
        if self._max_speakers is not None:
            kwargs["max_speakers"] = self._max_speakers

        annotation = self._pipeline(str(input_path), **kwargs)
        turns: list[SpeakerTurn] = []
        speaker_map: dict[str, str] = {}
        for turn, _, raw_speaker in annotation.itertracks(yield_label=True):
            speaker_key = str(raw_speaker)
            speaker = speaker_map.setdefault(speaker_key, f"Speaker {len(speaker_map) + 1}")
            turns.append(SpeakerTurn(start=float(turn.start), end=float(turn.end), speaker=speaker))
        return turns


class NemoSortformerDiarizationEngine(DiarizationEngine):
    def __init__(self, model: Any) -> None:
        self._model = model

    def diarize(self, input_path: Path) -> list[SpeakerTurn]:
        with tempfile.TemporaryDirectory(prefix="sortformer-audio-") as temp_dir:
            wav_path = Path(temp_dir) / f"{input_path.stem}.wav"
            convert_audio_for_sortformer(input_path, wav_path)
            predicted_segments = self._model.diarize(audio=str(wav_path), batch_size=1)
        return parse_sortformer_segments(predicted_segments)


def create_diarization_engine(settings: WorkerSettings) -> DiarizationEngine | None:
    if not settings.diarization_enabled:
        return None
    if settings.diarization_provider == "nemo_sortformer":
        return create_nemo_sortformer_engine(settings)
    if settings.diarization_provider != "pyannote":
        raise ValueError(f"Unsupported diarization provider: {settings.diarization_provider}")

    ensure_python_int_digit_compat()
    model_cache_dir = Path(settings.diarization_model_cache_dir)
    model_cache_dir.mkdir(parents=True, exist_ok=True)
    missing = missing_required_pyannote_cache_entries(model_cache_dir, settings.diarization_model)
    if missing:
        missing_list = ", ".join(missing)
        raise RuntimeError(
            f"Diarization model '{settings.diarization_model}' is not fully available in the local cache. "
            f"Missing: {missing_list}. "
            "Run scripts/prod/prepare-diarization-model.ps1 with a Hugging Face token after accepting the pyannote model terms."
        )

    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise RuntimeError("DIARIZATION_ENABLED=true requires the pyannote.audio extra to be installed.") from exc

    pipeline = load_pyannote_pipeline(
        Pipeline,
        settings.diarization_model,
        cache_dir=model_cache_dir,
        local_files_only=True,
    )
    if settings.diarization_device == "cuda":
        try:
            import torch

            pipeline.to(torch.device("cuda"))
        except Exception as exc:
            raise RuntimeError("DIARIZATION_DEVICE=cuda was requested but pyannote could not use CUDA.") from exc
    elif settings.diarization_device == "cpu":
        try:
            import torch

            pipeline.to(torch.device("cpu"))
        except Exception:
            pass
    return PyannoteDiarizationEngine(
        pipeline,
        min_speakers=settings.diarization_min_speakers,
        max_speakers=settings.diarization_max_speakers,
    )


def create_nemo_sortformer_engine(settings: WorkerSettings) -> DiarizationEngine:
    if settings.diarization_device != "cuda":
        raise RuntimeError("DIARIZATION_PROVIDER=nemo_sortformer requires DIARIZATION_DEVICE=cuda.")

    ensure_python_int_digit_compat()
    try:
        from nemo.collections.asr.models.sortformer_diar_models import SortformerEncLabelModel
    except ImportError as exc:
        raise RuntimeError(
            "DIARIZATION_PROVIDER=nemo_sortformer requires the NeMo ASR dependencies to be installed."
        ) from exc

    model_path = get_sortformer_model_path(Path(settings.diarization_model_cache_dir), settings.diarization_model)
    try:
        model = SortformerEncLabelModel.restore_from(
            restore_path=str(model_path),
            map_location="cuda",
            strict=False,
        )
        model.eval()
    except Exception as exc:
        raise RuntimeError(f"Could not load NeMo Sortformer model from {model_path}: {exc}") from exc
    return NemoSortformerDiarizationEngine(model)


def get_sortformer_model_path(cache_dir: Path, model_name: str) -> Path:
    model_file = Path(model_name)
    if model_file.exists() and model_file.is_file():
        return model_file

    repo_cache_dir = cache_dir / f"models--{model_name.replace('/', '--')}"
    candidates = list(repo_cache_dir.rglob("*.nemo")) if repo_cache_dir.exists() else []
    if not candidates:
        candidates = list(cache_dir.rglob("*.nemo")) if cache_dir.exists() else []
    if not candidates:
        raise RuntimeError(
            f"NeMo Sortformer model '{model_name}' is not available in {cache_dir}. "
            "Run scripts/prod/prepare-sortformer-model.ps1 before starting the worker."
        )
    return sorted(candidates, key=lambda path: len(str(path)))[0]


def convert_audio_for_sortformer(input_path: Path, wav_path: Path) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        str(wav_path),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Could not convert audio for NeMo Sortformer: {exc.stderr.strip()}") from exc


def parse_sortformer_segments(predicted_segments: Any) -> list[SpeakerTurn]:
    # NeMo may return either a flat list for one file or a list per input file.
    segments = predicted_segments
    if isinstance(segments, tuple):
        segments = segments[0]
    if isinstance(segments, list) and len(segments) == 1 and isinstance(segments[0], list):
        segments = segments[0]

    turns: list[SpeakerTurn] = []
    speaker_map: dict[str, str] = {}
    for item in segments or []:
        parsed = parse_sortformer_segment(item)
        if parsed is None:
            continue
        start, end, raw_speaker = parsed
        speaker_key = str(raw_speaker)
        speaker = speaker_map.setdefault(speaker_key, f"Speaker {len(speaker_map) + 1}")
        turns.append(SpeakerTurn(start=start, end=end, speaker=speaker))
    return turns


def parse_sortformer_segment(item: Any) -> tuple[float, float, str] | None:
    if isinstance(item, str):
        parts = item.replace(",", " ").split()
        if len(parts) < 3:
            return None
        return float(parts[0]), float(parts[1]), parts[2]
    if isinstance(item, dict):
        start = item.get("start") or item.get("begin") or item.get("start_time")
        end = item.get("end") or item.get("end_time") or item.get("stop")
        speaker = item.get("speaker") or item.get("speaker_id") or item.get("label")
        if start is None or end is None or speaker is None:
            return None
        return float(start), float(end), str(speaker)
    if isinstance(item, (list, tuple)) and len(item) >= 3:
        return float(item[0]), float(item[1]), str(item[2])
    return None


def apply_diarization_to_segments(segments: list[TranscriptSegment], turns: list[SpeakerTurn]) -> list[TranscriptSegment]:
    if not turns:
        return segments
    return [
        TranscriptSegment(
            start=segment.start,
            end=segment.end,
            text=segment.text,
            speaker=find_best_speaker(segment, turns),
        )
        for segment in segments
    ]


def find_best_speaker(segment: TranscriptSegment, turns: list[SpeakerTurn]) -> str | None:
    best_speaker: str | None = None
    best_overlap = 0.0
    midpoint = (segment.start + segment.end) / 2
    closest_distance = float("inf")
    closest_speaker: str | None = None
    for turn in turns:
        overlap = max(0.0, min(segment.end, turn.end) - max(segment.start, turn.start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = turn.speaker
        distance = min(abs(midpoint - turn.start), abs(midpoint - turn.end))
        if distance < closest_distance:
            closest_distance = distance
            closest_speaker = turn.speaker
    return best_speaker or closest_speaker


def prepare_diarization_model(settings: WorkerSettings, *, model_name: str | None = None) -> Path:
    selected_model = model_name or settings.diarization_model
    cache_dir = Path(settings.diarization_model_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    ensure_python_int_digit_compat()
    if selected_model.startswith("nvidia/diar_sortformer"):
        download_sortformer_snapshot(selected_model, cache_dir)
        get_sortformer_model_path(cache_dir, selected_model)
        return cache_dir

    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise RuntimeError("Preparing a diarization model requires the pyannote.audio extra to be installed.") from exc

    download_required_pyannote_snapshots(selected_model, cache_dir)
    load_pyannote_pipeline(Pipeline, selected_model, cache_dir=cache_dir, local_files_only=False)
    missing = missing_required_pyannote_cache_entries(cache_dir, selected_model)
    if missing:
        missing_list = ", ".join(missing)
        raise RuntimeError(
            f"Diarization model '{selected_model}' is not fully available after preparation. "
            f"Missing: {missing_list}. "
            "Check that the Hugging Face token can access all pyannote model terms."
        )
    return cache_dir


def download_sortformer_snapshot(model_name: str, cache_dir: Path) -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("Preparing NeMo Sortformer models requires huggingface-hub.") from exc

    token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN")
    snapshot_kwargs: dict[str, object] = {
        "cache_dir": str(cache_dir),
        "allow_patterns": ["*.nemo", "README.md", ".gitattributes"],
    }
    if token:
        snapshot_kwargs["token"] = token
    snapshot_download(repo_id=model_name, **snapshot_kwargs)


def download_required_pyannote_snapshots(model_name: str, cache_dir: Path) -> None:
    """Materialize pyannote pipeline dependencies that may be loaded lazily."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("Preparing diarization models requires huggingface-hub.") from exc

    token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN")
    snapshot_kwargs: dict[str, object] = {"cache_dir": str(cache_dir)}
    if token:
        snapshot_kwargs["token"] = token

    snapshot_download(repo_id=model_name, **snapshot_kwargs)
    for dependency in required_pyannote_dependency_models(model_name):
        snapshot_download(repo_id=dependency, **snapshot_kwargs)


def required_pyannote_dependency_models(model_name: str) -> list[str]:
    if model_name == "pyannote/speaker-diarization-3.1":
        return [
            "pyannote/segmentation-3.0",
            "pyannote/wespeaker-voxceleb-resnet34-LM",
        ]
    return []


def ensure_python_int_digit_compat() -> None:
    # Ubuntu 22.04's python3.11 package can be 3.11.0rc1, while newer torch
    # expects these CPython APIs. They are harmless no-ops for our usage.
    import sys

    if not hasattr(sys, "get_int_max_str_digits"):
        def get_int_max_str_digits() -> int:
            return 4300

        setattr(sys, "get_int_max_str_digits", get_int_max_str_digits)
    if not hasattr(sys, "set_int_max_str_digits"):
        def set_int_max_str_digits(maxdigits: int) -> None:
            return None

        setattr(sys, "set_int_max_str_digits", set_int_max_str_digits)


def load_pyannote_pipeline(
    pipeline_class: Any,
    model_name: str,
    *,
    cache_dir: Path,
    local_files_only: bool,
) -> Any:
    if local_files_only:
        missing = missing_required_pyannote_cache_entries(cache_dir, model_name)
        if missing:
            missing_list = ", ".join(missing)
            raise RuntimeError(
                f"Diarization model '{model_name}' is not fully available in the local cache. "
                f"Missing: {missing_list}. "
                "Run scripts/prod/prepare-diarization-model.ps1 with a Hugging Face token after accepting the pyannote model terms."
            )

    checkpoint_path = model_name
    if local_files_only:
        checkpoint_path = str(build_offline_pyannote_pipeline_config(cache_dir, model_name))

    token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN")
    base_kwargs: dict[str, object] = {"cache_dir": str(cache_dir)}
    attempts: list[dict[str, object]] = []
    if token:
        attempts.append({**base_kwargs, "use_auth_token": token})
        attempts.append({**base_kwargs, "token": token})
    attempts.append(base_kwargs)
    if not local_files_only:
        # Older pyannote/huggingface-hub combinations may not accept
        # local_files_only. Keep that fallback only during explicit model
        # preparation, never during runtime where network is intentionally off.
        attempts.append({"cache_dir": str(cache_dir)})

    last_error: Exception | None = None
    with temporary_huggingface_offline_mode(enabled=local_files_only):
        for kwargs in attempts:
            try:
                pipeline = pipeline_class.from_pretrained(checkpoint_path, **kwargs)
                if pipeline is None:
                    raise RuntimeError(
                        f"Could not load diarization model '{model_name}'. Accept the pyannote model terms on Hugging Face and pass -HuggingFaceToken or set HF_TOKEN during preparation."
                    )
                return pipeline
            except TypeError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = normalize_pyannote_load_error(exc, model_name, local_files_only=local_files_only)
                break

    if last_error is not None:
        if local_files_only and isinstance(last_error, TypeError):
            raise RuntimeError(
                f"Diarization model '{model_name}' could not be loaded from the local cache. "
                "Run scripts/prod/prepare-diarization-model.ps1 with a Hugging Face token after accepting the pyannote model terms."
            ) from last_error
        raise last_error
    return pipeline_class.from_pretrained(checkpoint_path)


@contextmanager
def temporary_huggingface_offline_mode(*, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return

    previous_hf_hub_offline = os.environ.get("HF_HUB_OFFLINE")
    previous_transformers_offline = os.environ.get("TRANSFORMERS_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        yield
    finally:
        restore_env_var("HF_HUB_OFFLINE", previous_hf_hub_offline)
        restore_env_var("TRANSFORMERS_OFFLINE", previous_transformers_offline)


def restore_env_var(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def missing_required_pyannote_cache_entries(cache_dir: Path, model_name: str) -> list[str]:
    missing: list[str] = []
    if get_snapshot_file(cache_dir, model_name, "config.yaml") is None:
        missing.append(f"{model_name}/config.yaml")

    if model_name == "pyannote/speaker-diarization-3.1":
        if get_snapshot_file(cache_dir, "pyannote/segmentation-3.0", "pytorch_model.bin") is None:
            missing.append("pyannote/segmentation-3.0/pytorch_model.bin")
        if get_snapshot_file(cache_dir, "pyannote/wespeaker-voxceleb-resnet34-LM", "pytorch_model.bin") is None:
            missing.append("pyannote/wespeaker-voxceleb-resnet34-LM/pytorch_model.bin")

    return missing


def has_snapshot_file(cache_dir: Path, model_name: str, file_name: str) -> bool:
    return get_snapshot_file(cache_dir, model_name, file_name) is not None


def get_snapshot_file(cache_dir: Path, model_name: str, file_name: str) -> Path | None:
    model_cache_dir = cache_dir / f"models--{model_name.replace('/', '--')}"
    snapshots_dir = model_cache_dir / "snapshots"
    if not snapshots_dir.exists():
        return None
    return next((path for path in snapshots_dir.rglob(file_name) if path.name == file_name), None)


def build_offline_pyannote_pipeline_config(cache_dir: Path, model_name: str) -> Path:
    config_path = get_snapshot_file(cache_dir, model_name, "config.yaml")
    if config_path is None:
        raise RuntimeError(f"Diarization model '{model_name}' has no local config.yaml in {cache_dir}.")

    if model_name != "pyannote/speaker-diarization-3.1":
        return config_path

    segmentation_path = get_snapshot_file(cache_dir, "pyannote/segmentation-3.0", "pytorch_model.bin")
    embedding_path = get_snapshot_file(cache_dir, "pyannote/wespeaker-voxceleb-resnet34-LM", "pytorch_model.bin")
    if segmentation_path is None or embedding_path is None:
        missing = missing_required_pyannote_cache_entries(cache_dir, model_name)
        raise RuntimeError(
            f"Diarization model '{model_name}' is not fully available in the local cache. "
            f"Missing: {', '.join(missing)}."
        )

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    params = config.setdefault("pipeline", {}).setdefault("params", {})
    params["segmentation"] = str(segmentation_path)
    params["embedding"] = str(embedding_path)

    offline_dir = cache_dir / "offline-configs"
    offline_dir.mkdir(parents=True, exist_ok=True)
    offline_config_path = offline_dir / f"{model_name.replace('/', '--')}.yaml"
    offline_config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return offline_config_path


def normalize_pyannote_load_error(exc: Exception, model_name: str, *, local_files_only: bool) -> RuntimeError:
    message = collect_exception_messages(exc)

    if local_files_only:
        if "localentrynotfounderror" in message or "cannot find the requested files in the local cache" in message:
            return RuntimeError(
                f"Diarization model '{model_name}' is not available in the local cache. "
                "Run scripts/prod/prepare-diarization-model.ps1 with a Hugging Face token after accepting the pyannote model terms."
            )
        if "temporary failure in name resolution" in message or "name or service not known" in message:
            return RuntimeError(
                f"Diarization model '{model_name}' attempted a network lookup but runtime is offline. "
                "Prepare the model cache first with scripts/prod/prepare-diarization-model.ps1."
            )

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
