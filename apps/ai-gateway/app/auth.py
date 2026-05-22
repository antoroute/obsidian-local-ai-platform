from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db_session
from app.models import ApiToken
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
