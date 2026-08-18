from __future__ import annotations

import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import time
from typing import Any

from sqlalchemy.orm import sessionmaker, Session

from whisper_worker.engines import TranscriptionCancelled, TranscriptionEngine
from whisper_worker.diarization_client import DiarizationClient, apply_speaker_turns, mark_diarization_failed
from whisper_worker.models import Job
from whisper_worker.repositories import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    get_job,
    job_cancellation_requested,
    mark_job_cancelled,
    mark_job_completed,
    mark_job_failed,
    mark_job_processing,
    update_job_progress,
)

logger = logging.getLogger(__name__)


def process_audio_job(
    session_factory: sessionmaker[Session],
    engine: TranscriptionEngine,
    job_id: str,
    *,
    progress_min_interval_seconds: float = 2.0,
    diarization_client: DiarizationClient | None = None,
) -> None:
    with session_factory() as session:
        job = get_job(session, job_id)
        if job is None:
            return
        if job.status in {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED}:
            return
        if job.status == JOB_STATUS_CANCELLED:
            cleanup_input_file(job)
            return
        if job_cancellation_requested(session, job):
            mark_job_cancelled(session, job, _utc_now())
            cleanup_input_file(job)
            return

        now = _utc_now()
        last_progress_at = 0.0

        def cancellation_check() -> bool:
            return job_cancellation_requested(session, job)

        def progress_callback(phase: str, progress: int, message: str) -> None:
            nonlocal last_progress_at
            if cancellation_check():
                raise TranscriptionCancelled("Transcription cancelled by the user.")
            monotonic_now = time.monotonic()
            if progress < 90 and monotonic_now - last_progress_at < progress_min_interval_seconds:
                return
            update_job_progress(
                session,
                job,
                phase=phase,
                progress=progress,
                message=message,
                now=_utc_now(),
            )
            last_progress_at = monotonic_now

        try:
            mark_job_processing(session, job, now)
            transcript = engine.transcribe(
                Path(job.input_path),
                transcription_language=get_job_transcription_language(job),
                progress_callback=progress_callback,
                cancellation_check=cancellation_check,
            )
            if cancellation_check():
                raise TranscriptionCancelled("Transcription cancelled by the user.")
            validate_transcript_has_speech(transcript.text)
            if get_job_diarization_enabled(job):
                update_job_progress(
                    session,
                    job,
                    phase="diarizing",
                    progress=92,
                    message="Identifying anonymous speakers on the GPU.",
                    now=_utc_now(),
                )
                try:
                    if diarization_client is None:
                        raise RuntimeError("GPU diarization service is not configured.")
                    turns = diarize_with_heartbeat(
                        diarization_client,
                        Path(job.input_path),
                        session=session,
                        job=job,
                    )
                    transcript = apply_speaker_turns(transcript, turns)
                except Exception as exc:
                    logger.warning("Diarization failed for job %s: %s", job.id, exc)
                    transcript = mark_diarization_failed(transcript)
                if cancellation_check():
                    raise TranscriptionCancelled("Transcription cancelled by the user.")
            update_job_progress(
                session,
                job,
                phase="saving",
                progress=95,
                message="Saving the transcript.",
                now=_utc_now(),
            )
            result_path = write_result_file(job, transcript.to_dict())
            mark_job_completed(session, job, result_path, _utc_now())
            cleanup_input_file(job)
        except TranscriptionCancelled:
            mark_job_cancelled(session, job, _utc_now())
            cleanup_input_file(job)
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


def cleanup_input_file(job: Job) -> None:
    input_path = Path(job.input_path)
    try:
        input_path.unlink(missing_ok=True)
    except OSError:
        # Cleanup must never turn a successful transcription into a failed job.
        pass


def diarize_with_heartbeat(
    client: DiarizationClient,
    input_path: Path,
    *,
    session: Session,
    job: Job,
) -> list:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diarization-request")
    future = executor.submit(client.diarize, input_path)
    try:
        while True:
            try:
                return future.result(timeout=5)
            except FutureTimeoutError:
                update_job_progress(
                    session,
                    job,
                    phase="diarizing",
                    progress=92,
                    message="Identifying anonymous speakers on the GPU.",
                    now=_utc_now(),
                )
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def get_job_transcription_language(job: Job) -> str:
    metadata = _decode_job_metadata(job)
    language = metadata.get("transcription_language")
    if language in {"auto", "fr", "en"}:
        return str(language)
    return "auto"


def get_job_diarization_enabled(job: Job) -> bool:
    return _decode_job_metadata(job).get("diarization_enabled") is True


def _decode_job_metadata(job: Job) -> dict[str, Any]:
    if not job.metadata_json:
        return {}
    try:
        decoded = json.loads(job.metadata_json)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
