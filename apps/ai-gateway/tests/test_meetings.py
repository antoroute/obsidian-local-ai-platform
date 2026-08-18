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
        self.predigest_system_prompts: list[str] = []
        self.predigest_user_prompts: list[str] = []

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
        assert "Never invent facts" in system_prompt or "Do not invent owners" in system_prompt
        assert "Manual notes" in user_prompt
        assert "Transcript" in user_prompt or "transcript" in user_prompt or "Prepared meeting brief" in user_prompt
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        if self.fail:
            raise OllamaUnavailableError("unavailable")
        return OllamaChatResult(model=model, content="## Resume executif\n\nCompte rendu mocke.")

    async def predigest_meeting(
        self,
        *,
        model: str,
        title: str,
        transcript_chars: int,
        manual_notes_chars: int,
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult:
        assert model
        assert title
        assert transcript_chars >= 0
        assert manual_notes_chars >= 0
        assert "This is not the final report" in system_prompt
        assert "Manual notes (priority source" in user_prompt
        assert "Transcript (primary source" in user_prompt
        self.predigest_system_prompts.append(system_prompt)
        self.predigest_user_prompts.append(user_prompt)
        if self.fail:
            raise OllamaUnavailableError("unavailable")
        return OllamaChatResult(model=model, content="## Brief\n\n- Decision candidate: conserver le budget.")


class NoisyDeepThinkClient(FakeMeetingOllamaClient):
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
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        return OllamaChatResult(
            model=model,
            content=(
                "# Compte rendu de reunion\n\n"
                "## Resume executif\n\n"
                "- Information utile conservee.\n\n"
                "## Transcription language hint\n"
                "Le texte est en francais.\n\n"
                "## Decisions\n"
                "Aucune decision formelle n'a ete prise."
            ),
        )


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
    diarization_status: str = "disabled",
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
                            "diarization_enabled": diarization_status != "disabled",
                            "diarization_status": diarization_status,
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
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(manual_notes=""),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["meeting_markdown"] == "## Resume executif\n\nCompte rendu mocke."
    assert fake_client.predigest_user_prompts == []


def test_meeting_generate_long_transcript_uses_predigest(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEETING_PREDIGEST_MIN_CHARS", "50")
    get_settings.cache_clear()
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(transcript="Decision budget. " * 10),
    )

    app.dependency_overrides.clear()
    get_settings.cache_clear()

    assert response.status_code == 200
    assert len(fake_client.predigest_user_prompts) == 1
    assert "Prepared meeting brief" in fake_client.user_prompts[0]
    assert "Decision candidate: conserver le budget" in fake_client.user_prompts[0]


def test_meeting_generate_very_long_transcript_uses_bounded_chronological_chunks(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEETING_PREDIGEST_MIN_CHARS", "50")
    monkeypatch.setenv("MEETING_PREDIGEST_CHUNK_CHARS", "4000")
    monkeypatch.setenv("MEETING_PREDIGEST_MAX_CHUNKS", "3")
    get_settings.cache_clear()
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(transcript=("Decision budget avec contexte detaille. " * 350)),
    )

    app.dependency_overrides.clear()
    get_settings.cache_clear()

    assert response.status_code == 200
    assert len(fake_client.predigest_user_prompts) == 3
    assert response.json()["generation_stages"] == 4
    assert response.json()["generation_analysis"]["sections_count"] == 3
    assert "Chronological part: 1/3" in fake_client.predigest_user_prompts[0]
    assert "Chronological part: 3/3" in fake_client.predigest_user_prompts[2]


def test_meeting_generate_deep_think_uses_section_calls(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEETING_DEEP_THINK_MAX_SECTIONS", "3")
    monkeypatch.setenv("MEETING_DEEP_THINK_EXCERPT_CHARS_PER_SECTION", "500")
    get_settings.cache_clear()
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(
            generation_mode="deep_think",
            output_language="same_as_meeting",
            manual_notes="## IAM\n- Identify IAM owners.\n\n## Endpoint\n- Check EDR coverage.",
            transcript="The IAM owner must be identified. Endpoint EDR coverage is important. " * 20,
        ),
    )

    app.dependency_overrides.clear()
    get_settings.cache_clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["generation_mode"] == "deep_think"
    assert payload["generation_stages"] == 2
    assert payload["generation_analysis"]["mode"] == "deep_think"
    assert payload["generation_analysis"]["sections_count"] == 2
    assert payload["generation_analysis"]["section_titles"] == ["IAM", "Endpoint"]
    assert payload["generation_analysis"]["transcript_chars"] > 0
    assert payload["generation_analysis"]["manual_notes_chars"] > 0
    assert len(fake_client.user_prompts) == 2
    assert fake_client.predigest_user_prompts == []
    assert "Section to write: IAM" in fake_client.user_prompts[0]
    assert "Section to write: Endpoint" in fake_client.user_prompts[1]
    assert "Identify IAM owners" not in json.dumps(payload["generation_analysis"])
    assert "Endpoint EDR coverage" not in json.dumps(payload["generation_analysis"])


def test_meeting_generate_deep_think_source_note_template_uses_core_sections(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEETING_DEEP_THINK_MAX_SECTIONS", "6")
    get_settings.cache_clear()
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(
            generation_mode="deep_think",
            manual_notes=(
                "---\ntype: meeting\n---\n\n"
                "## Notes\nResultats Parcoursup\n- Sante\n- Droit\n\n"
                "## Resume\n-\n\n"
                "## Actions\n- Attentes des candidats\n\n"
                "## Personnes rencontrees\n- [[ ]]\n"
            ),
            transcript="Parcoursup a battu des records avec les voeux en sante et en droit.",
        ),
    )

    app.dependency_overrides.clear()
    get_settings.cache_clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["generation_analysis"]["section_titles"] == [
        "Resume detaille",
        "Sujets abordes",
        "Decisions",
        "Actions",
        "Points ouverts et incertitudes",
        "Participants et references utiles",
    ]
    assert "Section to write: Resume detaille" in fake_client.user_prompts[0]


def test_meeting_generate_deep_think_cleans_noisy_section_reports(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEETING_DEEP_THINK_MAX_SECTIONS", "1")
    get_settings.cache_clear()
    fake_client = NoisyDeepThinkClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(
            generation_mode="deep_think",
            manual_notes="## IAM\n- Identifier les responsables IAM.",
            transcript="Les responsables IAM doivent etre identifies.",
        ),
    )

    app.dependency_overrides.clear()
    get_settings.cache_clear()

    assert response.status_code == 200
    markdown = response.json()["meeting_markdown"]
    assert markdown.count("# Compte rendu") == 1
    assert "Transcription language hint" not in markdown
    assert "Aucune decision formelle" not in markdown
    assert "Information utile conservee" in markdown


def test_meeting_generate_deep_think_can_be_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEETING_DEEP_THINK_ENABLED", "false")
    get_settings.cache_clear()
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(generation_mode="deep_think"),
    )

    get_settings.cache_clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "Deep think meeting generation is disabled."}


