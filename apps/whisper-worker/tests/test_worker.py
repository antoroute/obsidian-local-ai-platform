import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

from whisper_worker.database import Base, create_engine_for_settings, create_session_factory
from whisper_worker.diarization import (
    SpeakerTurn,
    apply_diarization_to_segments,
    build_offline_pyannote_pipeline_config,
    download_required_pyannote_snapshots,
    load_pyannote_pipeline,
    normalize_pyannote_load_error,
    required_pyannote_dependency_models,
)
from whisper_worker.engines import (
    DiarizationTimeoutError,
    DiarizingTranscriptionEngine,
    FakeTranscriptionEngine,
    FasterWhisperEngine,
    check_engine,
    create_engine,
    normalize_faster_whisper_error,
    run_diarization_with_timeout,
    should_skip_diarization_for_duration,
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


def slow_diarization_runner(input_path: Path, settings: WorkerSettings) -> list[SpeakerTurn]:
    del input_path, settings
    time.sleep(10)
    return [SpeakerTurn(start=0.0, end=1.0, speaker="Speaker 1")]


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


def test_runtime_pyannote_load_uses_local_files_only(tmp_path) -> None:
    class FakePipeline:
        captured_kwargs: dict[str, object] | None = None
        captured_model_name: str | None = None
        captured_hf_offline: str | None = None

        @classmethod
        def from_pretrained(cls, model_name: str, **kwargs):
            cls.captured_model_name = model_name
            cls.captured_kwargs = kwargs
            import os

            cls.captured_hf_offline = os.getenv("HF_HUB_OFFLINE")
            return object()

    seed_pyannote_cache(tmp_path)

    load_pyannote_pipeline(
        FakePipeline,
        "pyannote/speaker-diarization-3.1",
        cache_dir=tmp_path,
        local_files_only=True,
    )

    assert FakePipeline.captured_kwargs is not None
    assert FakePipeline.captured_model_name is not None
    captured_model_name = FakePipeline.captured_model_name.replace("\\", "/")
    assert captured_model_name.endswith("offline-configs/pyannote--speaker-diarization-3.1.yaml")
    assert "local_files_only" not in FakePipeline.captured_kwargs
    assert FakePipeline.captured_hf_offline == "1"


def test_prepare_pyannote_load_allows_download(tmp_path) -> None:
    class FakePipeline:
        captured_kwargs: dict[str, object] | None = None

        @classmethod
        def from_pretrained(cls, model_name: str, **kwargs):
            assert model_name == "pyannote/speaker-diarization-3.1"
            cls.captured_kwargs = kwargs
            return object()

    load_pyannote_pipeline(
        FakePipeline,
        "pyannote/speaker-diarization-3.1",
        cache_dir=tmp_path,
        local_files_only=False,
    )

    assert FakePipeline.captured_kwargs is not None
    assert "local_files_only" not in FakePipeline.captured_kwargs


def test_prepare_downloads_pyannote_pipeline_dependencies(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    fake_module = ModuleType("huggingface_hub")

    def fake_snapshot_download(repo_id: str, **kwargs):
        calls.append(repo_id)
        assert kwargs["cache_dir"] == str(tmp_path)
        return str(tmp_path / repo_id.replace("/", "--"))

    fake_module.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_module)

    download_required_pyannote_snapshots("pyannote/speaker-diarization-3.1", tmp_path)

    assert calls == [
        "pyannote/speaker-diarization-3.1",
        "pyannote/segmentation-3.0",
        "pyannote/wespeaker-voxceleb-resnet34-LM",
    ]


def test_required_pyannote_dependency_models_are_explicit() -> None:
    assert required_pyannote_dependency_models("pyannote/speaker-diarization-3.1") == [
        "pyannote/segmentation-3.0",
        "pyannote/wespeaker-voxceleb-resnet34-LM",
    ]


def test_offline_pyannote_config_points_to_local_checkpoints(tmp_path) -> None:
    seed_pyannote_cache(tmp_path)

    config_path = build_offline_pyannote_pipeline_config(tmp_path, "pyannote/speaker-diarization-3.1")
    text = config_path.read_text(encoding="utf-8")

    assert "pyannote/segmentation-3.0" not in text
    assert "pyannote/wespeaker-voxceleb-resnet34-LM" not in text
    assert "models--pyannote--segmentation-3.0" in text
    assert "models--pyannote--wespeaker-voxceleb-resnet34-LM" in text
    assert "pytorch_model.bin" in text


def test_normalize_pyannote_load_error_reports_missing_local_cache() -> None:
    error = RuntimeError("LocalEntryNotFoundError: cannot find the requested files in the local cache")

    normalized = normalize_pyannote_load_error(
        error,
        "pyannote/speaker-diarization-3.1",
        local_files_only=True,
    )

    assert "not available in the local cache" in str(normalized)
    assert "prepare-diarization-model.ps1" in str(normalized)


def test_runtime_pyannote_load_rejects_incomplete_cache(tmp_path) -> None:
    class FakePipeline:
        @classmethod
        def from_pretrained(cls, model_name: str, **kwargs):
            raise AssertionError("from_pretrained should not be called when cache is incomplete")

    try:
        load_pyannote_pipeline(
            FakePipeline,
            "pyannote/speaker-diarization-3.1",
            cache_dir=tmp_path,
            local_files_only=True,
        )
    except RuntimeError as exc:
        assert "not fully available in the local cache" in str(exc)
        assert "pyannote/segmentation-3.0/pytorch_model.bin" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError for incomplete pyannote cache")


def seed_pyannote_cache(cache_dir: Path) -> None:
    for model_name, file_name in [
        ("pyannote/speaker-diarization-3.1", "config.yaml"),
        ("pyannote/segmentation-3.0", "pytorch_model.bin"),
        ("pyannote/wespeaker-voxceleb-resnet34-LM", "pytorch_model.bin"),
    ]:
        snapshot_dir = cache_dir / f"models--{model_name.replace('/', '--')}" / "snapshots" / "test"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        if model_name == "pyannote/speaker-diarization-3.1":
            (snapshot_dir / file_name).write_text(
                """
version: 3.1.0
pipeline:
  name: pyannote.audio.pipelines.SpeakerDiarization
  params:
    embedding: pyannote/wespeaker-voxceleb-resnet34-LM
    segmentation: pyannote/segmentation-3.0
params: {}
""".strip(),
                encoding="utf-8",
            )
        else:
            (snapshot_dir / file_name).write_text("cache", encoding="utf-8")


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


def test_diarization_adds_speaker_labels_without_timeout(tmp_path, monkeypatch) -> None:
    class FastDiarizationEngine:
        def diarize(self, input_path: Path):
            return [SpeakerTurn(start=0.0, end=1.0, speaker="Speaker 1")]

    def fake_create_diarization_engine(settings):
        return FastDiarizationEngine()

    monkeypatch.setattr("whisper_worker.engines.create_diarization_engine", fake_create_diarization_engine)
    settings = WorkerSettings(**{**make_settings(tmp_path).__dict__, "diarization_enabled": True})
    engine = DiarizingTranscriptionEngine(FakeTranscriptionEngine(), settings)

    result = engine.transcribe(tmp_path / "a.mp3")

    assert result.diarization_status == "completed"
    assert result.segments[0].speaker == "Speaker 1"


def test_diarization_timeout_preserves_whisper_transcript(tmp_path) -> None:
    settings = make_settings(tmp_path)

    try:
        run_diarization_with_timeout(
            tmp_path / "input.mp3",
            settings,
            timeout_seconds=1,
            runner=slow_diarization_runner,
        )
    except DiarizationTimeoutError as exc:
        assert "exceeded 1 seconds" in str(exc)
    else:
        raise AssertionError("Expected diarization timeout")


def test_diarization_timeout_marks_diarization_failed_without_failing_transcription(tmp_path, monkeypatch) -> None:
    def fake_run_diarization_with_timeout(input_path, settings, *, timeout_seconds):
        del input_path, settings, timeout_seconds
        raise DiarizationTimeoutError("Diarization exceeded 1 seconds.")

    monkeypatch.setattr("whisper_worker.engines.run_diarization_with_timeout", fake_run_diarization_with_timeout)
    settings = WorkerSettings(**{**make_settings(tmp_path).__dict__, "diarization_enabled": True})
    engine = DiarizingTranscriptionEngine(FakeTranscriptionEngine(), settings)

    result = engine.transcribe(tmp_path / "input.mp3")

    assert result.text == "Fake transcript for testing."
    assert result.diarization_enabled is True
    assert result.diarization_status == "failed"
    assert result.segments[0].speaker is None


def test_diarization_max_audio_seconds_skips_long_audio() -> None:
    assert should_skip_diarization_for_duration(3618.6, 3600) is True
    assert should_skip_diarization_for_duration(120.0, 3600) is False
    assert should_skip_diarization_for_duration(3618.6, None) is False
    assert should_skip_diarization_for_duration(3618.6, 0) is False


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
