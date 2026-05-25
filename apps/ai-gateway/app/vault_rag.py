from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import VaultChunk, VaultDocument
from app.schemas import VaultAskRequest, VaultIndexNoteRequest, VaultSearchRequest
from app.security import utc_now
from app.vault_chunking import sha256_text, split_markdown_chunks

logger = logging.getLogger(__name__)

RAG_SYSTEM_PROMPT = """You are Note Compagnon in vault RAG mode.
Answer only from the provided retrieved sources.
If the sources are insufficient, say clearly that the available notes do not contain enough information.
Do not complete the answer with unsupported assumptions.
Do not claim that you read the whole vault.
Do not use notes that are not present in the retrieved context.
If answer_language=same_as_input, answer in the main language of the question.
If the question asks for general advice, frame the answer as "D'apres les notes disponibles..." or the equivalent language.
Return Markdown directly, with:
## Reponse
...
## Sources utilisees
- [[path or title]]
"""


@dataclass(frozen=True)
class SearchHit:
    chunk: VaultChunk
    document: VaultDocument
    score: float
    vector_score: float = 0.0
    keyword_bonus: float = 0.0


def ensure_rag_enabled(settings: Settings) -> None:
    if not settings.rag_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Vault RAG is disabled.")
    if settings.rag_vector_backend not in {"pgvector", "sqlite", "memory"}:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Unsupported RAG_VECTOR_BACKEND: {settings.rag_vector_backend}")


def validate_vault_model(model: str, settings: Settings) -> str:
    selected_model = model or settings.default_model
    if selected_model not in settings.allowed_models:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Model is not allowed: {selected_model}")
    return selected_model


def should_exclude_note(payload: VaultIndexNoteRequest, settings: Settings) -> bool:
    normalized_path = payload.path.replace("\\", "/").strip("/")
    parts = normalized_path.split("/")
    excluded_dirs = set(settings.rag_index_excluded_dirs)
    if any(part in excluded_dirs for part in parts):
        return True
    excluded_tags = set(settings.rag_index_excluded_tags)
    return any(tag in excluded_tags for tag in payload.tags)


async def index_note(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    payload: VaultIndexNoteRequest,
    settings: Settings,
    embed_text,
) -> tuple[str, VaultDocument, int, str]:
    ensure_rag_enabled(settings)
    ensure_vector_backend_allowed(session, settings)
    content_hash = sha256_text(payload.content)
    existing = find_document(session, workspace_id=workspace_id, vault_id=payload.vault_id, path=payload.path)
    if existing and existing.content_hash == content_hash and existing.deleted_at is None:
        existing_chunk_count = count_document_chunks(session, existing.id)
        if existing_chunk_count > 0:
            return "skipped", existing, existing_chunk_count, content_hash

    now = utc_now()
    document = existing or VaultDocument(
        id=str(uuid.uuid4()),
        user_id=user_id,
        workspace_id=workspace_id,
        vault_id=payload.vault_id,
        path=payload.path,
        content_hash=content_hash,
    )
    document.title = payload.title
    document.tags_json = json.dumps(payload.tags)
    document.frontmatter_json = json.dumps(payload.frontmatter)
    document.metadata_json = json.dumps(payload.metadata)
    document.modified_at = parse_datetime(payload.modified_at)
    document.indexed_at = now
    document.deleted_at = None

    if should_exclude_note(payload, settings):
        if existing:
            session.execute(delete(VaultChunk).where(VaultChunk.document_id == existing.id))
        session.add(document)
        session.commit()
        return "skipped", document, 0, content_hash

    session.add(document)
    session.flush()
    session.execute(delete(VaultChunk).where(VaultChunk.document_id == document.id))

    chunks = split_markdown_chunks(payload.content, chunk_size=settings.rag_chunk_size, chunk_overlap=settings.rag_chunk_overlap)
    for chunk in chunks:
        embedding = validate_embedding_dimension(await embed_text(chunk.content), settings)
        session.add(
            VaultChunk(
                id=str(uuid.uuid4()),
                document_id=document.id,
                user_id=user_id,
                workspace_id=workspace_id,
                vault_id=payload.vault_id,
                path=payload.path,
                chunk_index=chunk.chunk_index,
                heading_path=chunk.heading_path,
                content=chunk.content,
                content_hash=chunk.content_hash,
                token_estimate=chunk.token_estimate,
                embedding=format_vector_literal(embedding),
                metadata_json=json.dumps({"title": payload.title}),
                created_at=now,
            )
        )
    session.commit()
    session.refresh(document)
    return "indexed", document, len(chunks), content_hash


