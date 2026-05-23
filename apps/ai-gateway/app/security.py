from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

TOKEN_PREFIX = "obsai_live_"
PBKDF2_ITERATIONS = 600_000
AVAILABLE_SCOPES = {
    "models:list",
    "notes:summarize",
    "meetings:generate",
    "audio:transcribe",
    "admin",
}


@dataclass(frozen=True)
class CreatedToken:
    plain_token: str
    token_hash: str


def utc_now() -> datetime:
    return datetime.now(UTC)


def generate_user_id() -> str:
    return str(uuid.uuid4())


def generate_plain_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def validate_token_format(token: str) -> bool:
    return token.startswith(TOKEN_PREFIX) and len(token) > len(TOKEN_PREFIX)


def normalize_scopes(raw_scopes: list[str]) -> list[str]:
    scopes = sorted({scope.strip() for scope in raw_scopes if scope.strip()})
    invalid_scopes = sorted(set(scopes) - AVAILABLE_SCOPES)
    if invalid_scopes:
        invalid = ", ".join(invalid_scopes)
        raise ValueError(f"Unsupported scopes: {invalid}")
    return scopes


def encode_scopes(scopes: list[str]) -> str:
    return ",".join(normalize_scopes(scopes))


def decode_scopes(scopes: str) -> set[str]:
    return set(normalize_scopes(scopes.split(","))) if scopes else set()


def hash_token(token: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_token_hash(token: str, stored_hash: str) -> bool:
    try:
        algorithm, iteration_str, salt, expected = stored_hash.split("$", maxsplit=3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iteration_str)
    except ValueError:
        return False

    digest = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return secrets.compare_digest(digest, expected)


def create_token_secret() -> CreatedToken:
    plain_token = generate_plain_token()
    return CreatedToken(plain_token=plain_token, token_hash=hash_token(plain_token))


def is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    return expires_at <= utc_now()
