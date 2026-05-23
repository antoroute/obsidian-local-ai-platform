import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

from whisper_worker.database import Base, create_engine_for_settings, create_session_factory
from whisper_worker.engines import FakeTranscriptionEngine, FasterWhisperEngine, create_engine
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
        transcription_engine="fake",
        whisper_model_size="medium",
        whisper_device="cpu",
        whisper_compute_type="int8",
        whisper_language=None,
        whisper_beam_size=5,
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


def test_fake_engine_remains_selectable(tmp_path) -> None:
    engine = create_engine(make_settings(tmp_path))

    assert isinstance(engine, FakeTranscriptionEngine)


def test_engine_factory_rejects_unknown_value(tmp_path) -> None:
    settings = WorkerSettings(
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        redis_url="redis://unused",
        queue_name="audio_transcription_jobs",
        audio_storage_dir=str(tmp_path / "audio"),
        transcription_engine="unknown",
        whisper_model_size="medium",
        whisper_device="cpu",
        whisper_compute_type="int8",
        whisper_language=None,
        whisper_beam_size=5,
    )

    try:
        create_engine(settings)
    except ValueError as exc:
        assert "Unsupported transcription engine" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unsupported engine")


def test_faster_whisper_engine_converts_segments(monkeypatch, tmp_path) -> None:
    class FakeWhisperModel:
        def __init__(self, model_size: str, device: str, compute_type: str) -> None:
            self.model_size = model_size
            self.device = device
            self.compute_type = compute_type

        def transcribe(self, input_path: str, language: str | None, beam_size: int):
            del input_path
            segments = [
                SimpleNamespace(start=0.0, end=1.0, text=" Bonjour"),
                SimpleNamespace(start=1.0, end=2.5, text=" le monde "),
            ]
            info = SimpleNamespace(language=language or "fr", duration=2.5)
            return segments, info

    fake_module = ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    settings = WorkerSettings(
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        redis_url="redis://unused",
        queue_name="audio_transcription_jobs",
        audio_storage_dir=str(tmp_path / "audio"),
        transcription_engine="faster_whisper",
        whisper_model_size="medium",
        whisper_device="cpu",
        whisper_compute_type="int8",
        whisper_language="fr",
        whisper_beam_size=5,
    )

    engine = create_engine(settings)
    assert isinstance(engine, FasterWhisperEngine)
    input_path = tmp_path / "input.mp3"
    input_path.write_bytes(b"audio")

    result = engine.transcribe(input_path)

    assert result.text == "Bonjour le monde"
    assert result.language == "fr"
    assert result.duration == 2.5
    assert result.segments[0].start == 0.0
    assert result.segments[1].text == "le monde"


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
