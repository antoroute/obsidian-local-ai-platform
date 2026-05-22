from __future__ import annotations

import argparse
from datetime import UTC, datetime

from app.database import get_session_factory, init_db
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

    with session_factory() as session:
        created_token = create_api_token(
            session,
            name=args.name,
            scopes=scopes,
            expires_at=parse_expiration(args.expires_at),
        )

    print("Token created successfully.")
    print("Store it now: it will not be shown again.")
    print(created_token.plain_token)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "create-token":
        return create_token_command(args)

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