async def search_vault(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    payload: VaultSearchRequest,
    settings: Settings,
    embed_text,
) -> list[SearchHit]:
    ensure_rag_enabled(settings)
    ensure_vector_backend_allowed(session, settings)
    top_k = normalize_top_k(payload.top_k, settings)
    query_embedding = validate_embedding_dimension(await embed_text(payload.query), settings)
    if settings.rag_vector_backend == "pgvector":
        return search_vault_pgvector(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            payload=payload,
            query_embedding=query_embedding,
            top_k=top_k,
            settings=settings,
        )
    statement = (
        select(VaultChunk, VaultDocument)
        .join(VaultDocument, VaultDocument.id == VaultChunk.document_id)
        .where(VaultChunk.workspace_id == workspace_id, VaultChunk.vault_id == payload.vault_id, VaultDocument.deleted_at.is_(None))
    )
    if payload.path_prefix:
        statement = statement.where(VaultChunk.path.startswith(payload.path_prefix))
    rows = session.execute(statement).all()
    hits: list[SearchHit] = []
    tag_filter = set(payload.tags)
    for chunk, document in rows:
        if tag_filter and not tag_filter.issubset(set(load_json_list(document.tags_json))):
            continue
        vector_score = cosine_similarity(query_embedding, load_embedding(chunk.embedding))
        score, keyword_bonus = apply_hybrid_score(vector_score, payload.query, chunk, document, settings)
        if score >= settings.rag_min_score:
            hits.append(SearchHit(chunk=chunk, document=document, score=score, vector_score=vector_score, keyword_bonus=keyword_bonus))
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:top_k]


def search_vault_pgvector(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    payload: VaultSearchRequest,
    query_embedding: list[float],
    top_k: int,
    settings: Settings,
) -> list[SearchHit]:
    query_literal = format_vector_literal(query_embedding)
    limit = max(settings.rag_search_candidates, top_k * 5, top_k)
    sql = """
        SELECT c.id AS chunk_id, 1 - (c.embedding <=> CAST(:query_embedding AS vector)) AS score
        FROM vault_chunks c
        JOIN vault_documents d ON d.id = c.document_id
        WHERE c.workspace_id = :workspace_id
          AND c.vault_id = :vault_id
          AND d.deleted_at IS NULL
    """
    params: dict[str, object] = {
        "query_embedding": query_literal,
        "workspace_id": workspace_id,
        "vault_id": payload.vault_id,
        "limit": limit,
    }
    if payload.path_prefix:
        sql += " AND c.path LIKE :path_prefix"
        params["path_prefix"] = f"{payload.path_prefix}%"
    sql += " ORDER BY c.embedding <=> CAST(:query_embedding AS vector) LIMIT :limit"

    rows = session.execute(text(sql), params).mappings().all()
    tag_filter = set(payload.tags)
    hits: list[SearchHit] = []
    for row in rows:
        chunk = session.get(VaultChunk, row["chunk_id"])
        if chunk is None:
            continue
        document = session.get(VaultDocument, chunk.document_id)
        if document is None:
            continue
        if tag_filter and not tag_filter.issubset(set(load_json_list(document.tags_json))):
            continue
        vector_score = float(row["score"] or 0.0)
        score, keyword_bonus = apply_hybrid_score(vector_score, payload.query, chunk, document, settings)
        hits.append(SearchHit(chunk=chunk, document=document, score=score, vector_score=vector_score, keyword_bonus=keyword_bonus))
    hits.sort(key=lambda hit: hit.score, reverse=True)
    filtered = [hit for hit in hits if hit.score >= settings.rag_min_score]
    selected = (filtered or hits)[:top_k]
    logger.info(
        "RAG search workspace_id=%s vault_id=%s query_len=%s top_k=%s candidates=%s after_threshold=%s selected_paths=%s scores=%s",
        workspace_id,
        payload.vault_id,
        len(payload.query),
        top_k,
        len(hits),
        len(filtered),
        [hit.document.path for hit in selected],
        [round(hit.score, 4) for hit in selected],
    )
    return selected


def apply_hybrid_score(vector_score: float, query: str, chunk: VaultChunk, document: VaultDocument, settings: Settings) -> tuple[float, float]:
    if not settings.rag_keyword_bonus_enabled:
        return vector_score, 0.0
    terms = extract_keyword_terms(query)
    if not terms:
        return vector_score, 0.0
    path = document.path.lower()
    title = (document.title or "").lower()
    heading = (chunk.heading_path or "").lower()
    content = chunk.content.lower()
    tags = " ".join(load_json_list(document.tags_json)).lower()
    bonus = 0.0
    for term in terms:
        normalized = term.lower()
        if normalized in content:
            bonus += 0.03
        if normalized in heading:
            bonus += 0.04
        if normalized in title:
            bonus += 0.06
        if normalized in path:
            bonus += 0.05
        if normalized in tags:
            bonus += 0.04
    bounded_bonus = min(settings.rag_keyword_bonus_max, bonus)
    return vector_score + bounded_bonus, bounded_bonus


