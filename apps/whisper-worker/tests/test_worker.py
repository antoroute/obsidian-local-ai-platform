import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

from whisper_worker.database import Base, create_engine_for_settings, create_session_factory
from whisper_worker.engines import (
    FakeTranscriptionEngine,
    FasterWhisperEngine,
    TranscriptionCancelled,
    check_engine,
    create_engine,
    normalize_faster_whisper_error,
)
from whisper_worker.diarization_client import SpeakerTurn, apply_speaker_turns
from whisper_worker.models import Job
from whisper_worker.processor import process_audio_job
from whisper_worker.processor import validate_transcript_has_speech
from whisper_worker.repositories import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PROCESSING,
    recover_processing_jobs,
    TranscriptResult,
    TranscriptSegment,
    TranscriptWord,
)
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


def test_transcript_without_speech_is_rejected() -> None:
    try:
        validate_transcript_has_speech(". . ...")
    except ValueError as exc:
        assert "no usable speech text" in str(exc)
    else:
        raise AssertionError("Expected punctuation-only transcript to be rejected")


def test_transcript_with_speech_is_accepted() -> None:
    validate_transcript_has_speech("Bonjour.")


def test_transcript_result_keeps_legacy_diarization_fields_disabled(tmp_path) -> None:
    engine = FakeTranscriptionEngine()

    result = engine.transcribe(tmp_path / "input.mp3")
    payload = result.to_dict()

    assert payload["diarization_enabled"] is False
    assert payload["diarization_status"] == "disabled"
    assert "speaker" not in payload["segments"][0]  # type: ignore[operator]


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
        def __init__(
            self,
            model_size: str,
            device: str,
            compute_type: str,
            cpu_threads: int,
            num_workers: int,
            download_root: str,
        ) -> None:
            self.model_size = model_size
            self.device = device
            self.compute_type = compute_type
            self.cpu_threads = cpu_threads
            self.num_workers = num_workers
            self.download_root = download_root

        def transcribe(
            self,
            input_path: str,
            language: str | None,
            beam_size: int,
            vad_filter: bool,
            condition_on_previous_text: bool,
            word_timestamps: bool,
        ):
            del input_path
            self.language = language
            self.vad_filter = vad_filter
            self.condition_on_previous_text = condition_on_previous_text
            self.word_timestamps = word_timestamps
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
    assert engine._model.vad_filter is True
    assert engine._model.condition_on_previous_text is False
    assert engine._model.word_timestamps is True
    assert engine._model.cpu_threads == 0
    assert engine._model.num_workers == 1
    assert engine._model.download_root == str(tmp_path / "model-cache")


def test_faster_whisper_consumes_lazy_segments_before_normalized_audio_is_deleted(monkeypatch, tmp_path) -> None:
    normalized_path = tmp_path / "normalized.wav"

    @contextmanager
    def fake_normalize_audio(input_path: Path):
        del input_path
        normalized_path.write_bytes(b"wav")
        try:
            yield normalized_path
        finally:
            normalized_path.unlink()

    class LazyWhisperModel:
        def transcribe(self, input_path: str, **kwargs):
            del kwargs

            def segments():
                assert Path(input_path).exists()
                yield SimpleNamespace(start=0.0, end=1.0, text=" Bonjour")

            return segments(), SimpleNamespace(language="fr", duration=1.0)

    monkeypatch.setattr("whisper_worker.engines.normalize_audio_for_whisper", fake_normalize_audio)
    input_path = tmp_path / "input.webm"
    input_path.write_bytes(b"audio")

    result = FasterWhisperEngine(LazyWhisperModel(), default_language=None, beam_size=5).transcribe(input_path)

    assert result.text == "Bonjour"
    assert normalized_path.exists() is False


def test_faster_whisper_engine_uses_requested_french_language(tmp_path) -> None:
    class FakeWhisperModel:
        def __init__(self) -> None:
            self.language: str | None = "unset"

        def transcribe(self, input_path: str, language: str | None, beam_size: int, **kwargs):
            del input_path, beam_size, kwargs
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

        def transcribe(self, input_path: str, language: str | None, beam_size: int, **kwargs):
            del input_path, beam_size, kwargs
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

        def transcribe(self, input_path: str, language: str | None, beam_size: int, **kwargs):
            del input_path, beam_size, kwargs
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
      assert job.phase == "completed"
      assert job.progress == 100
      assert job.attempts == 1
      assert job.heartbeat_at is not None
      assert job.completed_at is not None
      assert input_path.exists() is False


def test_worker_marks_job_failed_on_error(tmp_path) -> None:
    class BrokenEngine(FakeTranscriptionEngine):
        def transcribe(self, input_path: Path, **kwargs):  # type: ignore[override]
            del input_path, kwargs
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


