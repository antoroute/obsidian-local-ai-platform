import logging
import os
import time


def configure_logging() -> None:
    log_level = os.getenv("WHISPER_WORKER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    configure_logging()
    queue_name = os.getenv("WHISPER_WORKER_QUEUE", "transcription_jobs")
    redis_url = os.getenv("WHISPER_WORKER_REDIS_URL", "redis://redis:6379/0")

    logging.getLogger("whisper_worker").info(
        "Bootstrap worker started for queue=%s redis=%s",
        queue_name,
        redis_url,
    )

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logging.getLogger("whisper_worker").info("Worker stopped")


if __name__ == "__main__":
    main()