def test_meeting_generate_cleans_repeated_transcript_lines(client: TestClient) -> None:
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(transcript="Point budget.\n\nPoint budget.\nEuh\nDecision finale."),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    prompt = fake_client.user_prompts[0]
    assert prompt.count("Point budget.") == 1
    assert "Euh" not in prompt
    assert "Decision finale." in prompt


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


def test_meeting_generate_same_as_meeting_detects_english_sources(client: TestClient) -> None:
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(
            transcript="The meeting is about zero trust pillars and questions for the AGOS team.",
            manual_notes="We need to identify people for each pillar and plan the next meeting.",
            output_language="same_as_meeting",
        ),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "written in English" in fake_client.system_prompts[0]


def test_meeting_generate_preserves_detailed_manual_note_structure(client: TestClient) -> None:
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"])

    response = client.post(
        "/v1/meetings/generate",
        headers=create_bearer_header(token),
        json=valid_payload(
            manual_notes="## Pillar IAM\n- Question one\n## Pillar Endpoint\n- EDR and hardening",
            output_language="en",
        ),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "at least as informative as the manual notes" in fake_client.user_prompts[0]
    assert "Preserve useful agenda structure, pillars, questions" in fake_client.user_prompts[0]


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
    assert response.json()["generation_analysis"] is None


def test_meeting_generate_from_job_deep_think_returns_safe_analysis(client: TestClient) -> None:
    fake_client = FakeMeetingOllamaClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_client
    token = create_token(["meetings:generate"], user_id="user-job-deep-analysis")
    job_id = create_completed_audio_job(
        user_id="user-job-deep-analysis",
        transcript_text="SECRET_TRANSCRIPT CouchDB LiveSync discussion.",
        diarization_status="failed",
    )

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(
            job_id,
            generation_mode="deep_think",
            manual_notes="## Context\nSECRET_NOTES manual decisions.",
        ),
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["generation_mode"] == "deep_think"
    assert payload["generation_stages"] == 1
    assert payload["generation_analysis"]["mode"] == "deep_think"
    assert payload["generation_analysis"]["section_titles"] == ["Context"]
    assert payload["generation_analysis"]["diarization_status"] == "failed"
    serialized_analysis = json.dumps(payload["generation_analysis"])
    assert "SECRET_TRANSCRIPT" not in serialized_analysis
    assert "SECRET_NOTES" not in serialized_analysis


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


def test_meeting_generate_from_job_rejects_empty_transcript_result(client: TestClient) -> None:
    token = create_token(["meetings:generate"], user_id="user-empty-transcript")
    job_id = create_completed_audio_job(user_id="user-empty-transcript", transcript_text="")

    response = client.post(
        "/v1/meetings/generate-from-job",
        headers=create_bearer_header(token),
        json=valid_generate_from_job_payload(job_id),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Stored transcript result is empty. The audio may contain no detectable speech."}


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
