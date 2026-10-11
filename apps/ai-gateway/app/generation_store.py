"""Short SQL transactions for durable relay ownership, fencing and recovery.

Every mutating operation holds the same per-user database mutex. This works
across gateway processes and covers sessions, run capacity, quotas and claims.
No network calls or long polls may execute while this mutex is held.
"""
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.generation_plans import SCOPES, finish_plan, next_messages
from app.models import ApiToken, GenerationRun, GenerationStep, GenerationUserState, RelaySession
from app.security import utc_now
from app.token_repository import token_has_scope

TERMINAL = {"completed", "cancelled", "expired", "failed"}
MAX_STEP_ATTEMPTS = 5
ERROR_MESSAGES = {
    "auth_required": "Renew the enterprise token, then resume.",
    "access_denied": "Enterprise access was denied.",
    "rate_limited": "The enterprise service is temporarily rate limited.",
    "network_unavailable": "The enterprise connection is unavailable.",
    "upstream_error": "The enterprise service returned an error.",
    "invalid_response": "The enterprise response was invalid or incomplete.",
    "uncertain": "The previous call may have executed; an explicit retry is required.",
    "lease_expired": "The relay stopped responding; an explicit retry is required.",
    "generation_expired": "The generation expired.",
    "prompt_too_large": "An intermediate prompt exceeded the configured input limit.",
    "cancelled": "The generation was cancelled.",
    "attempts_exhausted": "The maximum number of attempts for this step was reached.",
}


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def enabled_for(user_id: str, settings: Settings) -> bool:
    return settings.enterprise_relay_enabled and user_id in settings.enterprise_relay_allowed_user_ids


def require_enabled(user_id: str, settings: Settings) -> None:
    if not enabled_for(user_id, settings):
        raise HTTPException(403, "Enterprise relay is not enabled for this user.")


def require_scope_for(token: ApiToken, kind: str) -> None:
    if not token_has_scope(token, SCOPES[kind]):
        raise HTTPException(403, "The generation operation is not authorized.")


def lock_user(session: Session, user_id: str) -> None:
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif session.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Enterprise generations require PostgreSQL or SQLite.")
    session.execute(insert(GenerationUserState).values(user_id=user_id, revision=0).on_conflict_do_nothing(index_elements=["user_id"]))
    session.execute(update(GenerationUserState).where(GenerationUserState.user_id == user_id).values(revision=GenerationUserState.revision + 1))
    # A Session may have read a row before waiting for the database mutex.
    session.expire_all()


def require_run(session: Session, run_id: str, token: ApiToken) -> GenerationRun:
    run = session.scalar(select(GenerationRun).where(GenerationRun.id == run_id, GenerationRun.user_id == token.user_id))
    if run is None:
        raise HTTPException(404, "Generation not found.")
    require_scope_for(token, run.kind)
    return run


def require_session(session: Session, session_id: str, user_id: str, secret: str | None = None, *, proof: bool = True) -> RelaySession:
    relay = session.scalar(select(RelaySession).where(RelaySession.id == session_id, RelaySession.user_id == user_id))
    if relay is None or aware(relay.expires_at) <= utc_now():
        raise HTTPException(403, "Relay session is invalid or expired.")
    if proof and (not secret or not secrets.compare_digest(relay.secret_hash, digest(secret))):
        raise HTTPException(403, "Relay session proof is invalid.")
    return relay


def validate_model(model: str, relay: RelaySession, settings: Settings) -> None:
    if model not in settings.enterprise_relay_allowed_models or model not in json.loads(relay.models_json):
        raise HTTPException(403, "Requested enterprise model is not allowed or available to the relay.")


def steps_for(session: Session, run: GenerationRun) -> list[GenerationStep]:
    return list(session.scalars(select(GenerationStep).where(GenerationStep.generation_id == run.id).order_by(GenerationStep.position)))


def erase_intermediate(run: GenerationRun, steps: list[GenerationStep]) -> None:
    run.plan_json = "{}"
    for step in steps:
        step.messages_json = None
        step.result_content = None


def refresh_run(session: Session, run: GenerationRun, settings: Settings, now: datetime | None = None) -> None:
    now = now or utc_now()
    if run.status in TERMINAL:
        return
    steps = steps_for(session, run)
    if aware(run.expires_at) <= now:
        run.status, run.error_code, run.completed_at = "expired", "generation_expired", now
        run.updated_at = now
        for step in steps:
            if step.status != "completed":
                step.status, step.lease_until = "cancelled", None
        erase_intermediate(run, steps)
    else:
        for step in steps:
            if step.status == "running" and step.lease_until and aware(step.lease_until) <= now:
                step.status, step.error_code, step.lease_until = "uncertain", "lease_expired", None
                run.status, run.error_code, run.updated_at = "uncertain", "lease_expired", now


def status_view(session: Session, run: GenerationRun) -> dict[str, Any]:
    steps = steps_for(session, run)
    return {
        "id": run.id, "kind": run.kind, "status": run.status,
        "provider": run.provider, "model_id": run.model_id,
        "progress": {"completed": sum(step.status == "completed" for step in steps), "total": len(steps)},
        "error": {"code": run.error_code, "message": ERROR_MESSAGES.get(run.error_code, "Generation unavailable.")} if run.error_code else None,
        "created_at": aware(run.created_at).isoformat(), "updated_at": aware(run.updated_at).isoformat(),
    }


