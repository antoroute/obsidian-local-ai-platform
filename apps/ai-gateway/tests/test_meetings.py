from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import get_session_factory
from app.jobs import JOB_STATUS_COMPLETED
from app.main import app, get_llm_client
from app.models import Job
from app.services.ollama_client import OllamaChatResult, OllamaUnavailableError
from app.token_repository import create_api_token


def create_bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeMeetingOllamaClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    async def generate_meeting(
        self,
        *,
        model: str,
        title: str,
        transcript_chars: int,
        manual_notes_chars: int,
        template_chars: int,
        participants: list[str],
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult:
        assert model
        assert title
        assert transcript_chars >= 0
        assert manual_notes_chars >= 0
        assert template_chars > 0
        assert isinstance(participants, list)
        assert "Never invent facts" in system_prompt
        assert "Manual notes (priority source" in user_prompt
        assert "Transcript (primary source" in user_prompt
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        if self.fail:
            raise OllamaUnavailableError("unavailable")
        return OllamaChatResult(model=model, content="## Resume executif\n\nCompte rendu mocke.")


def create_token(scopes: list[str], user_id: str | None = None) -> str:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="meeting-token", scopes=scopes, user_id=user_id)
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


def valid_generate_from_job_payload(job_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": job_id,
        "title": "Reunion projet",
        "manual_notes": "Verifier les dates et actions.",
        "participants": ["Antonin", "Alice"],
        "template": "# Resume executif\n## Decisions prises\n## Actions a suivre",
        "model": "qwen2.5:14b",
    }
    payload.update(overrides)
    return payload


def create_completed_audio_job(
    *,
    user_id: str,
    transcript_text: str = "Transcript job complete.",
    job_type: str = "audio_transcription",
    status: str = "completed",
    with_result: bool = True,
    invalid_result_json: bool = False,
) -> str:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="seed-token", scopes=["meetings:generate"], user_id=user_id)
        del created_token
        job = Job(
            id="job-seeded-" + user_id + "-" + status + "-" + job_type.replace("_", "-"),
            user_id=user_id,
            type=job_type,
            status=status,
            input_path="/data/audio/input/audio.mp3",
            result_path=None,
            error="boom" if status == "failed" else None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        if with_result:
            result_dir = Path(tempfile.gettempdir()) / "obsidian-local-ai-platform-ai-gateway-test-results"
            result_dir.mkdir(parents=True, exist_ok=True)
            result_path = result_dir / f"{job.id}.json"
            if invalid_result_json:
                result_path.write_text("{invalid", encoding="utf-8")
            else:
                result_path.write_text(
                    json.dumps(
                        {
                            "text": transcript_text,
                            "language": "fr",
                            "duration": 0,
                            "segments": [{"start": 0, "end": 1, "text": transcript_text}],
                        }
                    ),
                    encoding="utf-8",
                )
            job.result_path = str(result_path)
        session.add(job)
        session.commit()
        return job.id


def test_meeting_generate_requires_token(client: TestClient) -> None:
    response = client.post("/v1/meetings/generate", json=valid_payload())

    assert response.status_code == 401


def test_meeting_generate_rejects_missing_scope(client: TestClient) -> None:
    token = create_token(["notes:summarize"])
    response = client.post("/v1/meetings/generate", headers=create_bearer_header(token), json=valid_payload())

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing required scope: meetings:generate"}


def test_meeting_generate_accepts_transcript_only(client: TestClient) -> None:
    app.dependency_overrides[get_llm_client] = lambda: FakeMeetingOllamaClient()
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
    app.dependency_overrides[get_llm_client] = lambda: FakeMeetingOllamaClient()
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(transcript="", manual_notes="Decision: conserver le budget."),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["usage"]["manual_notes_chars"] > 0


def test_meeting_generate_defaults_output_language_to_same_as_meeting(client: TestClient) -> None:
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post("/v1/meetings/generate", headers=create_bearer_header(token), json=valid_payload())

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "detect the main meeting language" in fake_client.system_prompts[0]


def test_meeting_generate_output_language_fr_adds_french_instruction(client: TestClient) -> None:
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(output_language="fr"),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "written in French" in fake_client.system_prompts[0]


def test_meeting_generate_output_language_en_adds_english_instruction(client: TestClient) -> None:
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(output_language="en"),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "written in English" in fake_client.system_prompts[0]


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
    app.dependency_overrides[get_llm_client] = lambda: FakeMeetingOllamaClient(fail=True)
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "The meeting generation backend is currently unavailable."}


def test_meeting_generate_fake_provider_returns_deterministic_markdown(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DEFAULT_MODEL", "fake-local-model")
    monkeypatch.setenv("ALLOWED_MODELS", "fake-local-model,mistral:latest,qwen2.5:14b")
    get_settings.cache_clear()
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(model="fake-local-model"),
    )

    get_settings.cache_clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "fake-local-model"
    assert "# Compte rendu fake" in payload["meeting_markdown"]
    assert "workflow Obsidian" in payload["meeting_markdown"]


