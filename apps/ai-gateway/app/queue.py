from __future__ import annotations

import json
from functools import lru_cache

from redis import Redis

from app.config import get_settings


class AudioJobQueue:
    def enqueue_audio_transcription(self, job_id: str) -> None:
        raise NotImplementedError


class RedisAudioJobQueue(AudioJobQueue):
    def __init__(self, redis_url: str, queue_name: str) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._queue_name = queue_name

    def enqueue_audio_transcription(self, job_id: str) -> None:
        message = json.dumps({"job_id": job_id})
        self._redis.rpush(self._queue_name, message)


@lru_cache
def get_audio_job_queue() -> AudioJobQueue:
    settings = get_settings()
    return RedisAudioJobQueue(settings.redis_url, settings.audio_queue_name)