def extract_keyword_terms(query: str) -> list[str]:
    raw_terms = re.findall(r"[\w.-]{3,}", query, flags=re.UNICODE)
    stopwords = {
        "avec", "dans", "pour", "quoi", "quel", "quelle", "comment", "d'apres", "apres", "mes", "notes",
        "the", "and", "for", "what", "how", "from", "with",
    }
    terms: list[str] = []
    for term in raw_terms:
        lowered = term.lower()
        if lowered not in stopwords and lowered not in terms:
            terms.append(lowered)
    return terms


def build_vault_answer_prompt(*, question: str, hits: list[SearchHit], settings: Settings, answer_language: str) -> tuple[str, str]:
    context_parts: list[str] = []
    used_chars = 0
    for index, hit in enumerate(hits, start=1):
        source_header = f"[Source {index}] path={hit.document.path} title={hit.document.title or ''} heading={hit.chunk.heading_path or ''} score={hit.score:.3f}"
        block = f"{source_header}\n{hit.chunk.content.strip()}"
        if used_chars + len(block) > settings.rag_max_context_chars:
            remaining = settings.rag_max_context_chars - used_chars
            if remaining <= 0:
                break
            block = block[:remaining]
        context_parts.append(block)
        used_chars += len(block)

    language_instruction = {
        "fr": "Reponds en francais.",
        "en": "Answer in English.",
    }.get(answer_language, "Answer in the main language of the question.")

    user_prompt = (
        f"answer_language={answer_language}\n"
        f"{language_instruction}\n\n"
        f"Question:\n{question}\n\n"
        "Retrieved vault sources:\n"
        f"{chr(10).join(context_parts) if context_parts else '(no source above the relevance threshold)'}"
    )
    return RAG_SYSTEM_PROMPT, user_prompt


def build_insufficient_sources_answer(hits: list[SearchHit]) -> str:
    if not hits:
        return "## Reponse\n\nLes notes disponibles ne contiennent pas assez d'informations pour repondre de maniere fiable.\n\n## Sources utilisees\n\n- Aucune source pertinente trouvee."
    return "## Reponse\n\nLes sources retrouvees sont trop faibles ou insuffisantes pour repondre sans supposition.\n\n## Sources utilisees\n" + "\n".join(
        f"- [[{hit.document.path}]]" for hit in hits
    )


def find_document(session: Session, *, workspace_id: str, vault_id: str, path: str) -> VaultDocument | None:
    return session.scalar(select(VaultDocument).where(VaultDocument.workspace_id == workspace_id, VaultDocument.vault_id == vault_id, VaultDocument.path == path))


def count_document_chunks(session: Session, document_id: str) -> int:
    return int(session.scalar(select(func.count()).select_from(VaultChunk).where(VaultChunk.document_id == document_id)) or 0)


def normalize_top_k(top_k: int | None, settings: Settings) -> int:
    requested = top_k or settings.rag_max_chunks_per_query
    return max(1, min(requested, settings.rag_max_chunks_per_query))


def ensure_vector_backend_allowed(session: Session, settings: Settings) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql" and settings.rag_vector_backend != "pgvector":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="PostgreSQL RAG requires RAG_VECTOR_BACKEND=pgvector.")
    if dialect != "postgresql" and settings.rag_vector_backend == "pgvector":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="RAG_VECTOR_BACKEND=pgvector requires PostgreSQL with pgvector.")


def validate_embedding_dimension(embedding: list[float], settings: Settings) -> list[float]:
    expected = settings.rag_embedding_dimension
    actual = len(embedding)
    if actual != expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Embedding dimension mismatch: expected {expected}, got {actual}. Check RAG_EMBEDDING_MODEL/RAG_EMBEDDING_DIMENSION.",
        )
    return embedding


def format_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{value:.9g}" for value in embedding) + "]"


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def load_embedding(raw: str) -> list[float]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [float(item) for item in value if isinstance(item, int | float)]


def load_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return (dot / (left_norm * right_norm) + 1.0) / 2.0


def make_snippet(content: str, max_chars: int = 320) -> str:
    collapsed = " ".join(content.split())
    return collapsed if len(collapsed) <= max_chars else f"{collapsed[: max_chars - 1]}..."
