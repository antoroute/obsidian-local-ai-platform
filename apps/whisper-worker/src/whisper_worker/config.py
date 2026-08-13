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
    whisper_cpu_threads: int = 0
    whisper_num_workers: int = 1


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
        whisper_cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "0")),
        whisper_num_workers=int(os.getenv("WHISPER_NUM_WORKERS", "1")),
    )
