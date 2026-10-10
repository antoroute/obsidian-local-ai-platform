"""Version 1 of the desktop relay protocol; no upstream credentials are accepted."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


GenerationKind = Literal["assistant", "note_summary", "meeting", "meeting_from_job", "vault_answer"]
RelayErrorCode = Literal[
    "auth_required", "access_denied", "rate_limited", "network_unavailable",
    "upstream_error", "invalid_response", "uncertain", "cancelled",
]


class GenerationTarget(StrictModel):
    provider: Literal["enterprise_relay"]
    model_id: str = Field(min_length=1, max_length=200)
    profile_id: Literal["headmind"]
    relay_session_id: UUID


class GenerationCreate(StrictModel):
    kind: GenerationKind
    input: dict[str, object]
    target: GenerationTarget
    idempotency_key: UUID


class GenerationResume(StrictModel):
    relay_session_id: UUID
    retry_uncertain: bool = False


class EmptyRequest(StrictModel):
    pass


class RelaySessionCreate(StrictModel):
    device_id: str = Field(min_length=1, max_length=100)
    profile_id: Literal["headmind"]
    models: list[str] = Field(min_length=1, max_length=100)


class RelayClaim(StrictModel):
    run_ids: list[UUID] = Field(min_length=1, max_length=10)
    wait_seconds: int = Field(default=25, ge=0, le=25)


class RelayStepHeartbeat(StrictModel):
    session_id: UUID
    attempt: int = Field(ge=1)


class RelayStepResult(RelayStepHeartbeat):
    model: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=500000)


class RelayStepError(RelayStepHeartbeat):
    code: RelayErrorCode
