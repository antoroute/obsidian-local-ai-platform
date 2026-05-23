from __future__ import annotations

from pathlib import Path

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


def create_engine(mode: str) -> TranscriptionEngine:
    if mode == "fake":
        return FakeTranscriptionEngine()
    raise ValueError(f"Unsupported transcription mode: {mode}")
