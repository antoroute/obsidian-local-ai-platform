from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job
from app.security import utc_now

JOB_TYPE_AUDIO_TRANSCRIPTION = "audio_transcription"
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"


def create_audio_transcription_job(session: Session, *, user_id: str, input_path: str) -> Job:
    now = utc_now()
    job = Job(
        id=str(uuid.uuid4()),
        user_id=user_id,
        type=JOB_TYPE_AUDIO_TRANSCRIPTION,
        status=JOB_STATUS_QUEUED,
        input_path=input_path,
        result_path=None,
        error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


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
