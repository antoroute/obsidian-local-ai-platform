import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

from whisper_worker.database import Base, create_engine_for_settings, create_session_factory
from whisper_worker.diarization import SpeakerTurn, apply_diarization_to_segments
from whisper_worker.engines import (
    DiarizingTranscriptionEngine,
    FakeTranscriptionEngine,
    FasterWhisperEngine,
    check_engine,
    create_engine,
    normalize_faster_whisper_error,
)
from whisper_worker.models import Job
from whisper_worker.processor import process_audio_job
from whisper_worker.repositories import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED
from whisper_worker.config import WorkerSettings
from whisper_worker.repositories import TranscriptSegment


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
        whisper_model_cache_dir=str(tmp_path / "model-cache"),
        hf_home=str(tmp_path / "model-cache"),
        huggingface_hub_cache=str(tmp_path / "model-cache" / "hub"),
    )


def seed_job(session_factory, input_path: Path, job_id: str = "job-1", metadata: dict[str, object] | None = None) -> None:
    with session_factory() as session:
        job = Job(
            id=job_id,
            user_id="user-1",
            type="audio_transcription",
            status="queued",
            input_path=str(input_path),
            result_path=None,
            error=None,
            metadata_json=json.dumps(metadata) if metadata else None,
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


def test_fake_engine_respects_french_transcription_language(tmp_path) -> None:
    engine = FakeTranscriptionEngine()

    result = engine.transcribe(tmp_path / "input.mp3", transcription_language="fr")

    assert result.language == "fr"


def test_fake_engine_respects_english_transcription_language(tmp_path) -> None:
    engine = FakeTranscriptionEngine()

    result = engine.transcribe(tmp_path / "input.mp3", transcription_language="en")

    assert result.language == "en"


def test_transcript_result_serializes_optional_speaker(tmp_path) -> None:
    engine = FakeTranscriptionEngine()

    result = engine.transcribe(tmp_path / "input.mp3")
    segment = result.segments[0]
    enriched = type(result)(
        text=result.text,
        language=result.language,
        duration=result.duration,
        segments=[TranscriptSegment(start=segment.start, end=segment.end, text=segment.text, speaker="Speaker 1")],
        diarization_enabled=True,
        diarization_status="completed",
    )

    payload = enriched.to_dict()

    assert payload["diarization_enabled"] is True
    assert payload["diarization_status"] == "completed"
    assert payload["segments"][0]["speaker"] == "Speaker 1"  # type: ignore[index]


def test_diarization_assigns_speaker_by_overlap() -> None:
    segments = [
        TranscriptSegment(start=0.0, end=2.0, text="Hello"),
        TranscriptSegment(start=2.0, end=4.0, text="Bonjour"),
    ]
    turns = [
        SpeakerTurn(start=0.0, end=1.8, speaker="Speaker 1"),
        SpeakerTurn(start=2.1, end=3.8, speaker="Speaker 2"),
    ]

    enriched = apply_diarization_to_segments(segments, turns)

    assert enriched[0].speaker == "Speaker 1"
    assert enriched[1].speaker == "Speaker 2"


def test_fake_engine_remains_selectable(tmp_path) -> None:
    engine = create_engine(make_settings(tmp_path))

    assert isinstance(engine, FakeTranscriptionEngine)


def test_engine_factory_wraps_diarization_when_enabled(tmp_path) -> None:
    settings = make_settings(tmp_path)
    settings = WorkerSettings(
        **{
            **settings.__dict__,
            "diarization_enabled": True,
        }
    )

    engine = create_engine(settings)

    assert isinstance(engine, DiarizingTranscriptionEngine)


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
        whisper_model_cache_dir=str(tmp_path / "model-cache"),
        hf_home=str(tmp_path / "model-cache"),
        huggingface_hub_cache=str(tmp_path / "model-cache" / "hub"),
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
            self.language = language
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
        whisper_model_cache_dir=str(tmp_path / "model-cache"),
        hf_home=str(tmp_path / "model-cache"),
        huggingface_hub_cache=str(tmp_path / "model-cache" / "hub"),
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


def test_faster_whisper_engine_uses_requested_french_language(tmp_path) -> None:
    class FakeWhisperModel:
        def __init__(self) -> None:
            self.language: str | None = "unset"

        def transcribe(self, input_path: str, language: str | None, beam_size: int):
            del input_path, beam_size
            self.language = language
            return [SimpleNamespace(start=0.0, end=1.0, text="Bonjour")], SimpleNamespace(language=language, duration=1.0)

    model = FakeWhisperModel()
    input_path = tmp_path / "input.mp3"
    input_path.write_bytes(b"audio")

    FasterWhisperEngine(model, default_language=None, beam_size=5).transcribe(input_path, transcription_language="fr")

    assert model.language == "fr"


def test_faster_whisper_engine_uses_requested_english_language(tmp_path) -> None:
    class FakeWhisperModel:
        def __init__(self) -> None:
            self.language: str | None = "unset"

        def transcribe(self, input_path: str, language: str | None, beam_size: int):
            del input_path, beam_size
            self.language = language
            return [SimpleNamespace(start=0.0, end=1.0, text="Hello")], SimpleNamespace(language=language, duration=1.0)

    model = FakeWhisperModel()
    input_path = tmp_path / "input.mp3"
    input_path.write_bytes(b"audio")

    FasterWhisperEngine(model, default_language=None, beam_size=5).transcribe(input_path, transcription_language="en")

    assert model.language == "en"


def test_faster_whisper_engine_does_not_force_language_in_auto_mode(tmp_path) -> None:
    class FakeWhisperModel:
        def __init__(self) -> None:
            self.language: str | None = "unset"

        def transcribe(self, input_path: str, language: str | None, beam_size: int):
            del input_path, beam_size
            self.language = language
            return [SimpleNamespace(start=0.0, end=1.0, text="Bonjour")], SimpleNamespace(language="fr", duration=1.0)

    model = FakeWhisperModel()
    input_path = tmp_path / "input.mp3"
    input_path.write_bytes(b"audio")

    FasterWhisperEngine(model, default_language="en", beam_size=5).transcribe(input_path, transcription_language="auto")

    assert model.language is None


def test_worker_processes_fake_job_successfully(tmp_path) -> None:
    settings = make_settings(tmp_path)
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    input_dir = tmp_path / "audio" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / "test.mp3"
    input_path.write_bytes(b"audio")
    seed_job(session_factory, input_path, metadata={"transcription_language": "en"})

    process_audio_job(session_factory, FakeTranscriptionEngine(), "job-1")

    with session_factory() as session:
      job = session.get(Job, "job-1")
      assert job is not None
      assert job.status == JOB_STATUS_COMPLETED
      assert job.result_path is not None
      result = json.loads(Path(job.result_path).read_text(encoding="utf-8"))
      assert result["text"] == "Fake transcript for testing."
      assert result["language"] == "en"


def test_worker_marks_job_failed_on_error(tmp_path) -> None:
    class BrokenEngine(FakeTranscriptionEngine):
        def transcribe(self, input_path: Path, *, transcription_language: str | None = None):  # type: ignore[override]
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


def test_check_engine_reports_success_for_fake_engine(tmp_path) -> None:
    result = check_engine(make_settings(tmp_path))

    assert result.engine_name == "fake"
    assert "loaded successfully" in result.message


def test_normalize_faster_whisper_error_reports_missing_model() -> None:
    error = RuntimeError("huggingface_hub.errors.LocalEntryNotFoundError: model missing")

    normalized = normalize_faster_whisper_error(error)

    assert "Whisper model is not available locally." in str(normalized)
