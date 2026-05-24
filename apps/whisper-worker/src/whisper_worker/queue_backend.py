from __future__ import annotations

import json
from dataclasses import dataclass

from redis import Redis


@dataclass(frozen=True)
class AudioJobMessage:
    job_id: str
    input_path: str | None = None
    transcription_language: str = "auto"


class QueueBackend:
    def pop_audio_job(self, timeout_seconds: int = 1) -> AudioJobMessage | None:
        raise NotImplementedError


class RedisQueueBackend(QueueBackend):
    def __init__(self, redis_url: str, queue_name: str) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._queue_name = queue_name

    def pop_audio_job(self, timeout_seconds: int = 1) -> AudioJobMessage | None:
        result = self._redis.blpop(self._queue_name, timeout=timeout_seconds)
        if result is None:
            return None
        _, payload = result
        message = json.loads(payload)
        return AudioJobMessage(
            job_id=message["job_id"],
            input_path=message.get("input_path"),
            transcription_language=message.get("transcription_language", "auto"),
        )
