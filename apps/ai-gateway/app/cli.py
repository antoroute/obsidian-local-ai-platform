from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

import httpx
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import get_engine, get_session_factory, init_db
from app.services.embedding_client import OllamaEmbeddingClient
from app.services.ollama_client import (
    ERROR_KIND_INVALID_JSON,
    OllamaClient,
    OllamaResponseError,
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

    subparsers.add_parser("check-rag", help="Check PostgreSQL pgvector and Ollama embeddings for RAG")

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
    client = OllamaClient(
        base_url=base_url,
        timeout_seconds=settings.ollama_timeout_seconds,
        num_ctx=settings.ollama_num_ctx,
        keep_alive=settings.ollama_keep_alive,
    )

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


def check_rag_command(_: argparse.Namespace) -> int:
    settings = get_settings()
    print(f"RAG_ENABLED: {settings.rag_enabled}")
    print(f"RAG_VECTOR_BACKEND: {settings.rag_vector_backend}")
    print(f"RAG_EMBEDDING_MODEL: {settings.rag_embedding_model}")
    print(f"RAG_EMBEDDING_DIMENSION: {settings.rag_embedding_dimension}")

    if not settings.rag_enabled:
        print("RAG is disabled.")
        return 1
    if settings.rag_vector_backend != "pgvector":
        print("RAG check failed: production RAG requires RAG_VECTOR_BACKEND=pgvector.", file=sys.stderr)
        return 1

    try:
        available_models = check_ollama_tags(settings.ollama_base_url, settings.ollama_timeout_seconds)
        print(f"Ollama models: {', '.join(available_models) if available_models else '(none)'}")
        if settings.rag_embedding_model not in available_models:
            print(
                f"Embedding model missing in Docker Ollama: {settings.rag_embedding_model}\n"
                "Run scripts/prod/prepare-ollama-models.ps1 -Mode gpu -Source host -Models 'mistral:latest,nomic-embed-text:latest'",
                file=sys.stderr,
            )
            return 1
        if settings.default_model not in available_models:
            print(f"Chat model missing in Docker Ollama: {settings.default_model}", file=sys.stderr)
            return 1
        embedding = asyncio.run(check_embedding_model(settings))
        check_chat_model(settings)
        init_db()
        engine = get_engine()
        if engine.dialect.name != "postgresql":
            print("RAG check failed: pgvector backend requires PostgreSQL.", file=sys.stderr)
            return 1
        check_pgvector_schema(engine, settings.rag_embedding_dimension)
    except OllamaServiceError as exc:
        print(format_ollama_diagnostic_error(exc), file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"RAG database check failed: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"RAG check failed: {exc}", file=sys.stderr)
        return 1

    if len(embedding) != settings.rag_embedding_dimension:
        print(
            f"Embedding dimension mismatch: expected {settings.rag_embedding_dimension}, got {len(embedding)}. Check RAG_EMBEDDING_MODEL/RAG_EMBEDDING_DIMENSION.",
            file=sys.stderr,
        )
        return 1

    print("RAG check OK")
    return 0


def check_ollama_tags(base_url: str, timeout_seconds: int) -> list[str]:
    normalized_base_url = base_url.rstrip("/")
    print(f"Testing GET {normalized_base_url}/api/tags ...")
    try:
        with httpx.Client(base_url=normalized_base_url, timeout=timeout_seconds) as client:
            response = client.get("/api/tags")
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise OllamaServiceError("Ollama /api/tags timed out.", base_url=normalized_base_url) from exc
    except httpx.ConnectError as exc:
        raise OllamaServiceError("Ollama /api/tags is unreachable.", base_url=normalized_base_url) from exc
    except httpx.HTTPStatusError as exc:
        raise OllamaResponseError(
            "Ollama /api/tags returned an error.",
            base_url=normalized_base_url,
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise OllamaServiceError("Failed to contact Ollama /api/tags.", base_url=normalized_base_url) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise OllamaResponseError(
            "Ollama returned invalid JSON for /api/tags.",
            kind=ERROR_KIND_INVALID_JSON,
            base_url=normalized_base_url,
        ) from exc

    models = payload.get("models")
    if not isinstance(models, list):
        raise OllamaResponseError(
            "Ollama returned an invalid /api/tags payload.",
            kind=ERROR_KIND_INVALID_JSON,
            base_url=normalized_base_url,
        )

    available: list[str] = []
    for item in models:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            available.append(item["name"].strip())
    return available


def check_pgvector_schema(engine, expected_dimension: int) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "vault_chunks" not in tables:
        raise RuntimeError("table vault_chunks is missing.")
    with engine.connect() as connection:
        extension_exists = connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"))
        if not extension_exists:
            raise RuntimeError("pgvector extension is not installed. Run CREATE EXTENSION IF NOT EXISTS vector.")
        column_type = connection.scalar(
            text(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = 'vault_chunks' AND a.attname = 'embedding' AND a.attnum > 0
                """
            )
        )
        expected_type = f"vector({expected_dimension})"
        if column_type != expected_type:
            raise RuntimeError(f"vault_chunks.embedding must be {expected_type}, got {column_type}.")
        index_exists = connection.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE tablename = 'vault_chunks'
                      AND indexname IN ('idx_vault_chunks_embedding_hnsw', 'idx_vault_chunks_embedding_ivfflat')
                )
                """
            )
        )
        if not index_exists:
            raise RuntimeError("no pgvector index found on vault_chunks.embedding.")


async def check_embedding_model(settings) -> list[float]:
    print(f"Testing POST {settings.ollama_base_url.rstrip('/')}/api/embed with {settings.rag_embedding_model} ...")
    client = OllamaEmbeddingClient(
        base_url=settings.ollama_base_url,
        timeout_seconds=settings.ollama_timeout_seconds,
        model=settings.rag_embedding_model,
    )
    result = await client.embed_text("test")
    return result.embedding


def check_chat_model(settings) -> None:
    print(f"Testing POST {settings.ollama_base_url.rstrip('/')}/api/chat with {settings.default_model} ...")
    client = OllamaClient(
        base_url=settings.ollama_base_url,
        timeout_seconds=settings.ollama_timeout_seconds,
        num_ctx=settings.ollama_num_ctx,
        keep_alive=settings.ollama_keep_alive,
    )
    client.check_connectivity(model=settings.default_model)


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
    if args.command == "check-rag":
        return check_rag_command(args)

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
