"""Real PostgreSQL races. Set TEST_POSTGRES_URL to an expendable test database.

Each test owns a fresh schema, so it neither reads nor changes application data.
The API uses independent SQLAlchemy sessions and multiple request threads.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.config import get_settings
from app.database import Base, get_db_session
from app import generation_routes
from app.generation_routes import router
from app.generation_store import lock_user
from app.models import ApiToken, GenerationRun, GenerationStep, GenerationUserState, RelaySession
from app.token_repository import create_api_token

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
MODEL = "gpt-oss-120b"
USER = "postgres-relay-test"


@pytest.fixture
def pg_relay(monkeypatch):
    assert POSTGRES_URL and POSTGRES_URL.startswith("postgresql"), "Concurrency tests require PostgreSQL"
    monkeypatch.setenv("ENTERPRISE_RELAY_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_RELAY_ALLOWED_USER_IDS", USER)
    monkeypatch.setenv("ENTERPRISE_RELAY_ALLOWED_MODELS", MODEL)
    monkeypatch.setenv("USAGE_QUOTAS_ENABLED", "false")
    get_settings.cache_clear()
    schema = "relay_test_" + uuid4().hex
    # A regression must fail with a useful database exception rather than wait
    # indefinitely for a transaction whose finalizer cannot reach the event loop.
    connect_args = {"connect_timeout": 5, "options": "-c lock_timeout=10000 -c statement_timeout=20000"}
    admin_engine = create_engine(POSTGRES_URL, connect_args=connect_args)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(POSTGRES_URL, connect_args=connect_args, execution_options={"schema_translate_map": {None: schema}})
    tables = [model.__table__ for model in (ApiToken, GenerationUserState, RelaySession, GenerationRun, GenerationStep)]
    Base.metadata.create_all(engine, tables=tables)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    application = FastAPI()
    application.include_router(router)

    @application.get("/v1/health")
    async def event_loop_probe():
        return {"status": "ok"}

    def database():
        with sessions() as session:
            yield session

    application.dependency_overrides[get_db_session] = database
    with sessions() as session:
        created = create_api_token(session, name="postgres-test", scopes=["notes:summarize"], user_id=USER)
    headers = {"Authorization": f"Bearer {created.plain_token}"}
    try:
        with TestClient(application) as client:
            response = client.post("/v1/relay/sessions", headers=headers, json={"device_id": "postgres-test", "profile_id": "headmind", "models": [MODEL]})
            assert response.status_code == 201, response.text
            relay = response.json()
            yield SimpleNamespace(client=client, sessions=sessions, headers=headers, relay=relay, proof={**headers, "X-Relay-Secret": relay["session_secret"]})
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()
        get_settings.cache_clear()


def payload(pg_relay, key=None):
    return {"kind": "note_summary", "input": {"note_content": "A durable decision."}, "target": {"provider": "enterprise_relay", "profile_id": "headmind", "model_id": MODEL, "relay_session_id": pg_relay.relay["session_id"]}, "idempotency_key": key or str(uuid4())}


def parallel(count, action):
    barrier = Barrier(count)

    def synchronized(index):
        barrier.wait(timeout=10)
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(synchronized, range(count)))


def test_postgres_concurrent_idempotent_create_uses_one_run_and_one_quota(pg_relay, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_MAX_ACTIVE_GENERATIONS_PER_USER", "1")
    monkeypatch.setenv("ENTERPRISE_DAILY_GENERATIONS_PER_USER", "1")
    get_settings.cache_clear()
    body = payload(pg_relay)
    responses = parallel(4, lambda _: pg_relay.client.post("/v1/generations", headers=pg_relay.headers, json=body))
    assert [response.status_code for response in responses] == [202] * 4
    assert len({response.json()["id"] for response in responses}) == 1
    with pg_relay.sessions() as session:
        assert session.scalar(select(func.count()).select_from(GenerationRun)) == 1
        assert session.scalar(select(func.count()).select_from(GenerationStep)) == 1


def test_postgres_concurrent_distinct_creates_cannot_exceed_active_limit(pg_relay, monkeypatch):
    monkeypatch.setenv("ENTERPRISE_MAX_ACTIVE_GENERATIONS_PER_USER", "1")
    get_settings.cache_clear()
    responses = parallel(4, lambda _: pg_relay.client.post("/v1/generations", headers=pg_relay.headers, json=payload(pg_relay)))
    assert sorted(response.status_code for response in responses) == [202, 429, 429, 429]
    with pg_relay.sessions() as session:
        assert session.scalar(select(func.count()).select_from(GenerationRun)) == 1
    # A rejected request must roll its mutex back immediately. Repeat contention
    # after capacity is exhausted to exercise the exception path, not just the
    # successful first commit.
    for _ in range(3):
        rejected = parallel(4, lambda _: pg_relay.client.post("/v1/generations", headers=pg_relay.headers, json=payload(pg_relay)))
        assert [response.status_code for response in rejected] == [429] * 4
    assert pg_relay.client.get("/v1/health").json() == {"status": "ok"}


@pytest.mark.parametrize("operation", ["create", "claim"])
def test_postgres_waiting_for_user_mutex_keeps_event_loop_responsive(pg_relay, monkeypatch, operation):
    """Reproduce loop starvation deterministically, without depending on races.

    A separate connection owns the user mutex. The request under test reaches
    its blocking lock, then an async health request must finish while that lock
    remains held. The finally clause always releases the external lock before
    waiting for either request thread, including when testing the broken code.
    """
    response = pg_relay.client.post("/v1/generations", headers=pg_relay.headers, json=payload(pg_relay))
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    reached_mutex = Event()
    original_lock_user = generation_routes.lock_user

    def observed_lock_user(session, user_id):
        reached_mutex.set()
        return original_lock_user(session, user_id)

    monkeypatch.setattr(generation_routes, "lock_user", observed_lock_user)

    def blocked_request():
        if operation == "create":
            return pg_relay.client.post("/v1/generations", headers=pg_relay.headers, json=payload(pg_relay))
        return pg_relay.client.post(f"/v1/relay/sessions/{pg_relay.relay['session_id']}/claim", headers=pg_relay.proof, json={"run_ids": [run_id], "wait_seconds": 0})

    with pg_relay.sessions() as owner:
        lock_user(owner, USER)
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(blocked_request)
            try:
                assert reached_mutex.wait(timeout=5), "Request never reached the user mutex"
                probe = executor.submit(pg_relay.client.get, "/v1/health")
                response = probe.result(timeout=2)
                assert response.status_code == 200 and response.json() == {"status": "ok"}
                assert not pending.done(), "The request must still wait for the held database mutex"
            finally:
                owner.rollback()
            response = pending.result(timeout=10)
            assert response.status_code == (202 if operation == "create" else 200), response.text


def test_postgres_concurrent_claim_and_duplicate_delivery_are_serialized(pg_relay):
    response = pg_relay.client.post("/v1/generations", headers=pg_relay.headers, json=payload(pg_relay))
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    claim_url = f"/v1/relay/sessions/{pg_relay.relay['session_id']}/claim"
    responses = parallel(4, lambda _: pg_relay.client.post(claim_url, headers=pg_relay.proof, json={"run_ids": [run_id], "wait_seconds": 0}))
    assert all(response.status_code == 200 for response in responses)
    steps = [response.json()["step"] for response in responses if response.json()["step"]]
    assert len(steps) == 1
    step = steps[0]
    assert step["attempt"] == 1
    result = {"session_id": pg_relay.relay["session_id"], "attempt": 1, "model": MODEL, "content": "The accepted decision."}
    results = parallel(4, lambda _: pg_relay.client.post(f"/v1/relay/steps/{step['id']}/result", headers=pg_relay.proof, json=result))
    assert all(response.status_code == 200 for response in results)
    status = pg_relay.client.get(f"/v1/generations/{run_id}", headers=pg_relay.headers).json()
    assert status["status"] == "completed" and status["progress"] == {"completed": 1, "total": 1}


def test_postgres_cancel_and_delivery_race_has_one_consistent_outcome(pg_relay):
    response = pg_relay.client.post("/v1/generations", headers=pg_relay.headers, json=payload(pg_relay))
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    claimed = pg_relay.client.post(f"/v1/relay/sessions/{pg_relay.relay['session_id']}/claim", headers=pg_relay.proof, json={"run_ids": [run_id], "wait_seconds": 0}).json()["step"]
    result = {"session_id": pg_relay.relay["session_id"], "attempt": 1, "model": MODEL, "content": "The accepted decision."}

    def action(index):
        if index == 0:
            return pg_relay.client.post(f"/v1/generations/{run_id}/cancel", headers=pg_relay.headers, json={})
        return pg_relay.client.post(f"/v1/relay/steps/{claimed['id']}/result", headers=pg_relay.proof, json=result)

    cancelled, delivered = parallel(2, action)
    assert cancelled.status_code == 200
    assert delivered.status_code in (200, 409)
    current = pg_relay.client.get(f"/v1/generations/{run_id}", headers=pg_relay.headers).json()
    output = pg_relay.client.get(f"/v1/generations/{run_id}/result", headers=pg_relay.headers)
    if delivered.status_code == 200:
        assert current["status"] == "completed" and output.status_code == 200
    else:
        assert current["status"] == "cancelled" and output.status_code == 409
