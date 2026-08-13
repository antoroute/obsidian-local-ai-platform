from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from redis import Redis

from app.config import get_settings


_CONSUME_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


@dataclass(frozen=True)
class QuotaUsage:
    count: int
    limit: int
    retry_after_seconds: int

    @property
    def exceeded(self) -> bool:
        return self.count > self.limit


class UsageQuotaLimiter:
    def consume(self, *, user_id: str, bucket: str, limit: int) -> QuotaUsage:
        raise NotImplementedError


class RedisUsageQuotaLimiter(UsageQuotaLimiter):
    def __init__(self, redis_url: str) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)

    def consume(self, *, user_id: str, bucket: str, limit: int) -> QuotaUsage:
        now = datetime.now(timezone.utc)
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        retry_after_seconds = max(1, int((tomorrow - now).total_seconds()))
        key_user = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
        key = f"obsidian-ai:quota:v1:{bucket}:{now.date().isoformat()}:{key_user}"
        count = int(self._redis.eval(_CONSUME_SCRIPT, 1, key, retry_after_seconds + 3600))
        return QuotaUsage(count=count, limit=limit, retry_after_seconds=retry_after_seconds)


@lru_cache
def get_usage_quota_limiter() -> UsageQuotaLimiter:
    settings = get_settings()
    return RedisUsageQuotaLimiter(settings.redis_url)
