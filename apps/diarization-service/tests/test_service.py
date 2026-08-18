import sys
from types import ModuleType, SimpleNamespace

from diarization_service import main
from diarization_service.main import normalize_speaker_turns


class FakeAnnotation:
    def itertracks(self, *, yield_label: bool):
        assert yield_label is True
        yield SimpleNamespace(start=5.0, end=6.0), None, "SPEAKER_01"
        yield SimpleNamespace(start=0.0, end=2.0), None, "SPEAKER_00"
        yield SimpleNamespace(start=2.0, end=4.0), None, "SPEAKER_01"


def test_turns_are_sorted_and_speakers_are_renamed_by_first_appearance() -> None:
    turns = normalize_speaker_turns(FakeAnnotation())

    assert [(turn.start, turn.end, turn.speaker) for turn in turns] == [
        (0.0, 2.0, "Speaker 1"),
        (2.0, 4.0, "Speaker 2"),
        (5.0, 6.0, "Speaker 2"),
    ]


def test_ollama_health_checks_the_real_api(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    called_urls: list[str] = []

    def fake_get(url: str, *, timeout: int):
        del timeout
        called_urls.append(url)
        return FakeResponse()

    monkeypatch.setattr(main.httpx, "get", fake_get)

    assert main.ollama_health()["status"] == "ok"
    assert called_urls == [f"{main.settings.ollama_base_url}/api/version"]


def test_pipeline_uses_the_writable_model_cache(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def empty_cache() -> None:
            return None

    class FakePipeline:
        @classmethod
        def from_pretrained(cls, model: str, **kwargs):
            captured.update(model=model, **kwargs)
            return cls()

        def to(self, device) -> None:
            captured["device"] = device

        def __call__(self, audio_path: str, **kwargs):
            captured["audio_path"] = audio_path
            captured["options"] = kwargs
            return FakeAnnotation()

    fake_torch = ModuleType("torch")
    fake_torch.cuda = FakeCuda()
    fake_torch.device = lambda name: name
    fake_pyannote = ModuleType("pyannote")
    fake_pyannote_audio = ModuleType("pyannote.audio")
    fake_pyannote_audio.Pipeline = FakePipeline
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "pyannote", fake_pyannote)
    monkeypatch.setitem(sys.modules, "pyannote.audio", fake_pyannote_audio)
    monkeypatch.setattr(main.settings, "model_cache_dir", str(tmp_path))

    turns = main.run_diarization_pipeline(tmp_path / "audio.wav", 2, 3)

    assert turns
    assert captured["cache_dir"] == str(tmp_path / "pipeline")
    assert captured["use_auth_token"] == (main.settings.hf_token or None)
    assert captured["options"] == {"min_speakers": 2, "max_speakers": 3}


def test_transparent_ollama_routes_are_registered_after_service_routes() -> None:
    paths = [route.path for route in main.app.routes]

    assert paths.index("/v1/health") < paths.index("/v1/{upstream_path:path}")
    assert paths.index("/api/version") < paths.index("/api/{upstream_path:path}")
    assert paths.index("/api/tags") < paths.index("/api/{upstream_path:path}")
    assert "/api/{upstream_path:path}" in paths
    assert "/ollama/{upstream_path:path}" in paths
