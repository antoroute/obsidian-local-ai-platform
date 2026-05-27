from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.audio import sanitize_original_filename, save_uploaded_audio, validate_transcription_language
from app.assistant import prepare_assistant_request
from app.auth import get_current_token, require_scope
from app.config import get_settings
from app.database import init_db
from app.models import ApiToken
from app.token_repository import token_has_scope
from app.jobs import (
    JOB_STATUS_COMPLETED,
    create_audio_transcription_job,
    read_transcript_result,
    require_job_for_user,
)
from app.meetings import (
    build_meeting_user_prompt_from_brief,
    extract_transcript_text_from_result,
    prepare_meeting_from_job_request,
    prepare_meeting_request,
    validate_audio_job_for_meeting,
)
from app.notes import prepare_summary_request
from app.queue import AudioJobQueue, get_audio_job_queue
from app.schemas import (
    AudioTranscriptionQueuedResponse,
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantUsageResponse,
    HealthResponse,
    JobResultResponse,
    JobStatusResponse,
    MeetingGenerateFromJobRequest,
    MeetingGenerateFromJobResponse,
    MeetingGenerateRequest,
    MeetingGenerateResponse,
    MeetingUsageResponse,
    ModelsResponse,
    NoteSummarizeRequest,
    NoteSummarizeResponse,
    TranscriptResponse,
    UsageResponse,
    VaultAskRequest,
    VaultAskResponse,
    VaultDeleteResponse,
    VaultIndexNoteRequest,
    VaultIndexNoteResponse,
    VaultSearchRequest,
    VaultSearchResponse,
    VaultSearchResult,
    VaultSourceResponse,
    VaultStatsResponse,
)
from app.services.llm_client import FakeLlmClient, LlmClient, OllamaLlmClient
from app.services.embedding_client import OllamaEmbeddingClient
from app.services.ollama_client import OllamaClient, OllamaResponseError, OllamaUnavailableError
from app.database import get_db_session
from app.models import VaultChunk, VaultDocument
from app.vault_rag import (
    build_insufficient_sources_answer,
    build_vault_answer_prompt,
    ensure_rag_enabled,
    index_note,
    make_snippet,
    search_vault,
    validate_vault_model,
)
from sqlalchemy import delete, func, select

settings = get_settings()

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

if settings.cors_enabled:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )


def get_ollama_client() -> OllamaClient:
    current_settings = get_settings()
    return OllamaClient(
        base_url=current_settings.ollama_base_url,
        timeout_seconds=current_settings.ollama_timeout_seconds,
    )


def get_llm_client() -> LlmClient:
    current_settings = get_settings()
    if current_settings.llm_provider == "fake":
        return FakeLlmClient()
    if current_settings.llm_provider == "ollama":
        return OllamaLlmClient(get_ollama_client())
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {current_settings.llm_provider}")


def get_embedding_client() -> OllamaEmbeddingClient:
    current_settings = get_settings()
    return OllamaEmbeddingClient(
        base_url=current_settings.ollama_base_url,
        timeout_seconds=current_settings.ollama_timeout_seconds,
        model=current_settings.rag_embedding_model,
    )


@app.get("/v1/health", tags=["system"], response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/v1/models", tags=["models"], response_model=ModelsResponse)
def list_models(
    token: Annotated[ApiToken, Depends(require_scope("models:list"))],
) -> ModelsResponse:
    del token
    return ModelsResponse(models=get_settings().allowed_models)


@app.post("/v1/notes/summarize", tags=["notes"], response_model=NoteSummarizeResponse)
async def summarize_note(
    payload: NoteSummarizeRequest,
    token: Annotated[ApiToken, Depends(require_scope("notes:summarize"))],
    llm_client: Annotated[LlmClient, Depends(get_llm_client)],
) -> NoteSummarizeResponse:
    del token
    prepared_request = prepare_summary_request(payload, get_settings())

    try:
        result = await llm_client.summarize_note(
            model=prepared_request.selected_model,
            title=prepared_request.title,
            note_chars=prepared_request.prompt_chars,
            template_chars=prepared_request.template_chars,
            system_prompt=prepared_request.system_prompt,
            user_prompt=prepared_request.user_prompt,
        )
    except OllamaUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The summarization backend is currently unavailable.",
        ) from exc
    except OllamaResponseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The summarization backend returned an invalid response.",
        ) from exc

    return NoteSummarizeResponse(
        model=result.model,
        title=payload.title,
        summary_markdown=result.content,
        usage=UsageResponse(
            prompt_chars=prepared_request.prompt_chars,
            template_chars=prepared_request.template_chars,
        ),
    )