def test_meeting_generate_from_job_requires_token(client: TestClient) -> None:
    response = client.post("/v1/meetings/generate-from-job", json=valid_generate_from_job_payload("job-1"))

    assert response.status_code == 401


def test_meeting_generate_from_job_rejects_missing_scope(client: TestClient) -> None:
    token = create_token(["notes:summarize"])
    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload("job-1"),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing required scope: meetings:generate"}


def test_meeting_generate_from_job_accepts_completed_job(client: TestClient) -> None:
    app.dependency_overrides[get_llm_client] = lambda: FakeMeetingOllamaClient()
    token = create_token(["meetings:generate"], user_id="user-job-ok")
    job_id = create_completed_audio_job(user_id="user-job-ok", transcript_text="Transcript from job.")

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job_id"] == job_id
    assert response.json()["meeting_markdown"] == "## Resume executif\n\nCompte rendu mocke."


def test_meeting_generate_from_job_transmits_output_language(client: TestClient) -> None:
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"], user_id="user-job-language")
    job_id = create_completed_audio_job(user_id="user-job-language", transcript_text="Transcript from job.")

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id, output_language="en"),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "written in English" in fake_client.system_prompts[0]


def test_meeting_generate_from_job_rejects_non_completed_job(client: TestClient) -> None:
    token = create_token(["meetings:generate"], user_id="user-job-queued")
    job_id = create_completed_audio_job(user_id="user-job-queued", status="queued", with_result=False)

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id),
    )

    assert response.status_code == 409


def test_meeting_generate_from_job_rejects_failed_job(client: TestClient) -> None:
    token = create_token(["meetings:generate"], user_id="user-job-failed")
    job_id = create_completed_audio_job(user_id="user-job-failed", status="failed", with_result=False)

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id),
    )

    assert response.status_code == 409


def test_meeting_generate_from_job_rejects_missing_job(client: TestClient) -> None:
    token = create_token(["meetings:generate"], user_id="user-job-none")

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload("missing-job"),
    )

    assert response.status_code == 404


def test_meeting_generate_from_job_rejects_other_user_job(client: TestClient) -> None:
    token = create_token(["meetings:generate"], user_id="user-a")
    job_id = create_completed_audio_job(user_id="user-b")

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id),
    )

    assert response.status_code == 404


def test_meeting_generate_from_job_rejects_invalid_result_json(client: TestClient) -> None:
    token = create_token(["meetings:generate"], user_id="user-invalid-json")
    job_id = create_completed_audio_job(user_id="user-invalid-json", invalid_result_json=True)

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id),
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Stored transcript result is invalid."}


def test_meeting_generate_from_job_rejects_forbidden_model(client: TestClient) -> None:
    token = create_token(["meetings:generate"], user_id="user-model-forbidden")
    job_id = create_completed_audio_job(user_id="user-model-forbidden")

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id, model="llama3.1:8b"),
    )

    assert response.status_code == 403


def test_meeting_generate_from_job_handles_ollama_unavailable(client: TestClient) -> None:
    app.dependency_overrides[get_llm_client] = lambda: FakeMeetingOllamaClient(fail=True)
    token = create_token(["meetings:generate"], user_id="user-job-unavailable")
    job_id = create_completed_audio_job(user_id="user-job-unavailable")

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "The meeting generation backend is currently unavailable."}


def test_meeting_generate_from_job_fake_provider_accepts_completed_job(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DEFAULT_MODEL", "fake-local-model")
    monkeypatch.setenv("ALLOWED_MODELS", "fake-local-model,mistral:latest,qwen2.5:14b")
    get_settings.cache_clear()
    token = create_token(["meetings:generate"], user_id="user-fake-job-ok")
    job_id = create_completed_audio_job(user_id="user-fake-job-ok", transcript_text="Transcript from fake job.")

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id, model="fake-local-model"),
    )

    get_settings.cache_clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == job_id
    assert payload["model"] == "fake-local-model"
    assert "# Compte rendu fake" in payload["meeting_markdown"]


def test_meeting_generate_from_job_fake_provider_still_rejects_other_user_job(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DEFAULT_MODEL", "fake-local-model")
    monkeypatch.setenv("ALLOWED_MODELS", "fake-local-model,mistral:latest,qwen2.5:14b")
    get_settings.cache_clear()
    token = create_token(["meetings:generate"], user_id="user-fake-a")
    job_id = create_completed_audio_job(user_id="user-fake-b")

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id, model="fake-local-model"),
    )

    get_settings.cache_clear()

    assert response.status_code == 404
