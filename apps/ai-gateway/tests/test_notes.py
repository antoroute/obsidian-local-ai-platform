from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import get_session_factory
from app.main import app, get_ollama_client
from app.services.ollama_client import OllamaChatResult, OllamaUnavailableError
from app.token_repository import create_api_token


def create_bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeOllamaClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def summarize_markdown(self, *, model: str, system_prompt: str, user_prompt: str) -> OllamaChatResult:
        assert "Never invent facts" in system_prompt
        assert "Template or instructions" in user_prompt
        if self.fail:
            raise OllamaUnavailableError("unavailable")
        return OllamaChatResult(model=model, content="## Summary\n\nMocked markdown summary.")


def create_token(scopes: list[str]) -> str:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="test-token", scopes=scopes)
    return created_token.plain_token


def test_notes_summarize_requires_token(client: TestClient) -> None:
    response = client.post("/v1/notes/summarize", json={"title": "A", "note_content": "B", "template": ""})

    assert response.status_code == 401


def test_notes_summarize_rejects_missing_scope(client: TestClient) -> None:
    token = create_token(["models:list"])
    response = client.post(
        "/v1/notes/summarize",
        headers=create_bearer_header(token),
        json={"title": "A", "note_content": "B", "template": ""},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing required scope: notes:summarize"}


def test_notes_summarize_accepts_valid_request(client: TestClient) -> None:
    app.dependency_overrides[get_ollama_client] = lambda: FakeOllamaClient()
    token = create_token(["notes:summarize"])

    response = client.post(
        "/v1/notes/summarize",
        headers=create_bearer_header(token),
        json={
            "title": "Project sync",
            "note_content": "Agenda:\n- Budget\n- Risks",
            "template": "## Summary\n## Risks\n## Actions",
            "model": "qwen2.5:14b",
        },
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "model": "qwen2.5:14b",
        "title": "Project sync",
        "summary_markdown": "## Summary\n\nMocked markdown summary.",
        "usage": {"prompt_chars": 24, "template_chars": 30},
    }


def test_notes_summarize_rejects_forbidden_model(client: TestClient) -> None:
    token = create_token(["notes:summarize"])
    response = client.post(
        "/v1/notes/summarize",
        headers=create_bearer_header(token),
        json={
            "title": "Project sync",
            "note_content": "Agenda:\n- Budget",
            "template": "## Summary",
            "model": "llama3.1:8b",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Requested model is not allowed."}


def test_notes_summarize_rejects_empty_note(client: TestClient) -> None:
    token = create_token(["notes:summarize"])
    response = client.post(
        "/v1/notes/summarize",
        headers=create_bearer_header(token),
        json={"title": "A", "note_content": "", "template": ""},
    )

    assert response.status_code == 422


def test_notes_summarize_rejects_note_too_long(client: TestClient, monkeypatch) -> None:
    app.dependency_overrides[get_ollama_client] = lambda: FakeOllamaClient()
    monkeypatch.setenv("MAX_NOTE_CHARS", "10")
    from app.config import get_settings

    get_settings.cache_clear()
    token = create_token(["notes:summarize"])
    response = client.post(
        "/v1/notes/summarize",
        headers=create_bearer_header(token),
        json={"title": "A", "note_content": "12345678901", "template": ""},
    )
    app.dependency_overrides.clear()
    get_settings.cache_clear()

    assert response.status_code == 413


def test_notes_summarize_handles_ollama_unavailable(client: TestClient) -> None:
    app.dependency_overrides[get_ollama_client] = lambda: FakeOllamaClient(fail=True)
    token = create_token(["notes:summarize"])

    response = client.post(
        "/v1/notes/summarize",
        headers=create_bearer_header(token),
        json={"title": "A", "note_content": "Meeting notes", "template": ""},
    )

    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "The summarization backend is currently unavailable."}


def test_no_generic_ollama_proxy_route_exists(client: TestClient) -> None:
    paths = {route.path for route in app.routes}

    assert "/v1/ollama" not in paths
    assert not any(path.startswith("/v1/ollama/") for path in paths)
