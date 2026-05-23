from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.queue import get_audio_job_queue
from app.database import get_engine, get_session_factory, init_db
from app.main import app


class FakeAudioJobQueue:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    def enqueue_audio_transcription(self, job_id: str) -> None:
        self.job_ids.append(job_id)


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    database_path = tmp_path / "test-ai-gateway.db"
    audio_dir = tmp_path / "audio-storage"
    monkeypatch.setenv("AI_GATEWAY_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("ALLOWED_MODELS", "qwen2.5:14b,mistral:7b")
    monkeypatch.setenv("DEFAULT_MODEL", "qwen2.5:14b")
    monkeypatch.setenv("MAX_NOTE_CHARS", "200000")
    monkeypatch.setenv("MAX_TEMPLATE_CHARS", "50000")
    monkeypatch.setenv("MAX_TRANSCRIPT_CHARS", "300000")
    monkeypatch.setenv("MAX_MANUAL_NOTES_CHARS", "100000")
    monkeypatch.setenv("MAX_PARTICIPANTS", "100")
    monkeypatch.setenv("AUDIO_STORAGE_DIR", str(audio_dir))
    monkeypatch.setenv("MAX_AUDIO_UPLOAD_MB", "1")
    monkeypatch.setenv("AUDIO_QUEUE_NAME", "audio_transcription_jobs")
    monkeypatch.setenv("CORS_ENABLED", "true")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    monkeypatch.setenv("CORS_ALLOW_METHODS", "GET,POST,OPTIONS")
    monkeypatch.setenv("CORS_ALLOW_HEADERS", "Authorization,Content-Type")
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "false")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    init_db()

    fake_queue = FakeAudioJobQueue()
    app.dependency_overrides[get_audio_job_queue] = lambda: fake_queue

    with TestClient(app) as test_client:
        setattr(test_client, "fake_audio_queue", fake_queue)
        setattr(test_client, "audio_storage_dir", audio_dir)
        yield test_client

    app.dependency_overrides.clear()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