@app.post("/v1/assistant/chat", tags=["assistant"], response_model=AssistantChatResponse)
async def assistant_chat(
    payload: AssistantChatRequest,
    token: Annotated[ApiToken, Depends(require_scope("assistant:chat"))],
    llm_client: Annotated[LlmClient, Depends(get_llm_client)],
) -> AssistantChatResponse:
    del token
    prepared_request = prepare_assistant_request(payload, get_settings())

    try:
        result = await llm_client.assistant_chat(
            model=prepared_request.selected_model,
            mode=prepared_request.mode,
            message_chars=prepared_request.message_chars,
            context_chars=prepared_request.context_chars,
            system_prompt=prepared_request.system_prompt,
            user_prompt=prepared_request.user_prompt,
        )
    except OllamaUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The assistant backend is currently unavailable.",
        ) from exc
    except OllamaResponseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The assistant backend returned an invalid response.",
        ) from exc

    return AssistantChatResponse(
        model=result.model,
        mode=prepared_request.mode,
        answer_markdown=result.content,
        usage=AssistantUsageResponse(
            message_chars=prepared_request.message_chars,
            context_chars=prepared_request.context_chars,
        ),
    )


@app.post("/v1/meetings/generate", tags=["meetings"], response_model=MeetingGenerateResponse)
async def generate_meeting_report(
    payload: MeetingGenerateRequest,
    token: Annotated[ApiToken, Depends(require_scope("meetings:generate"))],
    llm_client: Annotated[LlmClient, Depends(get_llm_client)],
) -> MeetingGenerateResponse:
    del token
    prepared_request = prepare_meeting_request(payload, get_settings())

    try:
        user_prompt = prepared_request.user_prompt
        if prepared_request.should_predigest:
            brief = await llm_client.predigest_meeting(
                model=prepared_request.selected_model,
                title=prepared_request.title,
                transcript_chars=prepared_request.transcript_chars,
                manual_notes_chars=prepared_request.manual_notes_chars,
                system_prompt=prepared_request.predigest_system_prompt,
                user_prompt=prepared_request.predigest_user_prompt,
            )
            user_prompt = build_meeting_user_prompt_from_brief(prepared_request, brief.content)
        result = await llm_client.generate_meeting(
            model=prepared_request.selected_model,
            title=prepared_request.title,
            transcript_chars=prepared_request.transcript_chars,
            manual_notes_chars=prepared_request.manual_notes_chars,
            template_chars=prepared_request.template_chars,
            participants=prepared_request.participants,
            system_prompt=prepared_request.system_prompt,
            user_prompt=user_prompt,
        )
    except OllamaUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The meeting generation backend is currently unavailable.",
        ) from exc
    except OllamaResponseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The meeting generation backend returned an invalid response.",
        ) from exc

    return MeetingGenerateResponse(
        model=result.model,
        title=prepared_request.title,
        meeting_markdown=result.content,
        usage=MeetingUsageResponse(
            transcript_chars=prepared_request.transcript_chars,
            manual_notes_chars=prepared_request.manual_notes_chars,
            template_chars=prepared_request.template_chars,
            participants_count=prepared_request.participants_count,
        ),
    )


