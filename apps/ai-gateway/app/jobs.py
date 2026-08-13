from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Job
from app.security import utc_now

JOB_TYPE_AUDIO_TRANSCRIPTION = "audio_transcription"
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"


def encode_job_metadata(metadata: dict[str, object] | None) -> str | None:
    if not metadata:
        return None
    return json.dumps(metadata, separators=(",", ":"), sort_keys=True)


def decode_job_metadata(job: Job) -> dict[str, object]:
    if not job.metadata_json:
        return {}
    try:
        decoded = json.loads(job.metadata_json)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def get_job_transcription_language(job: Job) -> str:
    metadata = decode_job_metadata(job)
    language = metadata.get("transcription_language")
    if language in {"auto", "fr", "en"}:
        return str(language)
    return "auto"


def create_audio_transcription_job(
    session: Session,
    *,
    user_id: str,
    input_path: str,
    metadata: dict[str, object] | None = None,
) -> Job:
    now = utc_now()
    job = Job(
        id=str(uuid.uuid4()),
        user_id=user_id,
        type=JOB_TYPE_AUDIO_TRANSCRIPTION,
        status=JOB_STATUS_QUEUED,
        input_path=input_path,
        result_path=None,
        error=None,
        metadata_json=encode_job_metadata(metadata),
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def ensure_audio_job_capacity(session: Session, *, user_id: str, max_active_jobs: int) -> None:
    active_jobs = int(
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.user_id == user_id,
                Job.type == JOB_TYPE_AUDIO_TRANSCRIPTION,
                Job.status.in_([JOB_STATUS_QUEUED, JOB_STATUS_PROCESSING]),
            )
        )
        or 0
    )
    if active_jobs >= max_active_jobs:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many active audio transcription jobs.",
            headers={"Retry-After": "30"},
        )


def get_job_for_user(session: Session, *, job_id: str, user_id: str) -> Job | None:
    statement = select(Job).where(Job.id == job_id, Job.user_id == user_id)
    return session.scalar(statement)


def require_job_for_user(session: Session, *, job_id: str, user_id: str) -> Job:
    job = get_job_for_user(session, job_id=job_id, user_id=user_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


def read_transcript_result(job: Job) -> dict[str, object]:
    if not job.result_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job result is not available.")

    result_path = Path(job.result_path)
    if not result_path.exists():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job result is not available.")

    return json.loads(result_path.read_text(encoding="utf-8"))
