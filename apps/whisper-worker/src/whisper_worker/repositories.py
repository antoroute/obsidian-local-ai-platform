from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from whisper_worker.models import Job


JOB_STATUS_QUEUED = "queued"
JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"


@dataclass(frozen=True)
class TranscriptWord:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[TranscriptWord] = field(default_factory=list)


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    language: str
    duration: float
    segments: list[TranscriptSegment]
    diarization_enabled: bool = False
    diarization_status: str = "disabled"

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "language": self.language,
            "duration": self.duration,
            "diarization_enabled": self.diarization_enabled,
            "diarization_status": self.diarization_status,
            "segments": [
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    **({"speaker": segment.speaker} if segment.speaker else {}),
                }
                for segment in self.segments
            ],
        }


def get_job(session: Session, job_id: str) -> Job | None:
    return session.get(Job, job_id)


def mark_job_processing(session: Session, job: Job, now) -> None:
    job.status = JOB_STATUS_PROCESSING
    job.phase = "preparing"
    job.progress = max(job.progress, 5)
    job.progress_message = "Preparing the audio for transcription."
    job.attempts += 1
    job.started_at = job.started_at or now
    job.heartbeat_at = now
    job.updated_at = now
    job.error = None
    session.commit()


def mark_job_completed(session: Session, job: Job, result_path: Path, now) -> None:
    job.status = JOB_STATUS_COMPLETED
    job.phase = "completed"
    job.progress = 100
    job.progress_message = "Transcription completed."
    job.result_path = str(result_path)
    job.heartbeat_at = now
    job.completed_at = now
    job.updated_at = now
    job.error = None
    session.commit()


def mark_job_failed(session: Session, job: Job, error_message: str, now) -> None:
    job.status = JOB_STATUS_FAILED
    job.phase = "failed"
    job.progress_message = "Transcription failed."
    job.heartbeat_at = now
    job.completed_at = now
    job.updated_at = now
    job.error = error_message
    session.commit()


def mark_job_cancelled(session: Session, job: Job, now) -> None:
    job.status = JOB_STATUS_CANCELLED
    job.phase = "cancelled"
    job.progress_message = "Transcription cancelled."
    job.heartbeat_at = now
    job.completed_at = now
    job.updated_at = now
    job.error = None
    session.commit()


def update_job_progress(
    session: Session,
    job: Job,
    *,
    phase: str,
    progress: int,
    message: str,
    now,
) -> None:
    job.phase = phase
    job.progress = max(job.progress, min(99, max(0, progress)))
    job.progress_message = message
    job.heartbeat_at = now
    job.updated_at = now
    session.commit()


def job_cancellation_requested(session: Session, job: Job) -> bool:
    session.refresh(job, attribute_names=["status", "cancel_requested_at"])
    return job.status == JOB_STATUS_CANCELLED or job.cancel_requested_at is not None


def recover_processing_jobs(session: Session, *, max_attempts: int, now) -> list[str]:
    jobs = list(session.scalars(select(Job).where(Job.status == JOB_STATUS_PROCESSING)))
    recovered_job_ids: list[str] = []
    for job in jobs:
        if job.cancel_requested_at is not None:
            job.status = JOB_STATUS_CANCELLED
            job.phase = "cancelled"
            job.progress_message = "Transcription cancelled during worker restart."
            job.completed_at = now
            job.heartbeat_at = now
            job.error = None
        elif job.attempts >= max_attempts:
            job.status = JOB_STATUS_FAILED
            job.phase = "failed"
            job.progress_message = "Transcription failed after repeated worker restarts."
            job.completed_at = now
            job.heartbeat_at = now
            job.error = "Maximum transcription attempts reached after worker restart."
        else:
            job.status = JOB_STATUS_QUEUED
            job.phase = "queued"
            job.progress_message = "Worker restarted; transcription queued for automatic retry."
            job.heartbeat_at = None
            job.error = None
            recovered_job_ids.append(job.id)
        job.updated_at = now
    if jobs:
        session.commit()
    return recovered_job_ids
