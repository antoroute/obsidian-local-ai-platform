from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from whisper_worker.models import Job


JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None


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
    job.updated_at = now
    job.error = None
    session.commit()


def mark_job_completed(session: Session, job: Job, result_path: Path, now) -> None:
    job.status = JOB_STATUS_COMPLETED
    job.result_path = str(result_path)
    job.updated_at = now
    job.error = None
    session.commit()


def mark_job_failed(session: Session, job: Job, error_message: str, now) -> None:
    job.status = JOB_STATUS_FAILED
    job.updated_at = now
    job.error = error_message
    session.commit()
