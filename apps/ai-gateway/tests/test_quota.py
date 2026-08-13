from app.quota import QuotaUsage, RedisUsageQuotaLimiter


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.calls: list[tuple[str, int]] = []

    def eval(self, _script: str, _numkeys: int, key: str, ttl: int) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        self.calls.append((key, ttl))
        return self.values[key]


def test_redis_quota_limiter_hashes_user_and_rejects_after_limit() -> None:
    limiter = RedisUsageQuotaLimiter("redis://unused")
    fake_redis = FakeRedis()
    limiter._redis = fake_redis  # type: ignore[assignment]

    first = limiter.consume(user_id="private-user-id", bucket="llm", limit=1)
    second = limiter.consume(user_id="private-user-id", bucket="llm", limit=1)

    assert first == QuotaUsage(count=1, limit=1, retry_after_seconds=first.retry_after_seconds)
    assert not first.exceeded
    assert second.exceeded
    assert fake_redis.calls[0][0] == fake_redis.calls[1][0]
    assert "private-user-id" not in fake_redis.calls[0][0]
    assert 3600 < fake_redis.calls[0][1] <= 90000
