"""Authenticated, additive enterprise generation API.

This router never connects to the enterprise endpoint. The desktop supplies
only completion text; URLs, headers, tools and enterprise tokens are excluded.
"""
import asyncio
import json
import secrets
import time
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.auth import get_current_token, require_scope
from app.config import get_settings
from app.database import get_db_session
from app.generation_plans import SCOPES, prepare_plan
from app.generation_schemas import (
    EmptyRequest, GenerationCreate, GenerationResume, RelayClaim,
    RelaySessionCreate, RelayStepError, RelayStepHeartbeat, RelayStepResult,
)
from app.generation_store import (
    TERMINAL, advance, claim, create_run, digest, dump, enabled_for,
    erase_intermediate, lock_user, reconcile_user, refresh_run, require_enabled,
    require_run, require_scope_for, require_session, require_step,
    status_view, steps_for, validate_model,
)
from app.models import ApiToken, GenerationRun, RelaySession
from app.quota import UsageQuotaLimiter, get_usage_quota_limiter
from app.security import is_expired, utc_now
from app.services.ollama_client import OllamaResponseError, OllamaUnavailableError
from app.token_repository import token_has_scope

router = APIRouter(prefix="/v1", tags=["generations"])
Token = Annotated[ApiToken, Depends(get_current_token)]
Database = Annotated[Session, Depends(get_db_session)]
Secret = Annotated[str | None, Header(alias="X-Relay-Secret", max_length=200)]


