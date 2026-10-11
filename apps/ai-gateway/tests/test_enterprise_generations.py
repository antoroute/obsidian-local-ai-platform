"""API-level relay contracts: authorization, durable recovery and bounded work."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.database import get_session_factory
from app.generation_maintenance import maintain
from app.generation_routes import get_generation_embedding_client
from app.generation_store import digest
from app.main import app, get_embedding_client
from app.models import ApiToken, GenerationRun, GenerationStep, Job, RelaySession
from app.quota import QuotaUsage, get_usage_quota_limiter
from app.security import utc_now
from app.token_repository import create_api_token

USER = "relay-test-owner"
MODEL = "gpt-oss-120b"
SCOPES = ["models:list", "assistant:chat", "notes:summarize", "meetings:generate", "vault:ask", "vault:index"]


def token_for(scopes=SCOPES, user_id=USER):
    with get_session_factory()() as session:
        token = create_api_token(session, name="relay-test", scopes=list(scopes), user_id=user_id)
    return {"Authorization": f"Bearer {token.plain_token}"}


class Embeddings:
    def __init__(self):
        self.calls = []

    async def embed_text(self, text):
        self.calls.append(text)
        return SimpleNamespace(embedding=[1.0, 0.0, 0.0])


@pytest.fixture
def relay(client, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_RELAY_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_RELAY_ALLOWED_USER_IDS", f"{USER},other-user")
    monkeypatch.setenv("ENTERPRISE_RELAY_ALLOWED_MODELS", MODEL)
    monkeypatch.setenv("RAG_WORKSPACE_ID", "")
    get_settings.cache_clear()
    embeddings = Embeddings()
    app.dependency_overrides[get_generation_embedding_client] = lambda: embeddings
    app.dependency_overrides[get_embedding_client] = lambda: embeddings
    headers = token_for()
    response = client.post("/v1/relay/sessions", headers=headers, json={"device_id": "desktop-a", "profile_id": "headmind", "models": [MODEL, "not-allowed"]})
    assert response.status_code == 201, response.text
    return SimpleNamespace(client=client, headers=headers, session=response.json(), embeddings=embeddings)


def session_headers(relay, session=None):
    return {**relay.headers, "X-Relay-Secret": (session or relay.session)["session_secret"]}


def request_payload(relay, *, kind="note_summary", input=None, key=None, session=None):
    return {
        "kind": kind,
        "input": input if input is not None else {"title": "Planning", "note_content": "Decision: retain PostgreSQL.", "template": "## Decisions"},
        "target": {"provider": "enterprise_relay", "model_id": MODEL, "profile_id": "headmind", "relay_session_id": (session or relay.session)["session_id"]},
        "idempotency_key": key or str(uuid4()),
    }


def create_run(relay, **kwargs):
    response = relay.client.post("/v1/generations", headers=relay.headers, json=request_payload(relay, **kwargs))
    assert response.status_code == 202, response.text
    return response.json()


def claim_response(relay, run_ids, session=None, headers=None):
    return relay.client.post(f"/v1/relay/sessions/{(session or relay.session)['session_id']}/claim", headers=headers or session_headers(relay, session), json={"run_ids": run_ids, "wait_seconds": 0})


def claim(relay, run_id, session=None):
    response = claim_response(relay, [run_id], session)
    assert response.status_code == 200, response.text
    return response.json()["step"]


def deliver(relay, step, content="## Decisions\n\nKeep PostgreSQL.", session=None, **overrides):
    payload = {"session_id": (session or relay.session)["session_id"], "attempt": step["attempt"], "model": MODEL, "content": content, **overrides}
    return relay.client.post(f"/v1/relay/steps/{step['id']}/result", headers=session_headers(relay, session), json=payload)


def status(relay, run_id):
    response = relay.client.get(f"/v1/generations/{run_id}", headers=relay.headers)
    assert response.status_code == 200, response.text
    return response.json()


def new_session(relay):
    response = relay.client.post("/v1/relay/sessions", headers=relay.headers, json={"device_id": "desktop-b", "profile_id": "headmind", "models": [MODEL]})
    assert response.status_code == 201, response.text
    return response.json()


def test_every_relay_endpoint_requires_valid_authentication(client):
    identifier = str(uuid4())
    cases = [
        ("GET", "/v1/llm/capabilities", None),
        ("GET", "/v1/generations", None),
        ("GET", f"/v1/generations/{identifier}", None),
        ("GET", f"/v1/generations/{identifier}/result", None),
        ("POST", "/v1/generations", {}),
        ("POST", f"/v1/generations/{identifier}/cancel", {}),
        ("POST", f"/v1/generations/{identifier}/resume", {"relay_session_id": identifier}),
        ("POST", "/v1/relay/sessions", {"device_id": "a", "profile_id": "headmind", "models": [MODEL]}),
        ("POST", f"/v1/relay/sessions/{identifier}/heartbeat", {}),
        ("POST", f"/v1/relay/sessions/{identifier}/revoke", {}),
        ("POST", f"/v1/relay/sessions/{identifier}/claim", {"run_ids": [identifier], "wait_seconds": 0}),
        ("POST", f"/v1/relay/steps/{identifier}/heartbeat", {"session_id": identifier, "attempt": 1}),
        ("POST", f"/v1/relay/steps/{identifier}/result", {"session_id": identifier, "attempt": 1, "model": MODEL, "content": "ok"}),
        ("POST", f"/v1/relay/steps/{identifier}/error", {"session_id": identifier, "attempt": 1, "code": "uncertain"}),
    ]
    for method, url, body in cases:
        for headers in ({}, {"Authorization": "Bearer invalid-token"}):
            response = client.request(method, url, headers=headers, json=body)
            assert response.status_code == 401, (method, url, response.text)


def test_capabilities_and_session_secret_are_scoped(relay, monkeypatch):
    response = relay.client.get("/v1/llm/capabilities", headers=relay.headers)
    assert response.status_code == 200
    providers = {provider["id"]: provider for provider in response.json()["providers"]}
    assert providers["enterprise_relay"]["models"] == [MODEL]
    assert providers["ollama"]["generation_jobs"] is False
    with get_session_factory()() as session:
        stored = session.get(RelaySession, relay.session["session_id"])
        assert stored.secret_hash == digest(relay.session["session_secret"])
        assert relay.session["session_secret"] not in repr(vars(stored))
        assert json.loads(stored.models_json) == [MODEL]
    monkeypatch.setenv("ENTERPRISE_RELAY_ENABLED", "false")
    get_settings.cache_clear()
    response = relay.client.get("/v1/llm/capabilities", headers=relay.headers)
    assert response.json()["providers"][1]["enabled"] is False
    assert response.json()["providers"][1]["models"] == []
    assert relay.client.post("/v1/generations", headers=relay.headers, json=request_payload(relay)).status_code == 403


def test_user_allowlist_and_business_scopes_are_enforced(relay):
    outsider = token_for(user_id="not-a-pilot")
    assert relay.client.post("/v1/relay/sessions", headers=outsider, json={"device_id": "a", "profile_id": "headmind", "models": [MODEL]}).status_code == 403
    read_only = token_for(["models:list"])
    assert relay.client.post("/v1/relay/sessions", headers=read_only, json={"device_id": "a", "profile_id": "headmind", "models": [MODEL]}).status_code == 403
    run = create_run(relay)
    for method, suffix, body in [("GET", "", None), ("GET", "/result", None), ("POST", "/cancel", {}), ("POST", "/resume", {"relay_session_id": relay.session["session_id"]})]:
        assert relay.client.request(method, f"/v1/generations/{run['id']}{suffix}", headers=read_only, json=body).status_code == 403
    assert relay.client.post("/v1/generations", headers=read_only, json=request_payload(relay)).status_code == 403
    assert relay.client.get("/v1/generations", headers=read_only).json() == {"generations": []}
    assert claim_response(relay, [run["id"]], headers={**read_only, "X-Relay-Secret": relay.session["session_secret"]}).status_code == 403


def test_foreign_user_cannot_observe_or_mutate_run(relay):
    run = create_run(relay)
    foreign = token_for(user_id="other-user")
    assert relay.client.get("/v1/generations", headers=foreign).json() == {"generations": []}
    for method, suffix, body in [("GET", "", None), ("GET", "/result", None), ("POST", "/cancel", {}), ("POST", "/resume", {"relay_session_id": relay.session["session_id"]})]:
        assert relay.client.request(method, f"/v1/generations/{run['id']}{suffix}", headers=foreign, json=body).status_code == 404
    assert claim_response(relay, [run["id"]], headers={**foreign, "X-Relay-Secret": relay.session["session_secret"]}).status_code == 403
    step = claim(relay, run["id"])
    foreign_session = relay.client.post("/v1/relay/sessions", headers=foreign, json={"device_id": "c", "profile_id": "headmind", "models": [MODEL]}).json()
    response = relay.client.post(f"/v1/relay/steps/{step['id']}/result", headers={**foreign, "X-Relay-Secret": foreign_session["session_secret"]}, json={"session_id": foreign_session["session_id"], "attempt": 1, "model": MODEL, "content": "forged"})
    assert response.status_code == 404


def test_session_proof_and_binding_are_required(relay):
    run = create_run(relay)
    for supplied in (None, "wrong-proof"):
        headers = dict(relay.headers)
        if supplied:
            headers["X-Relay-Secret"] = supplied
        assert claim_response(relay, [run["id"]], headers=headers).status_code == 403
        assert relay.client.post(f"/v1/relay/sessions/{relay.session['session_id']}/heartbeat", headers=headers, json={}).status_code == 403
    other = new_session(relay)
    assert claim_response(relay, [run["id"]], other).status_code == 403
    step = claim(relay, run["id"])
    assert deliver(relay, step, session=other).status_code == 409
    assert status(relay, run["id"])["status"] == "running"


@pytest.mark.parametrize("mutation", ["forbidden_model", "mismatched_model", "upstream_url", "enterprise_token", "target_url", "empty_note"])
def test_generation_rejects_forbidden_or_unsafe_input(relay, mutation):
    payload = request_payload(relay)
    expected = 422
    if mutation == "forbidden_model":
        payload["target"]["model_id"] = "unauthorized-model"
        expected = 403
    elif mutation == "mismatched_model":
        payload["input"]["model"] = "qwen2.5:14b"
    elif mutation in {"upstream_url", "enterprise_token"}:
        payload["input"][mutation] = "private-value"
    elif mutation == "target_url":
        payload["target"]["url"] = "https://arbitrary.example"
    else:
        payload["input"]["note_content"] = ""
    response = relay.client.post("/v1/generations", headers=relay.headers, json=payload)
    assert response.status_code == expected
    assert relay.client.get("/v1/generations", headers=relay.headers).json() == {"generations": []}


def test_duplicate_request_and_result_are_idempotent_and_conflicts_fail(relay):
    payload = request_payload(relay)
    first = relay.client.post("/v1/generations", headers=relay.headers, json=payload)
    second = relay.client.post("/v1/generations", headers=relay.headers, json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    payload["input"]["note_content"] = "different request"
    assert relay.client.post("/v1/generations", headers=relay.headers, json=payload).status_code == 409
    run_id = first.json()["id"]
    assert relay.client.get(f"/v1/generations/{run_id}/result", headers=relay.headers).status_code == 409
    step = claim(relay, run_id)
    assert step["model_id"] == MODEL
    assert "PostgreSQL" in step["messages"][1]["content"]
    assert deliver(relay, step).status_code == 200
    assert deliver(relay, step).status_code == 200
    assert deliver(relay, step, "a contradictory result").status_code == 409
    assert status(relay, run_id)["progress"] == {"completed": 1, "total": 1}
    result = relay.client.get(f"/v1/generations/{run_id}/result", headers=relay.headers).json()
    assert result["summary_markdown"] == "## Decisions\n\nKeep PostgreSQL."
    assert result["title"] == "Planning"
    with get_session_factory()() as session:
        assert session.scalar(select(func.count()).select_from(GenerationRun)) == 1
        assert session.get(GenerationRun, run_id).plan_json == "{}"
        stored = session.get(GenerationStep, step["id"])
        assert stored.messages_json is None and stored.result_content is None
        assert stored.result_hash


def test_only_one_upstream_step_can_be_active_per_user(relay):
    first = create_run(relay)
    other = new_session(relay)
    second = create_run(relay, session=other)
    step = claim(relay, first["id"])
    assert step and claim(relay, first["id"]) is None
    assert claim(relay, second["id"], other) is None
    assert deliver(relay, step).status_code == 200
    assert claim(relay, second["id"], other) is not None


def test_expired_lease_requires_explicit_retry_and_fences_old_attempt(relay):
    run = create_run(relay)
    step = claim(relay, run["id"])
    with get_session_factory()() as session:
        session.get(GenerationStep, step["id"]).lease_until = utc_now() - timedelta(seconds=1)
        session.commit()
    current = status(relay, run["id"])
    assert current["status"] == "uncertain" and current["error"]["code"] == "lease_expired"
    assert claim(relay, run["id"]) is None
    other = new_session(relay)
    resume_url = f"/v1/generations/{run['id']}/resume"
    assert relay.client.post(resume_url, headers=relay.headers, json={"relay_session_id": other["session_id"]}).status_code == 409
    assert relay.client.post(resume_url, headers=relay.headers, json={"relay_session_id": other["session_id"], "retry_uncertain": True}).status_code == 200
    assert deliver(relay, step).status_code == 409
    replacement = claim(relay, run["id"], other)
    assert replacement["attempt"] == step["attempt"] + 1
    assert replacement["model_id"] == MODEL
    assert deliver(relay, replacement, session=other).status_code == 200
    assert deliver(relay, step).status_code == 409


@pytest.mark.parametrize("code,expected", [("auth_required", "paused"), ("rate_limited", "paused"), ("access_denied", "paused"), ("uncertain", "uncertain"), ("network_unavailable", "uncertain"), ("invalid_response", "uncertain")])
def test_provider_errors_never_trigger_automatic_retry(relay, code, expected):
    run = create_run(relay)
    step = claim(relay, run["id"])
    response = relay.client.post(f"/v1/relay/steps/{step['id']}/error", headers=session_headers(relay), json={"session_id": relay.session["session_id"], "attempt": 1, "code": code})
    assert response.status_code == 200
    assert status(relay, run["id"])["status"] == expected
    assert claim(relay, run["id"]) is None
    assert deliver(relay, step).status_code == 409


def test_cancel_invalidates_delivery_and_erases_prompts(relay):
    run = create_run(relay)
    step = claim(relay, run["id"])
    for _ in range(2):
        response = relay.client.post(f"/v1/generations/{run['id']}/cancel", headers=relay.headers, json={})
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert deliver(relay, step).status_code == 409
    assert relay.client.post(f"/v1/generations/{run['id']}/resume", headers=relay.headers, json={"relay_session_id": relay.session["session_id"], "retry_uncertain": True}).status_code == 409
    with get_session_factory()() as session:
        assert session.get(GenerationRun, run["id"]).plan_json == "{}"
        assert session.get(GenerationStep, step["id"]).messages_json is None


def test_heartbeat_renews_lease_and_revocation_forces_recovery(relay):
    run = create_run(relay)
    step = claim(relay, run["id"])
    shortened = utc_now() + timedelta(seconds=2)
    with get_session_factory()() as session:
        session.get(GenerationStep, step["id"]).lease_until = shortened
        session.commit()
    response = relay.client.post(f"/v1/relay/steps/{step['id']}/heartbeat", headers=session_headers(relay), json={"session_id": relay.session["session_id"], "attempt": 1})
    assert response.status_code == 200
    with get_session_factory()() as session:
        assert session.get(GenerationStep, step["id"]).lease_until.replace(tzinfo=shortened.tzinfo) > shortened
    assert relay.client.post(f"/v1/relay/sessions/{relay.session['session_id']}/revoke", headers=session_headers(relay), json={}).status_code == 200
    assert status(relay, run["id"])["status"] == "uncertain"
    assert deliver(relay, step).status_code == 403


def test_removed_model_cannot_be_claimed_or_completed(relay, monkeypatch):
    run = create_run(relay)
    step = claim(relay, run["id"])
    monkeypatch.setenv("ENTERPRISE_RELAY_ALLOWED_MODELS", "another-model")
    get_settings.cache_clear()
    assert deliver(relay, step).status_code == 403
    assert claim_response(relay, [run["id"]]).status_code == 403


def test_active_limit_releases_after_cancel_but_daily_quota_remains(relay, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_MAX_ACTIVE_GENERATIONS_PER_USER", "1")
    monkeypatch.setenv("ENTERPRISE_DAILY_GENERATIONS_PER_USER", "2")
    get_settings.cache_clear()
    first = create_run(relay)
    assert relay.client.post("/v1/generations", headers=relay.headers, json=request_payload(relay)).status_code == 429
    relay.client.post(f"/v1/generations/{first['id']}/cancel", headers=relay.headers, json={})
    second = create_run(relay)
    relay.client.post(f"/v1/generations/{second['id']}/cancel", headers=relay.headers, json={})
    response = relay.client.post("/v1/generations", headers=relay.headers, json=request_payload(relay))
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0


def test_session_capacity_and_expiration(relay):
    for _ in range(9):
        new_session(relay)
    body = {"device_id": "eleventh", "profile_id": "headmind", "models": [MODEL]}
    assert relay.client.post("/v1/relay/sessions", headers=relay.headers, json=body).status_code == 429
    with get_session_factory()() as session:
        session.get(RelaySession, relay.session["session_id"]).expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
    assert relay.client.post("/v1/generations", headers=relay.headers, json=request_payload(relay)).status_code == 403
    assert relay.client.post("/v1/relay/sessions", headers=relay.headers, json=body).status_code == 201


def test_request_prompt_and_completion_size_limits(relay, monkeypatch):
    response = relay.client.post("/v1/generations", headers={**relay.headers, "Content-Type": "application/json"}, content=iter([b" " * (1024 * 1024)] * 5))
    assert response.status_code == 413
    monkeypatch.setenv("ENTERPRISE_MAX_PROMPT_CHARS", "4000")
    monkeypatch.setenv("ENTERPRISE_MAX_RESULT_CHARS", "1000")
    get_settings.cache_clear()
    oversized = request_payload(relay, input={"note_content": "x" * 5000})
    assert relay.client.post("/v1/generations", headers=relay.headers, json=oversized).status_code == 413
    run = create_run(relay)
    step = claim(relay, run["id"])
    assert deliver(relay, step, "x" * 1001).status_code == 413
    assert deliver(relay, step, "   ").status_code == 413
    assert status(relay, run["id"])["status"] == "running"


def test_expiration_and_retention_purge_user_content(relay):
    expired = create_run(relay)
    completed = create_run(relay)
    step = claim(relay, completed["id"])
    assert deliver(relay, step).status_code == 200
    with get_session_factory()() as session:
        session.get(GenerationRun, expired["id"]).expires_at = utc_now() - timedelta(seconds=1)
        session.get(GenerationRun, completed["id"]).completed_at = utc_now() - timedelta(days=8)
        session.commit()
    maintain()
    assert status(relay, expired["id"])["status"] == "expired"
    assert relay.client.get(f"/v1/generations/{completed['id']}", headers=relay.headers).status_code == 404
    with get_session_factory()() as session:
        assert session.get(GenerationStep, step["id"]) is None
        assert session.get(GenerationRun, expired["id"]).plan_json == "{}"


def test_retries_are_bounded(relay):
    from app.generation_store import MAX_STEP_ATTEMPTS
    run = create_run(relay)
    for attempt in range(1, MAX_STEP_ATTEMPTS + 1):
        step = claim(relay, run["id"])
        assert step["attempt"] == attempt
        assert relay.client.post(f"/v1/relay/steps/{step['id']}/error", headers=session_headers(relay), json={"session_id": relay.session["session_id"], "attempt": attempt, "code": "auth_required"}).status_code == 200
        assert relay.client.post(f"/v1/generations/{run['id']}/resume", headers=relay.headers, json={"relay_session_id": relay.session["session_id"]}).status_code == 200
    assert claim(relay, run["id"]) is None
    assert status(relay, run["id"])["status"] == "failed"
    assert status(relay, run["id"])["error"]["code"] == "attempts_exhausted"


@pytest.mark.parametrize("kind,input,output", [
    ("assistant", {"message": "Summarize this", "context": "A decision was made.", "mode": "chat"}, "answer_markdown"),
    ("meeting", {"title": "Weekly sync", "transcript": "Alice decided to retain PostgreSQL.", "template": "## Decisions"}, "meeting_markdown"),
])
def test_business_workflows_preserve_response_contract(relay, kind, input, output):
    run = create_run(relay, kind=kind, input=input)
    step = claim(relay, run["id"])
    assert len(step["messages"]) == 2
    assert deliver(relay, step, "Prepared answer").status_code == 200
    result = relay.client.get(f"/v1/generations/{run['id']}/result", headers=relay.headers).json()
    assert result[output] == "Prepared answer" and result["model"] == MODEL
    assert "usage" in result


def test_meeting_job_snapshots_transcript_and_checks_owner(relay, tmp_path):
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps({"text": "Original transcript decision", "language": "fr", "segments": []}), encoding="utf-8")
    job_id = str(uuid4())
    with get_session_factory()() as session:
        session.add(Job(id=job_id, user_id=USER, type="audio_transcription", status="completed", input_path="audio.wav", result_path=str(path), created_at=utc_now(), updated_at=utc_now()))
        session.commit()
    payload = {"job_id": job_id, "title": "Recorded meeting", "template": "## Decisions"}
    run = create_run(relay, kind="meeting_from_job", input=payload)
    path.write_text(json.dumps({"text": "Changed afterwards"}), encoding="utf-8")
    step = claim(relay, run["id"])
    assert "Original transcript decision" in step["messages"][1]["content"]
    assert "Changed afterwards" not in step["messages"][1]["content"]
    assert deliver(relay, step).status_code == 200
    assert relay.client.get(f"/v1/generations/{run['id']}/result", headers=relay.headers).json()["job_id"] == job_id
    with get_session_factory()() as session:
        session.get(Job, job_id).user_id = "other-user"
        session.commit()
    response = relay.client.post("/v1/generations", headers=relay.headers, json=request_payload(relay, kind="meeting_from_job", input=payload))
    assert response.status_code == 404


def test_predigest_persists_progress_and_prepares_final_prompt(relay, monkeypatch):
    monkeypatch.setenv("MEETING_PREDIGEST_MIN_CHARS", "100")
    monkeypatch.setenv("MEETING_PREDIGEST_CHUNK_CHARS", "4000")
    get_settings.cache_clear()
    run = create_run(relay, kind="meeting", input={"title": "Long meeting", "transcript": "The PostgreSQL budget was approved. " * 230, "template": "## Decisions"})
    assert run["progress"]["total"] >= 3
    for index in range(run["progress"]["total"]):
        step = claim(relay, run["id"])
        assert step["attempt"] == 1
        if index == run["progress"]["total"] - 1:
            assert "Saved brief 1" in step["messages"][1]["content"]
            answer = "Final report"
        else:
            answer = f"Saved brief {index + 1}"
        assert deliver(relay, step, answer).status_code == 200
        assert status(relay, run["id"])["progress"]["completed"] == index + 1
    assert relay.client.get(f"/v1/generations/{run['id']}/result", headers=relay.headers).json()["meeting_markdown"] == "Final report"


def test_deep_think_assembles_sections(relay):
    run = create_run(relay, kind="meeting", input={"title": "Structured meeting", "transcript": "Budget approved. Alice delivers Friday.", "template": "## Decisions\n## Actions", "generation_mode": "deep_think"})
    assert run["progress"]["total"] >= 2
    for index in range(run["progress"]["total"]):
        assert deliver(relay, claim(relay, run["id"]), f"Useful section {index + 1}").status_code == 200
    result = relay.client.get(f"/v1/generations/{run['id']}/result", headers=relay.headers).json()
    assert result["generation_mode"] == "deep_think"
    assert result["generation_stages"] == run["progress"]["total"]
    assert "Useful section 1" in result["meeting_markdown"]
    assert "Useful section 2" in result["meeting_markdown"]


def test_rag_snapshots_sources_and_disallows_foreign_workspace(relay):
    response = relay.client.post("/v1/vault/index-note", headers=relay.headers, json={"vault_id": "default", "path": "Projects/Database.md", "title": "Database", "content": "# Database\n\nPostgreSQL is the chosen database."})
    assert response.status_code == 200, response.text
    run = create_run(relay, kind="vault_answer", input={"question": "What database was chosen?", "debug": True})
    step = claim(relay, run["id"])
    assert "PostgreSQL" in step["messages"][1]["content"]
    assert deliver(relay, step, "PostgreSQL [[Projects/Database.md]]").status_code == 200
    result = relay.client.get(f"/v1/generations/{run['id']}/result", headers=relay.headers).json()
    assert result["sources"][0]["path"] == "Projects/Database.md"
    assert result["debug_info"]["selected_sources_count"] == 1
    response = relay.client.post("/v1/generations", headers=relay.headers, json=request_payload(relay, kind="vault_answer", input={"question": "Secret?", "workspace_id": "other-user"}))
    assert response.status_code == 403


def test_rag_without_sources_finishes_without_company_call(relay):
    run = create_run(relay, kind="vault_answer", input={"question": "Unknown topic?"})
    assert run["status"] == "completed" and run["progress"]["total"] == 0
    assert claim(relay, run["id"]) is None
    result = relay.client.get(f"/v1/generations/{run['id']}/result", headers=relay.headers).json()
    assert result["sources"] == [] and result["answer_markdown"]


def test_rag_honors_existing_embedding_quota(relay, monkeypatch):
    class ExhaustedQuota:
        def __init__(self):
            self.buckets = []

        def consume(self, *, user_id, bucket, limit):
            assert user_id == USER
            self.buckets.append(bucket)
            return QuotaUsage(count=limit + 1, limit=limit, retry_after_seconds=60)

    quota = ExhaustedQuota()
    monkeypatch.setenv("USAGE_QUOTAS_ENABLED", "true")
    get_settings.cache_clear()
    app.dependency_overrides[get_usage_quota_limiter] = lambda: quota
    response = relay.client.post("/v1/generations", headers=relay.headers, json=request_payload(relay, kind="vault_answer", input={"question": "Database?"}))
    assert response.status_code == 429
    assert quota.buckets == ["embedding"] and relay.embeddings.calls == []
    assert relay.client.get("/v1/generations", headers=relay.headers).json() == {"generations": []}


@pytest.mark.parametrize("change", ["revoked", "expired"])
def test_long_poll_rechecks_token_after_releasing_transaction(relay, monkeypatch, change):
    import asyncio
    from app import generation_routes

    run = create_run(relay)
    assert deliver(relay, claim(relay, run["id"])).status_code == 200
    waits = []

    async def invalidate_between_polls(_delay):
        waits.append(True)
        with get_session_factory()() as session:
            token = session.scalar(select(ApiToken).where(ApiToken.user_id == USER))
            if change == "revoked":
                token.revoked = True
            else:
                token.expires_at = utc_now() - timedelta(seconds=1)
            session.commit()
        await asyncio.sleep(0)

    # Replace the module binding, not asyncio.sleep globally in the event loop.
    monkeypatch.setattr(generation_routes, "asyncio", SimpleNamespace(sleep=invalidate_between_polls))
    response = relay.client.post(f"/v1/relay/sessions/{relay.session['session_id']}/claim", headers=session_headers(relay), json={"run_ids": [run["id"]], "wait_seconds": 25})
    assert response.status_code == 401
    assert len(waits) == 1


def test_relay_change_preserves_completed_meeting_sections(relay):
    run = create_run(relay, kind="meeting", input={"title": "Structured meeting", "transcript": "Budget approved. Alice delivers Friday.", "template": "## Decisions\n## Actions", "generation_mode": "deep_think"})
    first = claim(relay, run["id"])
    assert deliver(relay, first, "Saved first section").status_code == 200
    other = new_session(relay)
    response = relay.client.post(f"/v1/generations/{run['id']}/resume", headers=relay.headers, json={"relay_session_id": other["session_id"]})
    assert response.status_code == 200 and response.json()["progress"]["completed"] == 1
    for _ in range(1, run["progress"]["total"]):
        step = claim(relay, run["id"], other)
        assert step["id"] != first["id"]
        assert deliver(relay, step, "Resumed section", session=other).status_code == 200
    result = relay.client.get(f"/v1/generations/{run['id']}/result", headers=relay.headers).json()
    assert "Saved first section" in result["meeting_markdown"]
    assert "Resumed section" in result["meeting_markdown"]


def test_oversized_intermediate_brief_fails_without_another_company_call(relay, monkeypatch):
    monkeypatch.setenv("MEETING_PREDIGEST_MIN_CHARS", "100")
    monkeypatch.setenv("ENTERPRISE_MAX_PROMPT_CHARS", "10000")
    get_settings.cache_clear()
    run = create_run(relay, kind="meeting", input={"title": "Bounded prompt", "transcript": "Budget confirmed. " * 30, "template": "## Decisions"})
    assert run["progress"]["total"] == 2
    assert deliver(relay, claim(relay, run["id"]), "X" * 15000).status_code == 200
    current = status(relay, run["id"])
    assert current["status"] == "failed" and current["error"]["code"] == "prompt_too_large"
    assert claim(relay, run["id"]) is None
    with get_session_factory()() as session:
        assert session.get(GenerationRun, run["id"]).plan_json == "{}"
        assert all(step.messages_json is None and step.result_content is None for step in session.scalars(select(GenerationStep).where(GenerationStep.generation_id == run["id"])))
