from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from whisper_worker.repositories import TranscriptResult, TranscriptSegment, TranscriptWord


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str


class DiarizationClient:
    def __init__(self, *, base_url: str, token: str, timeout_seconds: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._token)

    def diarize(self, audio_path: Path) -> list[SpeakerTurn]:
        if not self.configured:
            raise RuntimeError("GPU diarization service is not configured.")
        with audio_path.open("rb") as audio_file:
            response = httpx.post(
                f"{self._base_url}/v1/diarize",
                headers={"Authorization": f"Bearer {self._token}"},
                files={"file": (audio_path.name, audio_file, "application/octet-stream")},
                timeout=self._timeout_seconds,
            )
        response.raise_for_status()
        payload = response.json()
        raw_turns = payload.get("turns") if isinstance(payload, dict) else None
        if not isinstance(raw_turns, list):
            raise RuntimeError("GPU diarization service returned an invalid response.")
        turns: list[SpeakerTurn] = []
        for raw_turn in raw_turns:
            if not isinstance(raw_turn, dict):
                continue
            start = raw_turn.get("start")
            end = raw_turn.get("end")
            speaker = raw_turn.get("speaker")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not isinstance(speaker, str):
                continue
            if end > start and speaker.strip():
                turns.append(SpeakerTurn(start=float(start), end=float(end), speaker=speaker.strip()))
        if not turns:
            raise RuntimeError("GPU diarization produced no speaker turns.")
        return turns


def apply_speaker_turns(transcript: TranscriptResult, turns: list[SpeakerTurn]) -> TranscriptResult:
    diarized_segments: list[TranscriptSegment] = []
    for segment in transcript.segments:
        if segment.words:
            diarized_segments.extend(_group_words_by_speaker(segment.words, turns))
            continue
        diarized_segments.append(replace(segment, speaker=_best_speaker(segment.start, segment.end, turns)))
    return replace(
        transcript,
        segments=diarized_segments,
        diarization_enabled=True,
        diarization_status="completed",
    )


def mark_diarization_failed(transcript: TranscriptResult) -> TranscriptResult:
    return replace(transcript, diarization_enabled=True, diarization_status="failed")


def _group_words_by_speaker(words: list[TranscriptWord], turns: list[SpeakerTurn]) -> list[TranscriptSegment]:
    grouped: list[tuple[str | None, list[TranscriptWord]]] = []
    for word in words:
        speaker = _best_speaker(word.start, word.end, turns)
        if grouped and grouped[-1][0] == speaker:
            grouped[-1][1].append(word)
        else:
            grouped.append((speaker, [word]))
    return [
        TranscriptSegment(
            start=group_words[0].start,
            end=group_words[-1].end,
            text=_join_words(group_words),
            speaker=speaker,
            words=group_words,
        )
        for speaker, group_words in grouped
        if group_words
    ]


def _best_speaker(start: float, end: float, turns: list[SpeakerTurn]) -> str | None:
    best_speaker: str | None = None
    best_overlap = 0.0
    midpoint = (start + end) / 2
    for turn in turns:
        overlap = max(0.0, min(end, turn.end) - max(start, turn.start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = turn.speaker
        elif overlap == 0 and best_speaker is None and turn.start <= midpoint <= turn.end:
            best_speaker = turn.speaker
    return best_speaker


def _join_words(words: list[TranscriptWord]) -> str:
    text = ""
    for word in words:
        token = word.text.strip()
        if not token:
            continue
        if text and token[0] not in ".,!?;:)]}'’":
            text += " "
        text += token
    return text.strip()
