from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import get_session_factory
from app.main import app, get_ollama_client
from app.services.ollama_client import OllamaChatResult, OllamaUnavailableError
from app.token_repository import create_api_token


def create_bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeMeetingOllamaClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def summarize_markdown(self, *, model: str, system_prompt: str, user_prompt: str) -> OllamaChatResult:
        assert "Never invent facts" in system_prompt
        assert "Manual notes (priority source" in user_prompt
        assert "Transcript (primary source" in user_prompt
        if self.fail:
            raise OllamaUnavailableError("unavailable")
        return OllamaChatResult(model=model, content="## Resume executif\n\nCompte rendu mocke.")


def create_token(scopes: list[str]) -> str:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="meeting-token", scopes=scopes)
    return created_token.plain_token


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "Reunion projet",
        "transcript": "Debut de reunion. Point budget.",
        "manual_notes": "",
        "participants": ["Antonin", "Alice"],
        "template": "# Resume executif\n## Decisions prises\n## Actions a suivre",
        "model": "qwen2.5:14b",
    }
    payload.update(overrides)
    return payload


def test_meeting_generate_requires_token(client: TestClient) -> None:
    response = client.post("/v1/meetings/generate", json=valid_payload())

    assert response.status_code == 401


def test_meeting_generate_rejects_missing_scope(client: TestClient) -> None:
    token = create_token(["notes:summarize"])
    response = client.post("/v1/meetings/generate", headers=create_bearer_header(token), json=valid_payload())

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing required scope: meetings:generate"}


def test_meeting_generate_accepts_transcript_only(client: TestClient) -> None:
    app.dependency_overrides[get_ollama_client] = lambda: FakeMeetingOllamaClient()
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(manual_notes=""),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["meeting_markdown"] == "## Resume executif\n\nCompte rendu mocke."


def test_meeting_generate_accepts_manual_notes_only(client: TestClient) -> None:
    app.dependency_overrides[get_ollama_client] = lambda: FakeMeetingOllamaClient()
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(transcript="", manual_notes="Decision: conserver le budget."),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["usage"]["manual_notes_chars"] > 0


def test_meeting_generate_rejects_missing_sources(client: TestClient) -> None:
    token = create_token(["meetings:generate"])
    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(transcript="", manual_notes=""),
    )

    assert response.status_code == 422


def test_meeting_generate_rejects_forbidden_model(client: TestClient) -> None:
    token = create_token(["meetings:generate"])
    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(model="llama3.1:8b"),
    )

    assert response.status_code == 403


def test_meeting_generate_rejects_transcript_too_long(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MAX_TRANSCRIPT_CHARS", "10")
    from app.config import get_settings

    get_settings.cache_clear()
    token = create_token(["meetings:generate"])
    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(transcript="12345678901"),
    )
    get_settings.cache_clear()

    assert response.status_code == 413


def test_meeting_generate_rejects_manual_notes_too_long(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MAX_MANUAL_NOTES_CHARS", "10")
    from app.config import get_settings

    get_settings.cache_clear()
    token = create_token(["meetings:generate"])
    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(transcript="", manual_notes="12345678901"),
    )
    get_settings.cache_clear()

    assert response.status_code == 413


def test_meeting_generate_rejects_too_many_participants(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MAX_PARTICIPANTS", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    token = create_token(["meetings:generate"])
    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(participants=["Antonin", "Alice"]),
    )
    get_settings.cache_clear()

    assert response.status_code == 413


def test_meeting_generate_handles_ollama_unavailable(client: TestClient) -> None:
    app.dependency_overrides[get_ollama_client] = lambda: FakeMeetingOllamaClient(fail=True)
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "The meeting generation backend is currently unavailable."}
