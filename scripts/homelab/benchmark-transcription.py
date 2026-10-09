"""Measure the configured worker without consuming jobs or printing transcripts.

Run inside the worker image with its model cache mounted, offline, with the same
CPU/memory limits as production. An optional UTF-8 reference enables normalized
word-error-rate measurement; synthetic speech is only a smoke test.
"""

import argparse
import hashlib
import json
import re
import resource
import time
import unicodedata
from pathlib import Path

from whisper_worker.config import get_settings
from whisper_worker.engines import create_engine


def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    return re.findall(r"[^\W_]+", text, flags=re.UNICODE)


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for index, expected in enumerate(reference, 1):
        current = [index]
        for position, actual in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[position] + 1,
                               previous[position - 1] + (expected != actual)))
        previous = current
    return previous[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--language", choices=("auto", "fr", "en"), default="auto")
    args = parser.parse_args()
    settings = get_settings()
    if settings.transcription_engine != "faster_whisper":
        raise ValueError("Benchmark requires TRANSCRIPTION_ENGINE=faster_whisper.")
    started = time.monotonic()
    engine = create_engine(settings)
    loaded = time.monotonic()
    result = engine.transcribe(args.audio, transcription_language=args.language)
    finished = time.monotonic()
    peak_rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    with args.audio.open("rb") as audio_file:
        audio_sha256 = hashlib.file_digest(audio_file, "sha256").hexdigest()
    output = {
        "audio_sha256": audio_sha256,
        "model": settings.whisper_model_size,
        "device": settings.whisper_device,
        "compute_type": settings.whisper_compute_type,
        "beam_size": settings.whisper_beam_size,
        "cpu_threads": settings.whisper_cpu_threads,
        "language_requested": args.language,
        "language_detected": result.language,
        "audio_seconds": round(result.duration, 3),
        "load_seconds": round(loaded - started, 3),
        "transcription_seconds": round(finished - loaded, 3),
        "real_time_factor": round((finished - loaded) / result.duration, 3) if result.duration else None,
        "peak_process_rss_mib": round(peak_rss_mib, 1),
        "segments": len(result.segments),
        "words": len(words(result.text)),
        "word_timestamps": sum(len(segment.words or []) for segment in result.segments),
    }
    if args.reference:
        reference = words(args.reference.read_text(encoding="utf-8-sig"))
        if not reference:
            raise ValueError("Reference must contain words.")
        errors = edit_distance(reference, words(result.text))
        output.update(reference_words=len(reference), word_errors=errors,
                      normalized_wer=round(errors / len(reference), 4))
    print(json.dumps(output, ensure_ascii=False))
    if not result.text.strip() or not result.segments:
        raise SystemExit("No usable transcript produced.")


if __name__ == "__main__":
    main()
