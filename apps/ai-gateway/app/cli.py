from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import get_session_factory, init_db
from app.services.ollama_client import (
    OllamaClient,
    OllamaServiceError,
    format_ollama_diagnostic_error,
)
from app.token_repository import create_api_token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_token_parser = subparsers.add_parser("create-token", help="Create a development API token")
    create_token_parser.add_argument("--name", required=True, help="Human-readable token name")
    create_token_parser.add_argument(
        "--scopes",
        required=True,
        help="Comma-separated scopes, for example models:list,notes:summarize",
    )
    create_token_parser.add_argument(
        "--expires-at",
        required=False,
        help="Optional UTC expiration in ISO-8601 format, for example 2026-12-31T23:59:59+00:00",
    )

    check_ollama_parser = subparsers.add_parser("check-ollama", help="Check Ollama connectivity from ai-gateway")
    check_ollama_parser.add_argument("--model", required=False, help="Model to test, defaults to DEFAULT_MODEL")
    check_ollama_parser.add_argument("--base-url", required=False, help="Ollama base URL, defaults to OLLAMA_BASE_URL")

    return parser


def parse_expiration(value: str | None) -> datetime | None:
    if value is None:
        return None

    expires_at = datetime.fromisoformat(value)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at


def create_token_command(args: argparse.Namespace) -> int:
    init_db()
    session_factory = get_session_factory()
    scopes = [scope.strip() for scope in args.scopes.split(",")]

    try:
        with session_factory() as session:
            created_token = create_api_token(
                session,
                name=args.name,
                scopes=scopes,
                expires_at=parse_expiration(args.expires_at),
            )
    except SQLAlchemyError as exc:
        if is_outdated_local_schema_error(exc):
            print(
                "Local database schema appears outdated. For development, delete apps/ai-gateway/ai_gateway.db and retry, or run migrations when available.",
                file=sys.stderr,
            )
            return 2
        raise

    print("Token created successfully.")
    print("Store it now: it will not be shown again.")
    print(created_token.plain_token)
    return 0


def check_ollama_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    base_url = (args.base_url or settings.ollama_base_url).rstrip("/")
    model = args.model or settings.default_model
    client = OllamaClient(base_url=base_url, timeout_seconds=settings.ollama_timeout_seconds)

    print(f"OLLAMA_BASE_URL: {base_url}")
    print(f"Model: {model}")
    print("Testing GET /api/tags ...")

    try:
        result = client.check_connectivity(model=model)
    except OllamaServiceError as exc:
        print(format_ollama_diagnostic_error(exc), file=sys.stderr)
        return 1

    print(f"Available models: {', '.join(result.available_models) if result.available_models else '(none)'}")
    print("Testing POST /api/chat ...")
    print(f"Chat model: {result.chat_model}")
    print(f"Chat preview: {result.chat_content[:80]}")
    print("Ollama connectivity OK")
    return 0


def is_outdated_local_schema_error(exc: SQLAlchemyError) -> bool:
    message = str(exc).lower()
    return (
        "no such column" in message
        or "has no column named" in message
        or "unknown column" in message
        or "undefined column" in message
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "create-token":
        return create_token_command(args)
    if args.command == "check-ollama":
        return check_ollama_command(args)

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
