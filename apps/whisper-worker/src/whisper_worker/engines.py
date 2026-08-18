from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable

from whisper_worker.config import WorkerSettings
from whisper_worker.repositories import TranscriptResult, TranscriptSegment, TranscriptWord

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, str], None]
CancellationCheck = Callable[[], bool]


class TranscriptionCancelled(RuntimeError):
    pass


class TranscriptionEngine:
    def transcribe(
        self,
        input_path: Path,
        *,
        transcription_language: str | None = None,
        progress_callback: ProgressCallback | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> TranscriptResult:
        raise NotImplementedError


class FakeTranscriptionEngine(TranscriptionEngine):
    def transcribe(
        self,
        input_path: Path,
        *,
        transcription_language: str | None = None,
        progress_callback: ProgressCallback | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> TranscriptResult:
        del input_path
        _raise_if_cancelled(cancellation_check)
        _report_progress(progress_callback, "transcribing", 85, "Transcribing audio.")
        language = _resolve_fake_language(transcription_language)
        return TranscriptResult(
            text="Fake transcript for testing.",
            language=language,
            duration=0,
            segments=[TranscriptSegment(start=0, end=1, text="Fake transcript for testing.")],
        )


@dataclass(frozen=True)
class EngineCheckResult:
    engine_name: str
    model_size: str
    device: str
    compute_type: str
    cache_dir: str
    message: str


class FasterWhisperEngine(TranscriptionEngine):
    def __init__(self, model: Any, *, default_language: str | None, beam_size: int) -> None:
        self._model = model
        self._default_language = default_language
        self._beam_size = beam_size

    def transcribe(
        self,
        input_path: Path,
        *,
        transcription_language: str | None = None,
        progress_callback: ProgressCallback | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> TranscriptResult:
        if not input_path.exists():
            raise FileNotFoundError("Audio input file is missing.")

        _raise_if_cancelled(cancellation_check)
        _report_progress(progress_callback, "normalizing", 10, "Normalizing audio to mono 16 kHz.")
        language = _resolve_faster_whisper_language(transcription_language, self._default_language)
        with normalize_audio_for_whisper(input_path) as whisper_input_path:
            _raise_if_cancelled(cancellation_check)
            _report_progress(progress_callback, "transcribing", 15, "Loading Whisper and starting transcription.")
            segments_iterable, info = self._model.transcribe(
                str(whisper_input_path),
                language=language,
                beam_size=self._beam_size,
                vad_filter=True,
                condition_on_previous_text=False,
                word_timestamps=True,
            )
            # faster-whisper returns a lazy generator. Consume it while the
            # normalized temporary WAV still exists and expose useful progress.
            duration = float(getattr(info, "duration", 0) or 0)
            segments = []
            for segment in segments_iterable:
                _raise_if_cancelled(cancellation_check)
                segments.append(segment)
                segment_end = float(getattr(segment, "end", 0) or 0)
                fraction = min(1.0, segment_end / duration) if duration > 0 else 0.0
                progress = 15 + int(fraction * 70)
                _report_progress(progress_callback, "transcribing", progress, "Transcribing audio.")
        text = " ".join(segment.text.strip() for segment in segments if getattr(segment, "text", "").strip()).strip()
        result_language = getattr(info, "language", None) or language or "unknown"
        _report_progress(progress_callback, "validating", 90, "Validating the transcript.")

        return TranscriptResult(
            text=text,
            language=str(result_language),
            duration=duration,
            segments=[
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=str(segment.text).strip(),
                    words=[
                        TranscriptWord(
                            start=float(word.start),
                            end=float(word.end),
                            text=str(word.word).strip(),
                        )
                        for word in (getattr(segment, "words", None) or [])
                        if getattr(word, "start", None) is not None and getattr(word, "end", None) is not None
                    ],
                )
                for segment in segments
            ],
        )


def _report_progress(callback: ProgressCallback | None, phase: str, progress: int, message: str) -> None:
    if callback is not None:
        callback(phase, progress, message)


def _raise_if_cancelled(cancellation_check: CancellationCheck | None) -> None:
    if cancellation_check is not None and cancellation_check():
        raise TranscriptionCancelled("Transcription cancelled by the user.")


@contextmanager
def normalize_audio_for_whisper(input_path: Path):
    """Decode browser/container audio into a stable mono WAV for faster-whisper."""
    with tempfile.TemporaryDirectory(prefix="whisper-audio-") as temp_dir:
        wav_path = Path(temp_dir) / "input.wav"
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(wav_path),
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if wav_path.exists() and wav_path.stat().st_size > 0:
                logger.info("Normalized audio for Whisper: %s -> mono 16 kHz WAV", input_path.name)
                yield wav_path
                return
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            logger.warning("Audio normalization failed; using original file for Whisper: %s", exc)

        yield input_path


def _resolve_fake_language(transcription_language: str | None) -> str:
    if transcription_language in {"fr", "en"}:
        return transcription_language
    return "fr"


def _resolve_faster_whisper_language(transcription_language: str | None, default_language: str | None) -> str | None:
    if transcription_language == "auto":
        return None
    if transcription_language in {"fr", "en"}:
        return transcription_language
    if default_language in {None, "", "auto"}:
        return None
    return default_language


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

def check_engine(settings: WorkerSettings) -> EngineCheckResult:
    engine = create_engine(settings)
    del engine
    return EngineCheckResult(
        engine_name=settings.transcription_engine,
        model_size=settings.whisper_model_size,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        cache_dir=settings.whisper_model_cache_dir,
        message="Transcription engine loaded successfully.",
    )


def prepare_model(settings: WorkerSettings, *, model_size: str | None = None) -> Path:
    selected_model = model_size or settings.whisper_model_size
    cache_dir = Path(settings.whisper_model_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if settings.transcription_engine != "faster_whisper":
        raise RuntimeError("Model preparation is only applicable to TRANSCRIPTION_ENGINE=faster_whisper.")

    try:
        from faster_whisper.utils import download_model
    except ImportError as exc:
        raise RuntimeError(
            "Preparing a faster-whisper model requires the faster-whisper package to be installed."
        ) from exc

    try:
        model_path = download_model(selected_model, cache_dir=str(cache_dir))
    except Exception as exc:
        raise normalize_faster_whisper_error(exc) from exc

    return Path(model_path)


def _build_faster_whisper_model(settings: WorkerSettings) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "TRANSCRIPTION_ENGINE=faster_whisper requires the faster-whisper package to be installed."
        ) from exc

    if settings.whisper_device == "cuda":
        _ensure_cuda_available()

    cache_dir = Path(settings.whisper_model_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        try:
            return WhisperModel(
                settings.whisper_model_size,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
                cpu_threads=settings.whisper_cpu_threads,
                num_workers=settings.whisper_num_workers,
                download_root=str(cache_dir),
            )
        except TypeError as exc:
            if "download_root" not in str(exc):
                raise
            return WhisperModel(
                settings.whisper_model_size,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
                cpu_threads=settings.whisper_cpu_threads,
                num_workers=settings.whisper_num_workers,
            )
    except Exception as exc:
        raise normalize_faster_whisper_error(exc) from exc


def _ensure_cuda_available() -> None:
    try:
        import ctranslate2
    except ImportError as exc:
        raise RuntimeError(
            "CUDA transcription requires ctranslate2, which is normally installed with faster-whisper."
        ) from exc

    if ctranslate2.get_cuda_device_count() < 1:
        raise RuntimeError("WHISPER_DEVICE=cuda was requested but no CUDA device is available.")


def normalize_faster_whisper_error(exc: Exception) -> RuntimeError:
    message = collect_exception_messages(exc)

    if "localentrynotfounderror" in message or "whisper model is not available locally" in message:
        return RuntimeError(
            "Whisper model is not available locally. Run scripts/prod/prepare-whisper-model.ps1 or allow worker network access to download it."
        )
    if "temporary failure in name resolution" in message or "name or service not known" in message:
        return RuntimeError(
            "Whisper model download failed because Hugging Face could not be resolved. Run scripts/prod/prepare-whisper-model.ps1 with worker network access, or fix container DNS/network access."
        )
    if "connection refused" in message:
        return RuntimeError(
            "Whisper model download failed because the remote service refused the connection. Check outbound connectivity from the worker container."
        )
    if "cuda" in message and "available" in message:
        return RuntimeError("WHISPER_DEVICE=cuda was requested but no CUDA device is available.")

    return RuntimeError(str(exc))


def collect_exception_messages(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = f"{type(current).__name__}: {current}".strip().lower()
        if text:
            parts.append(text)
        current = current.__cause__ or current.__context__
    return " | ".join(parts)
