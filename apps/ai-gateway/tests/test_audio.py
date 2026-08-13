import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import get_session_factory
from app.jobs import JOB_STATUS_COMPLETED, JOB_STATUS_QUEUED
from app.models import Job
from app.config import get_settings
from app.main import app
from app.quota import QuotaUsage, UsageQuotaLimiter, get_usage_quota_limiter
from app.token_repository import create_api_token


def create_bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_token(scopes: list[str], user_id: str | None = None) -> str:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="audio-token", scopes=scopes, user_id=user_id)
    return created_token.plain_token


def upload_audio(
    client: TestClient,
    token: str,
    filename: str = "sample.mp3",
    content: bytes = b"audio",
    transcription_language: str | None = None,
) -> object:
    data = {} if transcription_language is None else {"transcription_language": transcription_language}
    return client.post(
        "/v1/audio/transcribe",
        headers=create_bearer_header(token),
        data=data,
        files={"file": (filename, content, "audio/mpeg")},
    )


def test_audio_transcribe_requires_token(client: TestClient) -> None:
    response = client.post("/v1/audio/transcribe", files={"file": ("sample.mp3", b"audio", "audio/mpeg")})

    assert response.status_code == 401


def test_audio_transcribe_preflight_options_returns_cors_headers(client: TestClient) -> None:
    response = client.options(
        "/v1/audio/transcribe",
        headers={
            "Origin": "app://obsidian.md",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code in {200, 204}
    assert response.headers["access-control-allow-origin"] == "*"
    assert "POST" in response.headers["access-control-allow-methods"]


def test_vault_delete_preflight_is_allowed_for_obsidian(client: TestClient) -> None:
    response = client.options(
        "/v1/vault/index",
        headers={
            "Origin": "app://obsidian.md",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code in {200, 204}
    assert response.headers["access-control-allow-origin"] == "*"
    assert "DELETE" in response.headers["access-control-allow-methods"]


def test_audio_transcribe_rejects_missing_scope(client: TestClient) -> None:
    token = create_token(["notes:summarize"])
    response = upload_audio(client, token)

    assert response.status_code == 403


def test_audio_transcribe_rejects_unsupported_extension(client: TestClient) -> None:
    token = create_token(["audio:transcribe"])
    response = upload_audio(client, token, filename="sample.txt")

    assert response.status_code == 422


def test_audio_transcribe_queues_job(client: TestClient) -> None:
    token = create_token(["audio:transcribe"])
    response = upload_audio(client, token, filename="meeting.mp3", content=b"fake-audio")

    assert response.status_code in {200, 202}
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["job_id"]
    assert client.fake_audio_queue.job_ids == [payload["job_id"]]
    assert client.fake_audio_queue.messages[0]["transcription_language"] == "auto"


def test_audio_transcribe_rejects_second_active_job(client: TestClient) -> None:
    token = create_token(["audio:transcribe"], user_id="single-job-user")

    first = upload_audio(client, token, filename="first.mp3")
    second = upload_audio(client, token, filename="second.mp3")

    assert first.status_code in {200, 202}
    assert second.status_code == 429
    assert second.json() == {"detail": "Too many active audio transcription jobs."}
    assert second.headers["retry-after"] == "30"


class ExceededAudioQuota(UsageQuotaLimiter):
    def consume(self, *, user_id: str, bucket: str, limit: int) -> QuotaUsage:
        del user_id, bucket, limit
        return QuotaUsage(count=2, limit=1, retry_after_seconds=60)


def test_audio_transcribe_enforces_daily_user_quota(
    client: TestClient,
    monkeypatch,
) -> None:
    token = create_token(["audio:transcribe"], user_id="quota-user")
    monkeypatch.setenv("USAGE_QUOTAS_ENABLED", "true")
    monkeypatch.setenv("DAILY_AUDIO_JOBS_PER_USER", "1")
    get_settings.cache_clear()
    app.dependency_overrides[get_usage_quota_limiter] = lambda: ExceededAudioQuota()

    response = upload_audio(client, token)

    assert response.status_code == 429
    assert response.json() == {"detail": "Daily audio quota exceeded."}
    assert response.headers["retry-after"] == "60"


def test_audio_transcribe_neutralizes_path_traversal_filename(client: TestClient) -> None:
    token = create_token(["audio:transcribe"])
    response = upload_audio(client, token, filename="../../evil.mp3", content=b"fake-audio")

    assert response.status_code in {200, 202}
    job_id = response.json()["job_id"]
    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert ".." not in job.input_path


def test_audio_transcribe_defaults_transcription_language_to_auto(client: TestClient) -> None:
    token = create_token(["audio:transcribe"])
    response = upload_audio(client, token)

    assert response.status_code in {200, 202}
    job_id = response.json()["job_id"]
    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        metadata = json.loads(job.metadata_json or "{}")
        assert metadata["transcription_language"] == "auto"
    assert client.fake_audio_queue.messages[0]["transcription_language"] == "auto"


def test_audio_transcribe_accepts_french_transcription_language(client: TestClient) -> None:
    token = create_token(["audio:transcribe"])
    response = upload_audio(client, token, transcription_language="fr")

    assert response.status_code in {200, 202}
    job_id = response.json()["job_id"]
    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        metadata = json.loads(job.metadata_json or "{}")
        assert metadata["transcription_language"] == "fr"
    assert client.fake_audio_queue.messages[0]["transcription_language"] == "fr"


def test_audio_transcribe_accepts_english_transcription_language(client: TestClient) -> None:
    token = create_token(["audio:transcribe"])
    response = upload_audio(client, token, transcription_language="en")

    assert response.status_code in {200, 202}
    job_id = response.json()["job_id"]
    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        metadata = json.loads(job.metadata_json or "{}")
        assert metadata["transcription_language"] == "en"
    assert client.fake_audio_queue.messages[0]["transcription_language"] == "en"


def test_audio_transcribe_rejects_invalid_transcription_language(client: TestClient) -> None:
    token = create_token(["audio:transcribe"])
    response = upload_audio(client, token, transcription_language="de")

    assert response.status_code == 422


def test_audio_transcribe_rejects_too_large_file(client: TestClient) -> None:
    token = create_token(["audio:transcribe"])
    too_big = b"a" * (1024 * 1024 + 1)
    response = upload_audio(client, token, filename="meeting.mp3", content=too_big)

    assert response.status_code == 413


def test_get_job_returns_owner_job(client: TestClient) -> None:
    token = create_token(["audio:transcribe"], user_id="user-1")
    response = upload_audio(client, token)
    job_id = response.json()["job_id"]

    status_response = client.get(f"/v1/jobs/{job_id}", headers=create_bearer_header(token))

    assert status_response.status_code == 200
    assert status_response.json()["status"] == "queued"


def test_get_job_result_before_completion_returns_conflict(client: TestClient) -> None:
    token = create_token(["audio:transcribe"], user_id="user-1")
    response = upload_audio(client, token)
    job_id = response.json()["job_id"]

    result_response = client.get(f"/v1/jobs/{job_id}/result", headers=create_bearer_header(token))

    assert result_response.status_code == 409


def test_other_user_cannot_access_job(client: TestClient) -> None:
    owner_token = create_token(["audio:transcribe"], user_id="owner")
    other_token = create_token(["audio:transcribe"], user_id="other")
    response = upload_audio(client, owner_token)
    job_id = response.json()["job_id"]

    status_response = client.get(f"/v1/jobs/{job_id}", headers=create_bearer_header(other_token))

    assert status_response.status_code == 404


def test_completed_job_result_is_returned(client: TestClient) -> None:
    token = create_token(["audio:transcribe"], user_id="user-1")
    response = upload_audio(client, token)
    job_id = response.json()["job_id"]
    result_path = Path(client.audio_storage_dir) / "results" / f"{job_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "text": "Fake transcript for testing.",
                "language": "fr",
                "duration": 0,
                "diarization_enabled": True,
                "diarization_status": "completed",
                "segments": [{"start": 0, "end": 1, "text": "Fake transcript for testing.", "speaker": "Speaker 1"}],
            }
        ),
        encoding="utf-8",
    )

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = JOB_STATUS_COMPLETED
        job.result_path = str(result_path)
        session.commit()

    result_response = client.get(f"/v1/jobs/{job_id}/result", headers=create_bearer_header(token))

    assert result_response.status_code == 200
    assert result_response.json()["transcript"]["text"] == "Fake transcript for testing."
    assert result_response.json()["transcript"]["diarization_status"] == "completed"
    assert result_response.json()["transcript"]["segments"][0]["speaker"] == "Speaker 1"