@app.post("/v1/meetings/generate-from-job", tags=["meetings"], response_model=MeetingGenerateFromJobResponse)
async def generate_meeting_report_from_job(
    payload: MeetingGenerateFromJobRequest,
    token: Annotated[ApiToken, Depends(require_scope("meetings:generate"))],
    session: Annotated[Session, Depends(get_db_session)],
    llm_client: Annotated[LlmClient, Depends(get_llm_client)],
) -> MeetingGenerateFromJobResponse:
    job = require_job_for_user(session, job_id=payload.job_id, user_id=token.user_id)
    validate_audio_job_for_meeting(job)

    try:
        transcript_payload = read_transcript_result(job)
        transcript_text = extract_transcript_text_from_result(transcript_payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored transcript result is invalid.",
        ) from exc

    prepared_request = prepare_meeting_from_job_request(payload, transcript=transcript_text, settings=get_settings())

    try:
        user_prompt = prepared_request.user_prompt
        if prepared_request.should_predigest:
            brief = await llm_client.predigest_meeting(
                model=prepared_request.selected_model,
                title=prepared_request.title,
                transcript_chars=prepared_request.transcript_chars,
                manual_notes_chars=prepared_request.manual_notes_chars,
                system_prompt=prepared_request.predigest_system_prompt,
                user_prompt=prepared_request.predigest_user_prompt,
            )
            user_prompt = build_meeting_user_prompt_from_brief(prepared_request, brief.content)
        result = await llm_client.generate_meeting(
            model=prepared_request.selected_model,
            title=prepared_request.title,
            transcript_chars=prepared_request.transcript_chars,
            manual_notes_chars=prepared_request.manual_notes_chars,
            template_chars=prepared_request.template_chars,
            participants=prepared_request.participants,
            system_prompt=prepared_request.system_prompt,
            user_prompt=user_prompt,
        )
    except OllamaUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The meeting generation backend is currently unavailable.",
        ) from exc
    except OllamaResponseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The meeting generation backend returned an invalid response.",
        ) from exc

    return MeetingGenerateFromJobResponse(
        job_id=job.id,
        model=result.model,
        title=prepared_request.title,
        meeting_markdown=result.content,
        usage=MeetingUsageResponse(
            transcript_chars=prepared_request.transcript_chars,
            manual_notes_chars=prepared_request.manual_notes_chars,
            template_chars=prepared_request.template_chars,
            participants_count=prepared_request.participants_count,
        ),
    )


@app.post("/v1/vault/index-note", tags=["vault"], response_model=VaultIndexNoteResponse)
async def vault_index_note(
    payload: VaultIndexNoteRequest,
    token: Annotated[ApiToken, Depends(require_scope("vault:index"))],
    session: Annotated[Session, Depends(get_db_session)],
    embedding_client: Annotated[OllamaEmbeddingClient, Depends(get_embedding_client)],
) -> VaultIndexNoteResponse:
    settings = get_settings()
    workspace_id = resolve_rag_workspace_id(token, settings, payload.workspace_id)
    try:
        status_value, document, chunks_indexed, content_hash = await index_note(
            session,
            user_id=token.user_id,
            workspace_id=workspace_id,
            payload=payload,
            settings=settings,
            embed_text=lambda text: _embed_text(embedding_client, text),
        )
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The embedding backend is currently unavailable.") from exc
    except OllamaResponseError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The embedding backend returned an invalid response.") from exc

    return VaultIndexNoteResponse(
        status=status_value,
        document_id=document.id,
        path=document.path,
        chunks_indexed=chunks_indexed,
        content_hash=content_hash,
    )


