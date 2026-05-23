from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    redis_url: str
    queue_name: str
    audio_storage_dir: str
    transcription_mode: str


def get_settings() -> WorkerSettings:
    return WorkerSettings(
        database_url=os.getenv("WHISPER_WORKER_DATABASE_URL", "sqlite:///./whisper_worker.db"),
        redis_url=os.getenv("WHISPER_WORKER_REDIS_URL", "redis://redis:6379/0"),
        queue_name=os.getenv("WHISPER_WORKER_QUEUE", "audio_transcription_jobs"),
        audio_storage_dir=os.getenv("AUDIO_STORAGE_DIR", "./data/audio"),
        transcription_mode=os.getenv("WHISPER_WORKER_TRANSCRIPTION_MODE", "fake"),
    )
