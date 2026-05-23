from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import sessionmaker, Session

from whisper_worker.engines import TranscriptionEngine
from whisper_worker.models import Job
from whisper_worker.repositories import get_job, mark_job_completed, mark_job_failed, mark_job_processing


def process_audio_job(session_factory: sessionmaker[Session], engine: TranscriptionEngine, job_id: str) -> None:
    with session_factory() as session:
        job = get_job(session, job_id)
        if job is None:
            return

        now = _utc_now()
        try:
            mark_job_processing(session, job, now)
            transcript = engine.transcribe(Path(job.input_path))
            result_path = write_result_file(job, transcript.to_dict())
            mark_job_completed(session, job, result_path, _utc_now())
        except Exception as exc:
            mark_job_failed(session, job, str(exc), _utc_now())


def write_result_file(job: Job, transcript: dict[str, object]) -> Path:
    base_dir = Path(job.input_path).resolve().parents[1]
    result_dir = base_dir / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / f"{job.id}.json"
    result_path.write_text(json.dumps(transcript), encoding="utf-8")
    return result_path


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