def test_worker_honours_cancellation_and_removes_input(tmp_path) -> None:
    class CancelledEngine(FakeTranscriptionEngine):
        def transcribe(self, input_path: Path, **kwargs):  # type: ignore[override]
            del input_path, kwargs
            raise TranscriptionCancelled("cancelled")

    settings = make_settings(tmp_path)
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    input_dir = tmp_path / "audio" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / "test.mp3"
    input_path.write_bytes(b"audio")
    seed_job(session_factory, input_path)

    process_audio_job(session_factory, CancelledEngine(), "job-1")

    with session_factory() as session:
        job = session.get(Job, "job-1")
        assert job is not None
        assert job.status == JOB_STATUS_CANCELLED
        assert job.completed_at is not None
    assert input_path.exists() is False


def test_word_level_diarization_splits_segment_on_speaker_change() -> None:
    transcript = TranscriptResult(
        text="Bonjour comment allez-vous",
        language="fr",
        duration=2.0,
        segments=[
            TranscriptSegment(
                start=0.0,
                end=2.0,
                text="Bonjour comment allez-vous",
                words=[
                    TranscriptWord(start=0.0, end=0.5, text="Bonjour"),
                    TranscriptWord(start=0.5, end=1.0, text="comment"),
                    TranscriptWord(start=1.1, end=1.5, text="allez"),
                    TranscriptWord(start=1.5, end=2.0, text="vous"),
                ],
            )
        ],
    )
    turns = [
        SpeakerTurn(start=0.0, end=1.05, speaker="Speaker 1"),
        SpeakerTurn(start=1.05, end=2.1, speaker="Speaker 2"),
    ]

    result = apply_speaker_turns(transcript, turns)

    assert result.diarization_status == "completed"
    assert [(segment.speaker, segment.text) for segment in result.segments] == [
        ("Speaker 1", "Bonjour comment"),
        ("Speaker 2", "allez vous"),
    ]


def test_worker_keeps_transcript_when_optional_diarization_fails(tmp_path) -> None:
    class BrokenDiarizationClient:
        def diarize(self, audio_path: Path):
            del audio_path
            raise RuntimeError("GPU busy")

    settings = make_settings(tmp_path)
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    input_path = tmp_path / "audio" / "input" / "test.mp3"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"audio")
    seed_job(session_factory, input_path, metadata={"diarization_enabled": True})

    process_audio_job(
        session_factory,
        FakeTranscriptionEngine(),
        "job-1",
        diarization_client=BrokenDiarizationClient(),  # type: ignore[arg-type]
    )

    with session_factory() as session:
        job = session.get(Job, "job-1")
        assert job is not None
        assert job.status == JOB_STATUS_COMPLETED
        result = json.loads(Path(job.result_path or "").read_text(encoding="utf-8"))
        assert result["text"] == "Fake transcript for testing."
        assert result["diarization_status"] == "failed"


def test_worker_requeues_interrupted_jobs_below_attempt_limit(tmp_path) -> None:
    settings = make_settings(tmp_path)
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    input_path = tmp_path / "audio" / "input" / "test.mp3"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"audio")
    seed_job(session_factory, input_path)
    with session_factory() as session:
        job = session.get(Job, "job-1")
        assert job is not None
        job.status = JOB_STATUS_PROCESSING
        job.attempts = 1
        session.commit()

        recovered = recover_processing_jobs(session, max_attempts=3, now=datetime.now(UTC))

        assert recovered == ["job-1"]
        session.refresh(job)
        assert job.status == "queued"
        assert "automatic retry" in (job.progress_message or "")


def test_worker_fails_interrupted_job_at_attempt_limit(tmp_path) -> None:
    settings = make_settings(tmp_path)
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    input_path = tmp_path / "audio" / "input" / "test.mp3"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"audio")
    seed_job(session_factory, input_path)
    with session_factory() as session:
        job = session.get(Job, "job-1")
        assert job is not None
        job.status = JOB_STATUS_PROCESSING
        job.attempts = 3
        session.commit()

        recovered = recover_processing_jobs(session, max_attempts=3, now=datetime.now(UTC))

        assert recovered == []
        session.refresh(job)
        assert job.status == JOB_STATUS_FAILED
        assert "Maximum transcription attempts" in (job.error or "")


def test_check_engine_reports_success_for_fake_engine(tmp_path) -> None:
    result = check_engine(make_settings(tmp_path))

    assert result.engine_name == "fake"
    assert "loaded successfully" in result.message


def test_normalize_faster_whisper_error_reports_missing_model() -> None:
    error = RuntimeError("huggingface_hub.errors.LocalEntryNotFoundError: model missing")

    normalized = normalize_faster_whisper_error(error)

    assert "Whisper model is not available locally." in str(normalized)
