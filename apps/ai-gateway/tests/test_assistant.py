from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import get_session_factory
from app.main import app, get_llm_client
from app.services.ollama_client import OllamaChatResult, OllamaUnavailableError
from app.token_repository import create_api_token


def create_bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeAssistantClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    async def assistant_chat(
        self,
        *,
        model: str,
        mode: str,
        message_chars: int,
        context_chars: int,
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult:
        assert model
        assert mode in {"chat", "correct", "rewrite", "summarize"}
        assert message_chars >= 0
        assert context_chars >= 0
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        if self.fail:
            raise OllamaUnavailableError("unavailable")
        return OllamaChatResult(model=model, content=f"## Assistant mock\n\nMode: {mode}")


def create_token(scopes: list[str]) -> str:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="assistant-token", scopes=scopes)
    return created_token.plain_token


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "message": "Que dois-je faire ensuite ?",
        "context": "Note de projet avec trois actions.",
        "mode": "chat",
        "output_language": "fr",
        "model": "qwen2.5:14b",
    }
    payload.update(overrides)
    return payload


def test_assistant_chat_requires_token(client: TestClient) -> None:
    response = client.post("/v1/assistant/chat", json=valid_payload())

    assert response.status_code == 401


def test_assistant_chat_rejects_missing_scope(client: TestClient) -> None:
    token = create_token(["notes:summarize"])

    response = client.post("/v1/assistant/chat", headers=create_bearer_header(token), json=valid_payload())

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing required scope: assistant:chat"}


def test_assistant_chat_accepts_valid_chat(client: TestClient) -> None:
    fake_client = FakeAssistantClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["assistant:chat"])

    response = client.post("/v1/assistant/chat", headers=create_bearer_header(token), json=valid_payload())

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "chat"
    assert payload["answer_markdown"] == "## Assistant mock\n\nMode: chat"
    assert "Write the answer in French" in fake_client.system_prompts[0]
    assert "Do not use, fill, or imitate a note template" in fake_client.system_prompts[0]


def test_assistant_chat_same_as_input_uses_input_language_instruction_fr(client: TestClient) -> None:
    fake_client = FakeAssistantClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(message="Que fait ce compagnon ?", context="", output_language="same_as_input"),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "same_as_input: detect the main language" in fake_client.system_prompts[0]
    assert "French text must receive French output" in fake_client.system_prompts[0]


def test_assistant_chat_same_as_input_uses_input_language_instruction_en(client: TestClient) -> None:
    fake_client = FakeAssistantClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(message="What does this companion do?", context="", output_language="same_as_input"),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "English text must receive English output" in fake_client.system_prompts[0]


def test_assistant_chat_accepts_correct_with_context(client: TestClient) -> None:
    fake_client = FakeAssistantClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(message="", mode="correct", context="je sui pret"),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["mode"] == "correct"
    assert "Return only the corrected text" in fake_client.system_prompts[0]
    assert "Keep the same language as the text to process" in fake_client.system_prompts[0]
    assert "Do not translate unless explicitly forced" in fake_client.system_prompts[0]
    assert "detect the main language of the TEXT block" in fake_client.user_prompts[0]


def test_assistant_chat_accepts_rewrite_with_context(client: TestClient) -> None:
    fake_client = FakeAssistantClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(message="", mode="rewrite", context="Texte trop brut."),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["mode"] == "rewrite"
    assert "Response style: direct" in fake_client.system_prompts[0]
    assert "Keep the same language as the text to process" in fake_client.system_prompts[0]
    assert "Text to process:" in fake_client.user_prompts[0]
    assert "Texte trop brut." in fake_client.user_prompts[0]


def test_assistant_rewrite_professional_instruction_stays_direct(client: TestClient) -> None:
    fake_client = FakeAssistantClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(
            message="",
            mode="rewrite",
            context="salut on fait comme on peut pour le client",
            output_language="same_as_input",
            response_style="direct",
            action_preset="professional",
        ),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Professional rewrite preset" in fake_client.system_prompts[0]
    assert "Keep the same language as the text to process" in fake_client.system_prompts[0]
    assert "Return only the final usable result" in fake_client.system_prompts[0]
    assert "Apply the professional rewrite preset" in fake_client.user_prompts[0]
    assert "style plus professionnel" not in fake_client.user_prompts[0]
    assert "salut on fait comme on peut pour le client" in fake_client.user_prompts[0]
    assert "French text must receive French output" in fake_client.system_prompts[0]


def test_assistant_rejects_professional_preset_outside_rewrite(client: TestClient) -> None:
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(message="", mode="correct", context="je sui pret", action_preset="professional"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "action_preset=professional is only supported for rewrite mode."


def test_assistant_chat_summarize_requests_no_intro(client: TestClient) -> None:
    fake_client = FakeAssistantClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(message="", mode="summarize", context="Long texte a resumer.", output_language="same_as_input"),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Do not add an introduction" in fake_client.system_prompts[0]
    assert "Use the main language of the text to process" in fake_client.system_prompts[0]
    assert "French text must receive French output" in fake_client.system_prompts[0]


def test_assistant_chat_output_language_en_forces_english(client: TestClient) -> None:
    fake_client = FakeAssistantClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(output_language="en"),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Write the answer in English" in fake_client.system_prompts[0]


def test_assistant_chat_rejects_invalid_output_language(client: TestClient) -> None:
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(output_language="same_as_meeting"),
    )

    assert response.status_code == 422


def test_assistant_chat_rejects_forbidden_model(client: TestClient) -> None:
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(model="not-allowed:latest"),
    )

    assert response.status_code == 403


def test_assistant_chat_rejects_message_too_long(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MAX_ASSISTANT_MESSAGE_CHARS", "10")
    get_settings.cache_clear()
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(message="12345678901"),
    )

    get_settings.cache_clear()

    assert response.status_code == 413


def test_assistant_chat_handles_ollama_unavailable(client: TestClient) -> None:
    app.dependency_overrides[get_llm_client] = lambda: FakeAssistantClient(fail=True)
    token = create_token(["assistant:chat"])

    response = client.post("/v1/assistant/chat", headers=create_bearer_header(token), json=valid_payload())

    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "The assistant backend is currently unavailable."}


def test_assistant_chat_fake_provider_returns_deterministic_markdown(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DEFAULT_MODEL", "fake-local-model")
    monkeypatch.setenv("ALLOWED_MODELS", "fake-local-model,mistral:latest,qwen2.5:14b")
    get_settings.cache_clear()
    token = create_token(["assistant:chat"])

    response = client.post(
        "/v1/assistant/chat",
        headers=create_bearer_header(token),
        json=valid_payload(model="fake-local-model", mode="summarize"),
    )

    get_settings.cache_clear()

    assert response.status_code == 200
    assert "# Reponse assistant fake" in response.json()["answer_markdown"]
