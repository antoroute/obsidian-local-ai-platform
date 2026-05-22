from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import get_engine, get_session_factory, init_db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    database_path = tmp_path / "test-ai-gateway.db"
    monkeypatch.setenv("AI_GATEWAY_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("ALLOWED_MODELS", "qwen2.5:14b,mistral:7b")
    monkeypatch.setenv("DEFAULT_MODEL", "qwen2.5:14b")
    monkeypatch.setenv("MAX_NOTE_CHARS", "200000")
    monkeypatch.setenv("MAX_TEMPLATE_CHARS", "50000")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    init_db()

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
