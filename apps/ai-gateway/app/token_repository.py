from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApiToken
from app.security import CreatedToken, create_token_secret, decode_scopes, encode_scopes, utc_now, verify_token_hash


def create_api_token(
    session: Session,
    *,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
) -> CreatedToken:
    created_token = create_token_secret()
    token_record = ApiToken(
        token_hash=created_token.token_hash,
        name=name,
        scopes=encode_scopes(scopes),
        created_at=utc_now(),
        expires_at=expires_at,
        revoked=False,
    )
    session.add(token_record)
    session.commit()
    return created_token


def find_token_by_secret(session: Session, token: str) -> ApiToken | None:
    statement = select(ApiToken)
    for record in session.scalars(statement):
        if verify_token_hash(token, record.token_hash):
            return record
    return None


def token_has_scope(token: ApiToken, scope: str) -> bool:
    token_scopes = decode_scopes(token.scopes)
    return scope in token_scopes or "admin" in token_scopes