class GenerationBodyLimitMiddleware(BaseHTTPMiddleware):
    """Bound request bodies before JSON parsing, including chunked requests."""
    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith(("/v1/generations", "/v1/relay/")):
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    return JSONResponse({"detail": "Generation request exceeds the size limit."}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        return await call_next(request)


def get_generation_embedding_client():
    # Resolve at call time to share the existing Ollama embedding semaphore.
    from app.main import get_embedding_client
    return get_embedding_client()


@router.get("/llm/capabilities")
def capabilities(token: Annotated[ApiToken, Depends(require_scope("models:list"))]) -> dict[str, Any]:
    settings = get_settings()
    enabled = enabled_for(token.user_id, settings)
    return {"protocol_version": 1, "enterprise_origin": "https://ai.headmind.com", "providers": [
        {"id": "ollama", "enabled": True, "models": settings.allowed_models, "generation_jobs": False},
        {"id": "enterprise_relay", "enabled": enabled, "models": settings.enterprise_relay_allowed_models if enabled else [], "generation_jobs": enabled},
    ]}


def existing_request(session: Session, user_id: str, key: str, request_hash: str) -> GenerationRun | None:
    run = session.scalar(select(GenerationRun).where(GenerationRun.user_id == user_id, GenerationRun.idempotency_key == key))
    if run and run.request_hash != request_hash:
        raise HTTPException(409, "Idempotency key was already used for a different request.")
    return run


@router.post("/generations", status_code=202)
async def submit_generation(payload: GenerationCreate, token: Token, session: Database,
                            limiter: Annotated[UsageQuotaLimiter, Depends(get_usage_quota_limiter)],
                            embedding_client: Annotated[Any, Depends(get_generation_embedding_client)]) -> dict[str, Any]:
    settings, user_id = get_settings(), token.user_id
    require_enabled(user_id, settings)
    require_scope_for(token, payload.kind)
    fingerprint = digest(dump(payload.model_dump(mode="json")))

    def preflight():
        try:
            existing = existing_request(session, user_id, str(payload.idempotency_key), fingerprint)
            if existing:
                view = status_view(session, existing)
            else:
                relay = require_session(session, str(payload.target.relay_session_id), user_id, proof=False)
                validate_model(payload.target.model_id, relay, settings)
                view = None
            session.commit()
            return view
        except BaseException:
            session.rollback()
            raise

    existing_view = await run_in_threadpool(preflight)
    if existing_view is not None:
        return existing_view
    if payload.kind == "vault_answer" and settings.usage_quotas_enabled:
        try:
            usage = await run_in_threadpool(limiter.consume, user_id=user_id, bucket="embedding", limit=settings.daily_embedding_requests_per_user)
        except RedisError as exc:
            raise HTTPException(503, "Usage quota service is unavailable.") from exc
        if usage.exceeded:
            raise HTTPException(429, "Daily embedding quota exceeded.", headers={"Retry-After": str(usage.retry_after_seconds)})
    try:
        plan = await prepare_plan(payload.kind, payload.input, payload.target.model_id, user_id, session, settings, embedding_client)
    except OllamaUnavailableError as exc:
        raise HTTPException(503, "Local embedding service is unavailable.") from exc
    except OllamaResponseError as exc:
        raise HTTPException(502, "Local embedding service returned an invalid response.") from exc
    def persist() -> dict[str, Any]:
        # Database lock waits must never block the ASGI loop: another request
        # may need that loop to finish and release its transaction.
        session.commit()  # all network preparation ends before the user mutex
        try:
            lock_user(session, user_id)
            existing = existing_request(session, user_id, str(payload.idempotency_key), fingerprint)
            if existing:
                view = status_view(session, existing)
            else:
                require_enabled(user_id, settings)
                relay = require_session(session, str(payload.target.relay_session_id), user_id, proof=False)
                validate_model(payload.target.model_id, relay, settings)
                reconcile_user(session, user_id, settings)
                session.flush()
                run = create_run(session, user_id=user_id, payload=payload, request_hash=fingerprint, plan=plan, settings=settings)
                session.flush()
                view = status_view(session, run)
            session.commit()
            return view
        except BaseException:
            session.rollback()
            raise

    return await run_in_threadpool(persist)


@router.get("/generations")
def list_generations(token: Token, session: Database) -> dict[str, Any]:
    lock_user(session, token.user_id)
    reconcile_user(session, token.user_id, get_settings())
    runs = session.scalars(select(GenerationRun).where(GenerationRun.user_id == token.user_id).order_by(GenerationRun.created_at.desc()).limit(100))
    views = [status_view(session, run) for run in runs if token_has_scope(token, SCOPES[run.kind])]
    session.commit()
    return {"generations": views}


@router.get("/generations/{run_id}")
def generation_status(run_id: UUID, token: Token, session: Database) -> dict[str, Any]:
    lock_user(session, token.user_id)
    run = require_run(session, str(run_id), token)
    refresh_run(session, run, get_settings())
    view = status_view(session, run)
    session.commit()
    return view


@router.get("/generations/{run_id}/result")
def generation_result(run_id: UUID, token: Token, session: Database) -> dict[str, Any]:
    run = require_run(session, str(run_id), token)
    if run.status != "completed" or run.result_json is None:
        raise HTTPException(409, "Generation result is not available.")
    return json.loads(run.result_json)


@router.post("/generations/{run_id}/cancel")
def cancel_generation(run_id: UUID, payload: EmptyRequest, token: Token, session: Database) -> dict[str, Any]:
    lock_user(session, token.user_id)
    run = require_run(session, str(run_id), token)
    if run.status not in TERMINAL:
        run.status, run.error_code = "cancelled", "cancelled"
        run.completed_at = run.updated_at = utc_now()
        steps = steps_for(session, run)
        for step in steps:
            if step.status != "completed":
                step.status, step.lease_until = "cancelled", None
        erase_intermediate(run, steps)
    view = status_view(session, run)
    session.commit()
    return view


@router.post("/generations/{run_id}/resume")
def resume_generation(run_id: UUID, payload: GenerationResume, token: Token, session: Database) -> dict[str, Any]:
    settings = get_settings()
    require_enabled(token.user_id, settings)
    lock_user(session, token.user_id)
    run = require_run(session, str(run_id), token)
    refresh_run(session, run, settings)
    if run.status in TERMINAL:
        raise HTTPException(409, "A terminal generation cannot be resumed.")
    relay = require_session(session, str(payload.relay_session_id), token.user_id, proof=False)
    validate_model(run.model_id, relay, settings)
    steps = steps_for(session, run)
    uncertain = any(step.status in {"running", "uncertain"} for step in steps)
    if uncertain and not payload.retry_uncertain:
        raise HTTPException(409, "The previous call may have executed; retry_uncertain must be explicit.")
    for step in steps:
        if step.status != "completed":
            # Fencing happens immediately, even before another claim.
            step.status, step.lease_until, step.relay_session_id = "pending", None, None
            step.error_code = None
    run.relay_session_id = relay.id
    run.status, run.error_code, run.updated_at = "waiting_relay", None, utc_now()
    view = status_view(session, run)
    session.commit()
    return view


@router.post("/relay/sessions", status_code=201)
def create_relay_session(payload: RelaySessionCreate, token: Token, session: Database) -> dict[str, Any]:
    settings, now = get_settings(), utc_now()
    require_enabled(token.user_id, settings)
    if not any(token_has_scope(token, scope) for scope in SCOPES.values()):
        raise HTTPException(403, "No generation operation is authorized.")
    if any(not model or len(model) > 200 for model in payload.models):
        raise HTTPException(422, "Invalid model identifier.")
    models = sorted(set(payload.models) & set(settings.enterprise_relay_allowed_models))
    if not models:
        raise HTTPException(403, "No enterprise model is authorized.")
    lock_user(session, token.user_id)
    active_sessions = session.scalar(select(func.count()).select_from(RelaySession).where(RelaySession.user_id == token.user_id, RelaySession.expires_at > now))
    if active_sessions >= 10:
        raise HTTPException(429, "Too many active relay sessions; revoke an existing session first.")
    secret = secrets.token_urlsafe(32)
    relay = RelaySession(id=str(uuid4()), user_id=token.user_id, device_id=payload.device_id,
        profile_id="headmind", secret_hash=digest(secret), models_json=dump(models),
        created_at=now, heartbeat_at=now, expires_at=now + timedelta(seconds=settings.enterprise_relay_session_ttl_seconds))
    session.add(relay)
    response = {"session_id": relay.id, "session_secret": secret, "expires_at": relay.expires_at.isoformat()}
    session.commit()
    return response


@router.post("/relay/sessions/{session_id}/heartbeat")
def relay_heartbeat(session_id: UUID, payload: EmptyRequest, token: Token, session: Database, secret: Secret = None) -> dict[str, bool]:
    require_enabled(token.user_id, get_settings())
    lock_user(session, token.user_id)
    relay = require_session(session, str(session_id), token.user_id, secret)
    relay.heartbeat_at = utc_now()
    session.commit()
    return {"ok": True}


@router.post("/relay/sessions/{session_id}/revoke")
def revoke_relay(session_id: UUID, payload: EmptyRequest, token: Token, session: Database, secret: Secret = None) -> dict[str, bool]:
    lock_user(session, token.user_id)
    relay = require_session(session, str(session_id), token.user_id, secret)
    relay.expires_at = utc_now()
    for run in session.scalars(select(GenerationRun).where(GenerationRun.user_id == token.user_id, GenerationRun.relay_session_id == relay.id, GenerationRun.status.not_in(TERMINAL))):
        for step in steps_for(session, run):
            if step.status == "running":
                step.status, step.lease_until, step.error_code = "uncertain", None, "uncertain"
                run.status, run.error_code, run.updated_at = "uncertain", "uncertain", utc_now()
    session.commit()
    return {"ok": True}


@router.post("/relay/sessions/{session_id}/claim")
async def claim_step(session_id: UUID, payload: RelayClaim, token: Token, session: Database, secret: Secret = None) -> dict[str, Any]:
    settings, user_id = get_settings(), token.user_id
    require_enabled(user_id, settings)
    deadline = time.monotonic() + payload.wait_seconds

    def poll_once():
        try:
            lock_user(session, user_id)
            # Long polling may outlive revocation/expiry of the gateway credential.
            if token.revoked or is_expired(token.expires_at):
                raise HTTPException(401, "Authentication credentials are no longer valid.")
            relay = require_session(session, str(session_id), user_id, secret)
            reconcile_user(session, user_id, settings)
            relay.heartbeat_at = utc_now()
            step = claim(session, relay=relay, run_ids=[str(item) for item in payload.run_ids], token=token, settings=settings)
            session.commit()  # no transaction or connection held over the wait
            return step
        except BaseException:
            session.rollback()
            raise

    while True:
        step = await run_in_threadpool(poll_once)
        if step is not None or time.monotonic() >= deadline:
            return {"step": step}
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def prepare_step_update(session: Session, token: ApiToken, payload: RelayStepHeartbeat, step_id: UUID, secret: str | None):
    settings = get_settings()
    require_enabled(token.user_id, settings)
    lock_user(session, token.user_id)
    relay = require_session(session, str(payload.session_id), token.user_id, secret)
    run, step = require_step(session, str(step_id), token, relay, payload.attempt, settings)
    validate_model(run.model_id, relay, settings)
    return run, step, settings


@router.post("/relay/steps/{step_id}/heartbeat")
def step_heartbeat(step_id: UUID, payload: RelayStepHeartbeat, token: Token, session: Database, secret: Secret = None) -> dict[str, bool]:
    run, step, settings = prepare_step_update(session, token, payload, step_id, secret)
    if step.status == "running":
        step.lease_until = utc_now() + timedelta(seconds=settings.enterprise_relay_lease_seconds)
        run.updated_at = utc_now()
    session.commit()
    return {"ok": True}


@router.post("/relay/steps/{step_id}/result")
def submit_result(step_id: UUID, payload: RelayStepResult, token: Token, session: Database, secret: Secret = None) -> dict[str, bool]:
    run, step, settings = prepare_step_update(session, token, payload, step_id, secret)
    if not payload.content.strip() or len(payload.content) > settings.enterprise_max_result_chars:
        raise HTTPException(413, "Enterprise completion is empty or exceeds the output limit.")
    result_hash = digest(dump({"model": payload.model, "content": payload.content}))
    if step.status == "completed":
        if step.result_hash != result_hash:
            raise HTTPException(409, "A different result was already accepted for this step.")
    else:
        step.result_content, step.result_model, step.result_hash = payload.content, payload.model, result_hash
        step.status, step.lease_until = "completed", None
        session.flush()
        advance(session, run, settings)
    session.commit()
    return {"ok": True}


@router.post("/relay/steps/{step_id}/error")
def submit_error(step_id: UUID, payload: RelayStepError, token: Token, session: Database, secret: Secret = None) -> dict[str, bool]:
    run, step, settings = prepare_step_update(session, token, payload, step_id, secret)
    if step.status == "completed":
        raise HTTPException(409, "A result was already accepted for this step.")
    # Only explicit provider rejections can establish that no usable call ran.
    uncertain = payload.code not in {"auth_required", "access_denied", "rate_limited"}
    step.status = "uncertain" if uncertain else "paused"
    step.error_code, step.lease_until = payload.code, None
    run.status, run.error_code, run.updated_at = step.status, payload.code, utc_now()
    session.commit()
    return {"ok": True}
