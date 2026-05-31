from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from whisper_worker.config import WorkerSettings
from whisper_worker.repositories import TranscriptSegment


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str


class DiarizationEngine:
    def diarize(self, input_path: Path) -> list[SpeakerTurn]:
        raise NotImplementedError


class PyannoteDiarizationEngine(DiarizationEngine):
    def __init__(self, pipeline: Any, *, min_speakers: int | None, max_speakers: int | None) -> None:
        self._pipeline = pipeline
        self._min_speakers = min_speakers
        self._max_speakers = max_speakers

    def diarize(self, input_path: Path) -> list[SpeakerTurn]:
        kwargs: dict[str, int] = {}
        if self._min_speakers is not None:
            kwargs["min_speakers"] = self._min_speakers
        if self._max_speakers is not None:
            kwargs["max_speakers"] = self._max_speakers

        annotation = self._pipeline(str(input_path), **kwargs)
        turns: list[SpeakerTurn] = []
        speaker_map: dict[str, str] = {}
        for turn, _, raw_speaker in annotation.itertracks(yield_label=True):
            speaker_key = str(raw_speaker)
            speaker = speaker_map.setdefault(speaker_key, f"Speaker {len(speaker_map) + 1}")
            turns.append(SpeakerTurn(start=float(turn.start), end=float(turn.end), speaker=speaker))
        return turns


def create_diarization_engine(settings: WorkerSettings) -> DiarizationEngine | None:
    if not settings.diarization_enabled:
        return None
    if settings.diarization_provider != "pyannote":
        raise ValueError(f"Unsupported diarization provider: {settings.diarization_provider}")

    ensure_python_int_digit_compat()
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise RuntimeError("DIARIZATION_ENABLED=true requires the pyannote.audio extra to be installed.") from exc

    model_cache_dir = Path(settings.diarization_model_cache_dir)
    model_cache_dir.mkdir(parents=True, exist_ok=True)
    pipeline = load_pyannote_pipeline(Pipeline, settings.diarization_model, cache_dir=model_cache_dir)
    if settings.diarization_device == "cuda":
        try:
            import torch

            pipeline.to(torch.device("cuda"))
        except Exception as exc:
            raise RuntimeError("DIARIZATION_DEVICE=cuda was requested but pyannote could not use CUDA.") from exc
    elif settings.diarization_device == "cpu":
        try:
            import torch

            pipeline.to(torch.device("cpu"))
        except Exception:
            pass
    return PyannoteDiarizationEngine(
        pipeline,
        min_speakers=settings.diarization_min_speakers,
        max_speakers=settings.diarization_max_speakers,
    )


def apply_diarization_to_segments(segments: list[TranscriptSegment], turns: list[SpeakerTurn]) -> list[TranscriptSegment]:
    if not turns:
        return segments
    return [
        TranscriptSegment(
            start=segment.start,
            end=segment.end,
            text=segment.text,
            speaker=find_best_speaker(segment, turns),
        )
        for segment in segments
    ]


def find_best_speaker(segment: TranscriptSegment, turns: list[SpeakerTurn]) -> str | None:
    best_speaker: str | None = None
    best_overlap = 0.0
    midpoint = (segment.start + segment.end) / 2
    closest_distance = float("inf")
    closest_speaker: str | None = None
    for turn in turns:
        overlap = max(0.0, min(segment.end, turn.end) - max(segment.start, turn.start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = turn.speaker
        distance = min(abs(midpoint - turn.start), abs(midpoint - turn.end))
        if distance < closest_distance:
            closest_distance = distance
            closest_speaker = turn.speaker
    return best_speaker or closest_speaker


def prepare_diarization_model(settings: WorkerSettings, *, model_name: str | None = None) -> Path:
    selected_model = model_name or settings.diarization_model
    cache_dir = Path(settings.diarization_model_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    ensure_python_int_digit_compat()
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise RuntimeError("Preparing a diarization model requires the pyannote.audio extra to be installed.") from exc

    load_pyannote_pipeline(Pipeline, selected_model, cache_dir=cache_dir)
    return cache_dir


def ensure_python_int_digit_compat() -> None:
    # Ubuntu 22.04's python3.11 package can be 3.11.0rc1, while newer torch
    # expects these CPython APIs. They are harmless no-ops for our usage.
    import sys

    if not hasattr(sys, "get_int_max_str_digits"):
        setattr(sys, "get_int_max_str_digits", lambda: 4300)
    if not hasattr(sys, "set_int_max_str_digits"):
        setattr(sys, "set_int_max_str_digits", lambda maxdigits: None)


def load_pyannote_pipeline(pipeline_class: Any, model_name: str, *, cache_dir: Path) -> Any:
    token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN")
    attempts: list[dict[str, object]] = []
    if token:
        attempts.append({"cache_dir": str(cache_dir), "use_auth_token": token})
        attempts.append({"cache_dir": str(cache_dir), "token": token})
    attempts.append({"cache_dir": str(cache_dir)})

    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            pipeline = pipeline_class.from_pretrained(model_name, **kwargs)
            if pipeline is None:
                raise RuntimeError(
                    f"Could not load diarization model '{model_name}'. Accept the pyannote model terms on Hugging Face and pass -HuggingFaceToken or set HF_TOKEN during preparation."
                )
            return pipeline
        except TypeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            break

    if last_error is not None:
        raise last_error
    return pipeline_class.from_pretrained(model_name)