def advance(session: Session, run: GenerationRun, settings: Settings) -> None:
    steps = steps_for(session, run)
    plan = json.loads(run.plan_json)
    completed = [step for step in steps if step.status == "completed"]
    if len(completed) == len(steps):
        result = finish_plan(plan, [step.result_content for step in completed], [step.result_model for step in completed])
        run.result_json, run.status = dump(result), "completed"
        run.completed_at = run.updated_at = utc_now()
        erase_intermediate(run, steps)
        return
    pending = next(step for step in steps if step.status != "completed")
    if pending.messages_json is None:
        try:
            pending.messages_json = dump(next_messages(plan, [step.result_content for step in completed], settings))
        except HTTPException:
            run.status, run.error_code = "failed", "prompt_too_large"
            run.completed_at = run.updated_at = utc_now()
            erase_intermediate(run, steps)
            return
    run.status, run.error_code, run.updated_at = "waiting_relay", None, utc_now()


def create_run(session: Session, *, user_id: str, payload: Any, request_hash: str, plan: dict[str, Any], settings: Settings) -> GenerationRun:
    now = utc_now()
    active = session.scalar(select(func.count()).select_from(GenerationRun).where(GenerationRun.user_id == user_id, GenerationRun.status.not_in(TERMINAL)))
    if active >= settings.enterprise_max_active_generations_per_user:
        raise HTTPException(429, "Active enterprise generation limit reached.")
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    used = session.scalar(select(func.count()).select_from(GenerationRun).where(GenerationRun.user_id == user_id, GenerationRun.created_at >= day_start))
    if used >= settings.enterprise_daily_generations_per_user:
        raise HTTPException(429, "Daily enterprise generation quota exceeded.", headers={"Retry-After": str(max(1, int(((day_start + timedelta(days=1)) - now).total_seconds())))})
    run = GenerationRun(id=str(uuid4()), user_id=user_id, idempotency_key=str(payload.idempotency_key), request_hash=request_hash,
        kind=payload.kind, provider="enterprise_relay", model_id=payload.target.model_id, profile_id="headmind",
        relay_session_id=str(payload.target.relay_session_id), status="waiting_relay", plan_json=dump(plan),
        created_at=now, updated_at=now, expires_at=now + timedelta(seconds=settings.enterprise_generation_ttl_seconds))
    session.add(run)
    session.flush()
    for index, prepared_messages in enumerate(plan["steps"]):
        session.add(GenerationStep(id=str(uuid4()), generation_id=run.id, position=index, status="pending", attempt=0,
            messages_json=dump(prepared_messages) if prepared_messages is not None else None))
    session.flush()
    if not plan["steps"]:
        advance(session, run, settings)
    return run


def claim(session: Session, *, relay: RelaySession, run_ids: list[str], token: ApiToken, settings: Settings) -> dict[str, Any] | None:
    runs = [require_run(session, run_id, token) for run_id in run_ids]
    for run in runs:
        if run.relay_session_id != relay.id:
            raise HTTPException(403, "Generation is bound to another relay session.")
        validate_model(run.model_id, relay, settings)
        refresh_run(session, run, settings)
    session.flush()
    # One active upstream call per user, even across devices/sessions.
    busy = session.scalar(select(GenerationStep.id).join(GenerationRun).where(GenerationRun.user_id == token.user_id, GenerationStep.status == "running").limit(1))
    if busy:
        return None
    for run in runs:
        if run.status != "waiting_relay":
            continue
        step = next((item for item in steps_for(session, run) if item.status != "completed"), None)
        if step is None or step.status != "pending":
            continue
        if step.attempt >= MAX_STEP_ATTEMPTS:
            run.status, run.error_code = "failed", "attempts_exhausted"
            run.completed_at = run.updated_at = utc_now()
            erase_intermediate(run, steps_for(session, run))
            continue
        step.status, step.attempt, step.relay_session_id = "running", step.attempt + 1, relay.id
        step.lease_until = utc_now() + timedelta(seconds=settings.enterprise_relay_lease_seconds)
        run.status, run.updated_at = "running", utc_now()
        return {"id": step.id, "generation_id": run.id, "attempt": step.attempt, "model_id": run.model_id, "messages": json.loads(step.messages_json)}
    return None


def require_step(session: Session, step_id: str, token: ApiToken, relay: RelaySession, attempt: int, settings: Settings) -> tuple[GenerationRun, GenerationStep]:
    step = session.get(GenerationStep, step_id)
    if step is None:
        raise HTTPException(404, "Relay step not found.")
    run = require_run(session, step.generation_id, token)
    if run.relay_session_id != relay.id or step.relay_session_id != relay.id or step.attempt != attempt:
        raise HTTPException(409, "Relay attempt has been superseded.")
    refresh_run(session, run, settings)
    if run.status in {"cancelled", "expired", "failed"} or step.status not in {"running", "completed"}:
        raise HTTPException(409, "Relay attempt is no longer active.")
    return run, step


def reconcile_user(session: Session, user_id: str, settings: Settings) -> None:
    for run in session.scalars(select(GenerationRun).where(GenerationRun.user_id == user_id, GenerationRun.status.not_in(TERMINAL))):
        refresh_run(session, run, settings)


def purge_user(session: Session, user_id: str, settings: Settings) -> None:
    cutoff = utc_now() - timedelta(days=settings.enterprise_generation_retention_days)
    ids = list(session.scalars(select(GenerationRun.id).where(GenerationRun.user_id == user_id, GenerationRun.status.in_(TERMINAL), GenerationRun.completed_at < cutoff)))
    if ids:
        session.execute(delete(GenerationStep).where(GenerationStep.generation_id.in_(ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.id.in_(ids)))
    session.execute(delete(RelaySession).where(RelaySession.user_id == user_id, RelaySession.expires_at < cutoff))