@app.post("/v1/vault/search", tags=["vault"], response_model=VaultSearchResponse)
async def vault_search(
    payload: VaultSearchRequest,
    token: Annotated[ApiToken, Depends(require_scope("vault:search"))],
    session: Annotated[Session, Depends(get_db_session)],
    embedding_client: Annotated[OllamaEmbeddingClient, Depends(get_embedding_client)],
) -> VaultSearchResponse:
    settings = get_settings()
    workspace_id = resolve_rag_workspace_id(token, settings, payload.workspace_id)
    try:
        hits = await search_vault(
            session,
            user_id=token.user_id,
            workspace_id=workspace_id,
            payload=payload,
            settings=settings,
            embed_text=lambda text: _embed_text(embedding_client, text),
        )
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The embedding backend is currently unavailable.") from exc
    except OllamaResponseError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The embedding backend returned an invalid response.") from exc

    return VaultSearchResponse(
        query=payload.query,
        results=[
            VaultSearchResult(
                path=hit.document.path,
                title=hit.document.title,
                heading_path=hit.chunk.heading_path,
                snippet=make_snippet(hit.chunk.content),
                score=round(hit.score, 6),
                vector_score=round(hit.vector_score, 6),
                keyword_bonus=round(hit.keyword_bonus, 6),
                matched_terms=hit.matched_terms,
                chunk_index=hit.chunk.chunk_index,
            )
            for hit in hits
        ],
    )


