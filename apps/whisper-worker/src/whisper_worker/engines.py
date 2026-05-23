from __future__ import annotations

from pathlib import Path
from typing import Any

from whisper_worker.config import WorkerSettings
from whisper_worker.repositories import TranscriptResult, TranscriptSegment


class TranscriptionEngine:
    def transcribe(self, input_path: Path) -> TranscriptResult:
        raise NotImplementedError


class FakeTranscriptionEngine(TranscriptionEngine):
    def transcribe(self, input_path: Path) -> TranscriptResult:
        del input_path
        return TranscriptResult(
            text="Fake transcript for testing.",
            language="fr",
            duration=0,
            segments=[TranscriptSegment(start=0, end=1, text="Fake transcript for testing.")],
        )


class FasterWhisperEngine(TranscriptionEngine):
    def __init__(self, model: Any, *, default_language: str | None, beam_size: int) -> None:
        self._model = model
        self._default_language = default_language
        self._beam_size = beam_size

    def transcribe(self, input_path: Path) -> TranscriptResult:
        if not input_path.exists():
            raise FileNotFoundError("Audio input file is missing.")

        segments_iterable, info = self._model.transcribe(
            str(input_path),
            language=self._default_language,
            beam_size=self._beam_size,
        )
        segments = list(segments_iterable)
        text = " ".join(segment.text.strip() for segment in segments if getattr(segment, "text", "").strip()).strip()
        language = getattr(info, "language", None) or self._default_language or "unknown"
        duration = float(getattr(info, "duration", 0) or 0)

        return TranscriptResult(
            text=text,
            language=str(language),
            duration=duration,
            segments=[
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=str(segment.text).strip(),
                )
                for segment in segments
            ],
        )


def create_engine(settings: WorkerSettings) -> TranscriptionEngine:
    if settings.transcription_engine == "fake":
        return FakeTranscriptionEngine()

    if settings.transcription_engine == "faster_whisper":
        model = _build_faster_whisper_model(settings)
        return FasterWhisperEngine(
            model,
            default_language=settings.whisper_language,
            beam_size=settings.whisper_beam_size,
        )

    raise ValueError(f"Unsupported transcription engine: {settings.transcription_engine}")


def _build_faster_whisper_model(settings: WorkerSettings) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "TRANSCRIPTION_ENGINE=faster_whisper requires the faster-whisper package to be installed."
        ) from exc

    if settings.whisper_device == "cuda":
        _ensure_cuda_available()

    return WhisperModel(
        settings.whisper_model_size,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )


def _ensure_cuda_available() -> None:
    try:
        import ctranslate2
    except ImportError as exc:
        raise RuntimeError(
            "CUDA transcription requires ctranslate2, which is normally installed with faster-whisper."
        ) from exc

    if ctranslate2.get_cuda_device_count() < 1:
        raise RuntimeError("WHISPER_DEVICE=cuda was requested but no CUDA device is available.")
