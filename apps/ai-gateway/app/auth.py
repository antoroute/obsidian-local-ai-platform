from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db_session
from app.models import ApiToken
from app.quota import UsageQuotaLimiter, get_usage_quota_limiter
from app.security import is_expired, validate_token_format
from app.token_repository import find_token_by_secret, token_has_scope

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[Session, Depends(get_db_session)],
) -> ApiToken:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication credentials were not provided or are invalid.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized

    token_value = credentials.credentials.strip()
    if not validate_token_format(token_value):
        raise unauthorized

    token = find_token_by_secret(session, token_value)
    if token is None or token.revoked or is_expired(token.expires_at):
        raise unauthorized

    return token


def require_scope(required_scope: str):
    def dependency(token: Annotated[ApiToken, Depends(get_current_token)]) -> ApiToken:
        if not token_has_scope(token, required_scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope: {required_scope}",
            )
        return token

    return dependency


def require_scope_with_quotas(required_scope: str, *quota_buckets: str):
    scope_dependency = require_scope(required_scope)

    def dependency(
        token: Annotated[ApiToken, Depends(scope_dependency)],
        limiter: Annotated[UsageQuotaLimiter, Depends(get_usage_quota_limiter)],
    ) -> ApiToken:
        settings = get_settings()
        if not settings.usage_quotas_enabled:
            return token

        limits = {
            "llm": settings.daily_llm_requests_per_user,
            "embedding": settings.daily_embedding_requests_per_user,
            "audio": settings.daily_audio_jobs_per_user,
        }
        try:
            for bucket in quota_buckets:
                usage = limiter.consume(user_id=token.user_id, bucket=bucket, limit=limits[bucket])
                if usage.exceeded:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=f"Daily {bucket} quota exceeded.",
                        headers={"Retry-After": str(usage.retry_after_seconds)},
                    )
        except RedisError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Usage quota service is unavailable.",
            ) from exc
        return token

    return dependency