@app.post("/v1/vault/ask", tags=["vault"], response_model=VaultAskResponse)
async def vault_ask(
    payload: VaultAskRequest,
    token: Annotated[ApiToken, Depends(require_scope("vault:ask"))],
    session: Annotated[Session, Depends(get_db_session)],
    embedding_client: Annotated[OllamaEmbeddingClient, Depends(get_embedding_client)],
    llm_client: Annotated[LlmClient, Depends(get_llm_client)],
) -> VaultAskResponse:
    settings = get_settings()
    ensure_rag_enabled(settings)
    selected_model = validate_vault_model(payload.model or settings.default_model, settings)
    workspace_id = resolve_rag_workspace_id(token, settings, payload.workspace_id)
    try:
        hits = await search_vault(
            session,
            user_id=token.user_id,
            workspace_id=workspace_id,
            payload=VaultSearchRequest(
                vault_id=payload.vault_id,
                workspace_id=workspace_id,
                query=payload.question,
                top_k=payload.top_k,
                path_prefix=payload.path_prefix,
                tags=payload.tags,
            ),
            settings=settings,
            embed_text=lambda text: _embed_text(embedding_client, text),
        )
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The embedding backend is currently unavailable.") from exc
    except OllamaResponseError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The embedding backend returned an invalid response.") from exc

    sources = [
        VaultSourceResponse(
            path=hit.document.path,
            title=hit.document.title,
            heading_path=hit.chunk.heading_path,
            chunk_index=hit.chunk.chunk_index,
            score=round(hit.score, 6),
            vector_score=round(hit.vector_score, 6),
            keyword_bonus=round(hit.keyword_bonus, 6),
            matched_terms=hit.matched_terms,
        )
        for hit in hits
    ]
    debug_info = build_vault_debug_info(hits, settings) if payload.debug else None
    if not hits or all(hit.score < settings.rag_min_score for hit in hits):
        return VaultAskResponse(model=selected_model, answer_markdown=build_insufficient_sources_answer(hits), sources=sources, debug_info=debug_info)

    system_prompt, user_prompt = build_vault_answer_prompt(
        question=payload.question,
        hits=hits,
        settings=settings,
        answer_language=payload.answer_language,
    )
    try:
        result = await llm_client.vault_ask(
            model=selected_model,
            question_chars=len(payload.question),
            context_chars=len(user_prompt),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The vault answer backend is currently unavailable.") from exc
    except OllamaResponseError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The vault answer backend returned an invalid response.") from exc

    return VaultAskResponse(model=result.model, answer_markdown=result.content, sources=sources, debug_info=debug_info)


@app.get("/v1/vault/stats", tags=["vault"], response_model=VaultStatsResponse)
def vault_stats(
    token: Annotated[ApiToken, Depends(get_current_token)],
    session: Annotated[Session, Depends(get_db_session)],
    vault_id: str | None = None,
    workspace_id: str | None = None,
) -> VaultStatsResponse:
    settings = get_settings()
    ensure_rag_enabled(settings)
    if not (token_has_scope(token, "vault:search") or token_has_scope(token, "vault:admin")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing required scope: vault:search or vault:admin")
    selected_vault_id = vault_id or settings.rag_default_vault_id
    selected_workspace_id = resolve_rag_workspace_id(token, settings, workspace_id)
    documents = int(session.scalar(select(func.count()).select_from(VaultDocument).where(VaultDocument.workspace_id == selected_workspace_id, VaultDocument.vault_id == selected_vault_id, VaultDocument.deleted_at.is_(None))) or 0)
    chunks = int(session.scalar(select(func.count()).select_from(VaultChunk).where(VaultChunk.workspace_id == selected_workspace_id, VaultChunk.vault_id == selected_vault_id)) or 0)
    last_indexed = session.scalar(select(func.max(VaultDocument.indexed_at)).where(VaultDocument.workspace_id == selected_workspace_id, VaultDocument.vault_id == selected_vault_id))
    return VaultStatsResponse(vault_id=selected_vault_id, workspace_id=selected_workspace_id, documents=documents, chunks=chunks, last_indexed_at=last_indexed.isoformat() if last_indexed else None)


@app.delete("/v1/vault/index", tags=["vault"], response_model=VaultDeleteResponse)
def vault_delete_index(
    token: Annotated[ApiToken, Depends(require_scope("vault:admin"))],
    session: Annotated[Session, Depends(get_db_session)],
    vault_id: str | None = None,
    workspace_id: str | None = None,
    all_users: bool = False,
) -> VaultDeleteResponse:
    settings = get_settings()
    ensure_rag_enabled(settings)
    selected_vault_id = vault_id or settings.rag_default_vault_id
    selected_workspace_id = None if all_users else resolve_rag_workspace_id(token, settings, workspace_id)
    if all_users:
        chunks = int(session.scalar(select(func.count()).select_from(VaultChunk).where(VaultChunk.vault_id == selected_vault_id)) or 0)
        documents = int(session.scalar(select(func.count()).select_from(VaultDocument).where(VaultDocument.vault_id == selected_vault_id)) or 0)
        session.execute(delete(VaultDocument).where(VaultDocument.vault_id == selected_vault_id))
    else:
        chunks = int(session.scalar(select(func.count()).select_from(VaultChunk).where(VaultChunk.workspace_id == selected_workspace_id, VaultChunk.vault_id == selected_vault_id)) or 0)
        documents = int(session.scalar(select(func.count()).select_from(VaultDocument).where(VaultDocument.workspace_id == selected_workspace_id, VaultDocument.vault_id == selected_vault_id)) or 0)
        session.execute(delete(VaultDocument).where(VaultDocument.workspace_id == selected_workspace_id, VaultDocument.vault_id == selected_vault_id))
    session.commit()
    return VaultDeleteResponse(vault_id=selected_vault_id, workspace_id=selected_workspace_id, all_users=all_users, document_deleted=documents > 0, chunks_deleted=chunks, deleted_documents=documents, deleted_chunks=chunks)


@app.delete("/v1/vault/document", tags=["vault"], response_model=VaultDeleteResponse)
def vault_delete_document(
    token: Annotated[ApiToken, Depends(require_scope("vault:index"))],
    session: Annotated[Session, Depends(get_db_session)],
    vault_id: str | None = None,
    workspace_id: str | None = None,
    path: str | None = None,
) -> VaultDeleteResponse:
    settings = get_settings()
    ensure_rag_enabled(settings)
    selected_vault_id = vault_id or settings.rag_default_vault_id
    selected_workspace_id = resolve_rag_workspace_id(token, settings, workspace_id)
    if not path:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="path is required.")
    document = session.scalar(select(VaultDocument).where(VaultDocument.workspace_id == selected_workspace_id, VaultDocument.vault_id == selected_vault_id, VaultDocument.path == path))
    if document is None:
        return VaultDeleteResponse(vault_id=selected_vault_id, workspace_id=selected_workspace_id, path=path, document_deleted=False, chunks_deleted=0, deleted_documents=0, deleted_chunks=0)
    chunks = int(session.scalar(select(func.count()).select_from(VaultChunk).where(VaultChunk.document_id == document.id)) or 0)
    session.delete(document)
    session.commit()
    return VaultDeleteResponse(vault_id=selected_vault_id, workspace_id=selected_workspace_id, path=path, document_deleted=True, chunks_deleted=chunks, deleted_documents=1, deleted_chunks=chunks)


def build_vault_debug_info(hits, settings) -> dict[str, object]:
    return {
        "search_candidates_count": len(hits),
        "selected_sources_count": len(hits),
        "min_score": settings.rag_min_score,
        "top_scores": [round(hit.score, 6) for hit in hits[:10]],
        "top_vector_scores": [round(hit.vector_score, 6) for hit in hits[:10]],
        "top_keyword_bonuses": [round(hit.keyword_bonus, 6) for hit in hits[:10]],
        "selected_paths": [hit.document.path for hit in hits],
        "matched_terms_by_path": {hit.document.path: hit.matched_terms for hit in hits[:10]},
    }


def resolve_rag_workspace_id(token: ApiToken, settings, requested_workspace_id: str | None = None) -> str:
    configured = (requested_workspace_id or settings.rag_workspace_id or "").strip()
    return configured or token.user_id


async def _embed_text(embedding_client: OllamaEmbeddingClient, text: str) -> list[float]:
    result = await embedding_client.embed_text(text)
    return result.embedding


@app.post("/v1/audio/transcribe", tags=["audio"], response_model=AudioTranscriptionQueuedResponse)
async def transcribe_audio(
    file: Annotated[UploadFile, File(...)],
    token: Annotated[ApiToken, Depends(require_scope("audio:transcribe"))],
    session: Annotated[Session, Depends(get_db_session)],
    queue: Annotated[AudioJobQueue, Depends(get_audio_job_queue)],
    transcription_language: Annotated[str, Form()] = "auto",
) -> AudioTranscriptionQueuedResponse:
    settings = get_settings()
    requested_language = validate_transcription_language(transcription_language)
    input_path = await save_uploaded_audio(file, settings)
    job = create_audio_transcription_job(
        session,
        user_id=token.user_id,
        input_path=input_path,
        metadata={
            "transcription_language": requested_language,
            "original_filename": sanitize_original_filename(file.filename),
        },
    )
    queue.enqueue_audio_transcription(job.id, input_path=input_path, transcription_language=requested_language)
    return AudioTranscriptionQueuedResponse(job_id=job.id, status=job.status)


@app.get("/v1/jobs/{job_id}", tags=["jobs"], response_model=JobStatusResponse)
def get_job(
    job_id: str,
    token: Annotated[ApiToken, Depends(get_current_token)],
    session: Annotated[Session, Depends(get_db_session)],
) -> JobStatusResponse:
    job = require_job_for_user(session, job_id=job_id, user_id=token.user_id)
    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        error=job.error,
    )


@app.get("/v1/jobs/{job_id}/result", tags=["jobs"], response_model=JobResultResponse)
def get_job_result(
    job_id: str,
    token: Annotated[ApiToken, Depends(get_current_token)],
    session: Annotated[Session, Depends(get_db_session)],
) -> JobResultResponse:
    job = require_job_for_user(session, job_id=job_id, user_id=token.user_id)
    if job.status != JOB_STATUS_COMPLETED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job is not completed.")

    transcript = read_transcript_result(job)
    return JobResultResponse(job_id=job.id, transcript=TranscriptResponse.model_validate(transcript))
