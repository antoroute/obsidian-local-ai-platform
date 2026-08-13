from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
            transcript = engine.transcribe(
                Path(job.input_path),
                transcription_language=get_job_transcription_language(job),
            )
            validate_transcript_has_speech(transcript.text)
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


def validate_transcript_has_speech(text: str) -> None:
    if any(character.isalnum() for character in text):
        return
    raise ValueError(
        "Whisper produced no usable speech text. Check the audio source or retry after audio normalization."
    )


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def get_job_transcription_language(job: Job) -> str:
    metadata = _decode_job_metadata(job)
    language = metadata.get("transcription_language")
    if language in {"auto", "fr", "en"}:
        return str(language)
    return "auto"


def _decode_job_metadata(job: Job) -> dict[str, Any]:
    if not job.metadata_json:
        return {}
    try:
        decoded = json.loads(job.metadata_json)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
