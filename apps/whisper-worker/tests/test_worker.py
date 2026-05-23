import json
from datetime import UTC, datetime
from pathlib import Path

from whisper_worker.database import Base, create_engine_for_settings, create_session_factory
from whisper_worker.engines import FakeTranscriptionEngine
from whisper_worker.models import Job
from whisper_worker.processor import process_audio_job
from whisper_worker.repositories import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED
from whisper_worker.config import WorkerSettings


def make_settings(tmp_path) -> WorkerSettings:
    return WorkerSettings(
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        redis_url="redis://unused",
        queue_name="audio_transcription_jobs",
        audio_storage_dir=str(tmp_path / "audio"),
        transcription_mode="fake",
    )


def seed_job(session_factory, input_path: Path, job_id: str = "job-1") -> None:
    with session_factory() as session:
        job = Job(
            id=job_id,
            user_id="user-1",
            type="audio_transcription",
            status="queued",
            input_path=str(input_path),
            result_path=None,
            error=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(job)
        session.commit()


def test_fake_engine_returns_deterministic_transcript(tmp_path) -> None:
    engine = FakeTranscriptionEngine()

    result = engine.transcribe(tmp_path / "input.mp3")

    assert result.text == "Fake transcript for testing."
    assert result.language == "fr"
    assert result.duration == 0
    assert result.segments[0].text == "Fake transcript for testing."


def test_worker_processes_fake_job_successfully(tmp_path) -> None:
    settings = make_settings(tmp_path)
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    input_dir = tmp_path / "audio" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / "test.mp3"
    input_path.write_bytes(b"audio")
    seed_job(session_factory, input_path)

    process_audio_job(session_factory, FakeTranscriptionEngine(), "job-1")

    with session_factory() as session:
      job = session.get(Job, "job-1")
      assert job is not None
      assert job.status == JOB_STATUS_COMPLETED
      assert job.result_path is not None
      result = json.loads(Path(job.result_path).read_text(encoding="utf-8"))
      assert result["text"] == "Fake transcript for testing."


def test_worker_marks_job_failed_on_error(tmp_path) -> None:
    class BrokenEngine(FakeTranscriptionEngine):
        def transcribe(self, input_path: Path):  # type: ignore[override]
            raise RuntimeError("boom")

    settings = make_settings(tmp_path)
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    input_dir = tmp_path / "audio" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / "test.mp3"
    input_path.write_bytes(b"audio")
    seed_job(session_factory, input_path)

    process_audio_job(session_factory, BrokenEngine(), "job-1")

    with session_factory() as session:
      job = session.get(Job, "job-1")
      assert job is not None
      assert job.status == JOB_STATUS_FAILED
      assert job.error == "boom"
