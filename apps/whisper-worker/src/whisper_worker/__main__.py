from __future__ import annotations

import argparse
import logging
import time

from whisper_worker.config import get_settings
from whisper_worker.database import Base, create_engine_for_settings, create_session_factory, ensure_job_metadata_column
from whisper_worker.diarization import prepare_diarization_model
from whisper_worker.engines import check_engine, create_engine, prepare_model
from whisper_worker.processor import process_audio_job
from whisper_worker.queue_backend import RedisQueueBackend


def configure_logging() -> None:
    from os import getenv

    log_level = getenv("WHISPER_WORKER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m whisper_worker")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("run", help="Run the worker loop")
    subparsers.add_parser("check-engine", help="Check whether the configured transcription engine can be loaded")
    prepare_parser = subparsers.add_parser("prepare-model", help="Download and cache the configured faster-whisper model")
    prepare_parser.add_argument("--model", required=False, help="Optional model size override, for example medium or large-v3")
    diarization_parser = subparsers.add_parser(
        "prepare-diarization-model",
        help="Download and cache the configured pyannote diarization model",
    )
    diarization_parser.add_argument("--model", required=False, help="Optional model override")

    return parser


def run_worker_loop() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    db_engine = create_engine_for_settings(settings)
    Base.metadata.create_all(db_engine)
    ensure_job_metadata_column(db_engine)
    session_factory = create_session_factory(db_engine)
    queue = RedisQueueBackend(settings.redis_url, settings.queue_name)
    logger = logging.getLogger("whisper_worker")

    logger.info("Worker started with engine=%s queue=%s", settings.transcription_engine, settings.queue_name)

    while True:
        message = queue.pop_audio_job(timeout_seconds=1)
        if message is None:
            time.sleep(0.1)
            continue
        process_audio_job(session_factory, engine, message.job_id)


def check_engine_command() -> int:
    settings = get_settings()
    print(f"TRANSCRIPTION_ENGINE={settings.transcription_engine}")
    print(f"WHISPER_MODEL_SIZE={settings.whisper_model_size}")
    print(f"WHISPER_DEVICE={settings.whisper_device}")
    print(f"WHISPER_COMPUTE_TYPE={settings.whisper_compute_type}")
    print(f"WHISPER_MODEL_CACHE_DIR={settings.whisper_model_cache_dir}")
    print(f"DIARIZATION_ENABLED={settings.diarization_enabled}")
    print(f"DIARIZATION_PROVIDER={settings.diarization_provider}")
    print(f"DIARIZATION_MODEL={settings.diarization_model}")
    print(f"DIARIZATION_DEVICE={settings.diarization_device}")
    print(f"DIARIZATION_MODEL_CACHE_DIR={settings.diarization_model_cache_dir}")
    print(f"DIARIZATION_TIMEOUT_SECONDS={settings.diarization_timeout_seconds}")
    print(f"DIARIZATION_MAX_AUDIO_SECONDS={settings.diarization_max_audio_seconds}")
    try:
        result = check_engine(settings)
    except Exception as exc:
        print(str(exc))
        return 1

    print(result.message)
    return 0


def prepare_model_command(model_override: str | None) -> int:
    settings = get_settings()
    selected_model = model_override or settings.whisper_model_size
    print(f"Preparing faster-whisper model: {selected_model}")
    print(f"Model cache directory: {settings.whisper_model_cache_dir}")

    try:
        model_path = prepare_model(settings, model_size=selected_model)
    except Exception as exc:
        print(str(exc))
        return 1

    print(f"Model prepared successfully: {model_path}")
    return 0


def prepare_diarization_model_command(model_override: str | None) -> int:
    settings = get_settings()
    selected_model = model_override or settings.diarization_model
    print(f"Preparing diarization model: {selected_model}")
    print(f"Model cache directory: {settings.diarization_model_cache_dir}")
    print("A Hugging Face token may be required for gated pyannote models.")

    try:
        model_path = prepare_diarization_model(settings, model_name=selected_model)
    except Exception as exc:
        print(str(exc))
        return 1

    print(f"Diarization model prepared successfully: {model_path}")
    return 0


def main() -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "run"

    try:
        if command == "run":
            run_worker_loop()
            return 0
        if command == "check-engine":
            return check_engine_command()
        if command == "prepare-model":
            return prepare_model_command(args.model)
        if command == "prepare-diarization-model":
            return prepare_diarization_model_command(args.model)
        parser.error("Unknown command")
        return 1
    except KeyboardInterrupt:
        logging.getLogger("whisper_worker").info("Worker stopped")
        return 0
    except Exception as exc:
        logging.getLogger("whisper_worker").error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
