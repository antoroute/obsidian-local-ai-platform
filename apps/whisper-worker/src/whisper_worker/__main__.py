import logging
import time

from whisper_worker.config import get_settings
from whisper_worker.database import Base, create_engine_for_settings, create_session_factory
from whisper_worker.engines import create_engine
from whisper_worker.processor import process_audio_job
from whisper_worker.queue_backend import RedisQueueBackend


def configure_logging() -> None:
    from os import getenv

    log_level = getenv("WHISPER_WORKER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def run_worker_loop() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    db_engine = create_engine_for_settings(settings)
    Base.metadata.create_all(db_engine)
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


def main() -> None:
    configure_logging()
    try:
        run_worker_loop()
    except KeyboardInterrupt:
        logging.getLogger("whisper_worker").info("Worker stopped")


if __name__ == "__main__":
    main()
