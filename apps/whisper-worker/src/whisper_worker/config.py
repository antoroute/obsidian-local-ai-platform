from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    redis_url: str
    queue_name: str
    audio_storage_dir: str
    transcription_engine: str
    whisper_model_size: str
    whisper_device: str
    whisper_compute_type: str
    whisper_language: str | None
    whisper_beam_size: int
    whisper_model_cache_dir: str
    hf_home: str | None
    huggingface_hub_cache: str | None
    diarization_enabled: bool = False
    diarization_provider: str = "pyannote"
    diarization_model: str = "pyannote/speaker-diarization-3.1"
    diarization_device: str = "cpu"
    diarization_model_cache_dir: str = "/models/diarization"
    diarization_min_speakers: int | None = None
    diarization_max_speakers: int | None = None


def get_settings() -> WorkerSettings:
    return WorkerSettings(
        database_url=os.getenv("WHISPER_WORKER_DATABASE_URL", "sqlite:///./whisper_worker.db"),
        redis_url=os.getenv("WHISPER_WORKER_REDIS_URL", "redis://redis:6379/0"),
        queue_name=os.getenv("WHISPER_WORKER_QUEUE", "audio_transcription_jobs"),
        audio_storage_dir=os.getenv("AUDIO_STORAGE_DIR", "./data/audio"),
        transcription_engine=os.getenv("TRANSCRIPTION_ENGINE", os.getenv("WHISPER_WORKER_TRANSCRIPTION_MODE", "fake")),
        whisper_model_size=os.getenv("WHISPER_MODEL_SIZE", "medium"),
        whisper_device=os.getenv("WHISPER_DEVICE", "cpu"),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        whisper_language=os.getenv("WHISPER_LANGUAGE") or None,
        whisper_beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "5")),
        whisper_model_cache_dir=os.getenv("WHISPER_MODEL_CACHE_DIR", "/models/whisper"),
        hf_home=os.getenv("HF_HOME") or None,
        huggingface_hub_cache=os.getenv("HUGGINGFACE_HUB_CACHE") or None,
        diarization_enabled=_parse_bool(os.getenv("DIARIZATION_ENABLED", "false")),
        diarization_provider=os.getenv("DIARIZATION_PROVIDER", "pyannote"),
        diarization_model=os.getenv("DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1"),
        diarization_device=os.getenv("DIARIZATION_DEVICE", os.getenv("WHISPER_DEVICE", "cpu")),
        diarization_model_cache_dir=os.getenv("DIARIZATION_MODEL_CACHE_DIR", "/models/diarization"),
        diarization_min_speakers=_parse_optional_int(os.getenv("DIARIZATION_MIN_SPEAKERS")),
        diarization_max_speakers=_parse_optional_int(os.getenv("DIARIZATION_MAX_SPEAKERS")),
    )


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)
