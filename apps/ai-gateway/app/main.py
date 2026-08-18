from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
import secrets
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from redis import Redis
from sqlalchemy.orm import Session

from app.audio import sanitize_original_filename, save_uploaded_audio, validate_transcription_language
from app.assistant import prepare_assistant_request
from app.auth import get_current_token, require_scope, require_scope_with_quotas
from app.config import get_settings
from app.database import init_db
from app.models import ApiToken
from app.token_repository import token_has_scope
from app.jobs import (
    JOB_STATUS_COMPLETED,
    create_audio_transcription_job,
    decode_job_metadata,
    ensure_audio_job_capacity,
    job_is_stalled,
    list_jobs_for_user,
    read_transcript_result,
    request_job_cancellation,
    require_job_for_user,
)
from app.meetings import (
    DeepThinkRenderedSection,
    PreparedMeetingRequest,
    assemble_deep_think_report,
    build_deep_think_section_system_prompt,
    build_deep_think_section_user_prompt,
    build_deep_think_sections,
    build_meeting_predigest_chunk_user_prompt,
    build_meeting_user_prompt_from_brief,
    extract_transcript_text_from_result,
    prepare_meeting_from_job_request,
    prepare_meeting_request,
    split_transcript_for_predigest,
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
    JobCancelResponse,
    JobListResponse,
    JobStatusResponse,
    MeetingGenerateFromJobRequest,
    MeetingGenerateFromJobResponse,
    MeetingGenerateRequest,
    MeetingGenerateResponse,
    MeetingGenerationAnalysisResponse,
    MeetingUsageResponse,
    ModelsResponse,
    NoteSummarizeRequest,
    NoteSummarizeResponse,
    ReadinessComponentResponse,
    ReadinessResponse,
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
from app.services.ollama_client import OllamaClient, OllamaRequestLimiter, OllamaResponseError, OllamaUnavailableError
from app.database import get_db_session
from app.models import Job, VaultChunk, VaultDocument
from app.vault_rag import (
    build_insufficient_sources_answer,
    build_vault_answer_prompt,
    ensure_rag_enabled,
    index_note,
    make_snippet,
    search_vault,
    validate_vault_model,
)
from sqlalchemy import delete, func, select, text

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


@lru_cache
def get_ollama_request_limiter(max_concurrent_requests: int) -> OllamaRequestLimiter:
    return OllamaRequestLimiter(max_concurrent_requests)


def get_ollama_client() -> OllamaClient:
    current_settings = get_settings()
    request_limiter = get_ollama_request_limiter(current_settings.ollama_max_concurrent_requests)
    return OllamaClient(
        base_url=current_settings.ollama_base_url,
        timeout_seconds=current_settings.ollama_timeout_seconds,
        num_ctx=current_settings.ollama_num_ctx,
        keep_alive=current_settings.ollama_keep_alive,
        request_limiter=request_limiter,
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
    request_limiter = get_ollama_request_limiter(current_settings.ollama_max_concurrent_requests)
    return OllamaEmbeddingClient(
        base_url=current_settings.ollama_base_url,
        timeout_seconds=current_settings.ollama_timeout_seconds,
        model=current_settings.rag_embedding_model,
        keep_alive=current_settings.ollama_keep_alive,
        request_limiter=request_limiter,
    )


@app.get("/v1/health", tags=["system"], response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


def check_runtime_components(session: Session) -> tuple[dict[str, bool], int]:
    current_settings = get_settings()
    components = {"gateway": True, "database": False, "redis": False, "worker": False, "ollama": False}
    queue_depth = 0
    try:
        session.execute(text("SELECT 1"))
        components["database"] = True
    except Exception:
        session.rollback()

    redis_client: Redis | None = None
    try:
        redis_client = Redis.from_url(current_settings.redis_url, decode_responses=True)
        components["redis"] = bool(redis_client.ping())
        components["worker"] = redis_client.get(current_settings.worker_heartbeat_key) is not None
        queue_depth = int(redis_client.llen(current_settings.audio_queue_name))
    except Exception:
        pass
    finally:
        if redis_client is not None:
            redis_client.close()

    try:
        with httpx.Client(
            base_url=current_settings.ollama_base_url.rstrip("/"),
            timeout=current_settings.health_dependency_timeout_seconds,
        ) as client:
            response = client.get("/api/tags")
            components["ollama"] = response.status_code == 200
    except httpx.HTTPError:
        pass
    if current_settings.diarization_service_url:
        components["diarization"] = False
        try:
            response = httpx.get(
                f"{current_settings.diarization_service_url.rstrip('/')}/v1/health",
                timeout=current_settings.health_dependency_timeout_seconds,
            )
            payload = response.json() if response.status_code == 200 else {}
            components["diarization"] = payload.get("status") == "ok" if isinstance(payload, dict) else False
        except (httpx.HTTPError, ValueError):
            pass
    return components, queue_depth


@app.get("/v1/health/ready", tags=["system"], response_model=ReadinessResponse)
def readiness(
    token: Annotated[ApiToken, Depends(get_current_token)],
    session: Annotated[Session, Depends(get_db_session)],
) -> ReadinessResponse:
    del token
    components, _ = check_runtime_components(session)
    return ReadinessResponse(
        status="ok" if all(components.values()) else "degraded",
        components={
            name: ReadinessComponentResponse(status="up" if is_up else "down")
            for name, is_up in components.items()
        },
    )


def require_metrics_access(authorization: str | None) -> None:
    expected_token = get_settings().metrics_token.strip()
    if not expected_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    scheme, separator, supplied_token = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not secrets.compare_digest(supplied_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid metrics token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/metrics", tags=["system"], include_in_schema=False)
def metrics(
    session: Annotated[Session, Depends(get_db_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    require_metrics_access(authorization)
    components, queue_depth = check_runtime_components(session)
    counts = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all()) if components["database"] else {}
    processing_jobs = list(session.scalars(select(Job).where(Job.status == "processing"))) if components["database"] else []
    stalled_jobs = sum(
        1
        for job in processing_jobs
        if job_is_stalled(job, stalled_after_seconds=get_settings().job_stalled_after_seconds)
    )
    lines = [
        "# HELP obsidian_ai_component_up Whether an Obsidian AI runtime component is reachable.",
        "# TYPE obsidian_ai_component_up gauge",
        *[f'obsidian_ai_component_up{{component="{name}"}} {1 if is_up else 0}' for name, is_up in components.items()],
        "# HELP obsidian_ai_audio_queue_depth Number of transcription jobs waiting in Redis.",
        "# TYPE obsidian_ai_audio_queue_depth gauge",
        f"obsidian_ai_audio_queue_depth {queue_depth}",
        "# HELP obsidian_ai_jobs Current jobs grouped by status.",
        "# TYPE obsidian_ai_jobs gauge",
        *[
            f'obsidian_ai_jobs{{status="{job_status}"}} {int(counts.get(job_status, 0))}'
            for job_status in ("queued", "processing", "completed", "failed", "cancelled")
        ],
        "# HELP obsidian_ai_jobs_stalled Processing jobs whose worker heartbeat is stale.",
        "# TYPE obsidian_ai_jobs_stalled gauge",
        f"obsidian_ai_jobs_stalled {stalled_jobs}",
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/v1/models", tags=["models"], response_model=ModelsResponse)
def list_models(
    token: Annotated[ApiToken, Depends(require_scope("models:list"))],
) -> ModelsResponse:
    del token
    return ModelsResponse(models=get_settings().allowed_models)


@app.post("/v1/notes/summarize", tags=["notes"], response_model=NoteSummarizeResponse)
async def summarize_note(
    payload: NoteSummarizeRequest,
    token: Annotated[ApiToken, Depends(require_scope_with_quotas("notes:summarize", "llm"))],
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
    token: Annotated[ApiToken, Depends(require_scope_with_quotas("assistant:chat", "llm"))],
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


async def run_meeting_generation(
    prepared_request: PreparedMeetingRequest,
    llm_client: LlmClient,
    *,
    diarization_status: str | None = None,
) -> tuple[str, str, int | None, MeetingGenerationAnalysisResponse | None]:
    if prepared_request.generation_mode == "deep_think":
        sections = build_deep_think_sections(prepared_request, get_settings())
        rendered_sections: list[DeepThinkRenderedSection] = []
        last_model = prepared_request.selected_model
        for section in sections:
            result = await llm_client.generate_meeting(
                model=prepared_request.selected_model,
                title=prepared_request.title,
                transcript_chars=len(section.transcript_excerpt),
                manual_notes_chars=len(section.manual_notes),
                template_chars=prepared_request.template_chars,
                participants=prepared_request.participants,
                system_prompt=build_deep_think_section_system_prompt(
                    resolve_prepared_output_language(prepared_request),
                ),
                user_prompt=build_deep_think_section_user_prompt(prepared_request, section),
            )
            last_model = result.model
            rendered_sections.append(DeepThinkRenderedSection(title=section.title, markdown=result.content))
        return (
            last_model,
            assemble_deep_think_report(
                prepared_request,
                rendered_sections,
                final_cleanup=get_settings().meeting_deep_think_final_cleanup,
            ),
            len(rendered_sections),
            build_meeting_generation_analysis(
                prepared_request,
                sections_count=len(rendered_sections),
                section_titles=[section.title for section in sections],
                diarization_status=diarization_status,
            ),
        )

    user_prompt = prepared_request.user_prompt
    generation_stages: int | None = None
    generation_analysis: MeetingGenerationAnalysisResponse | None = None
    if prepared_request.should_predigest:
        current_settings = get_settings()
        chunks = split_transcript_for_predigest(prepared_request.cleaned_transcript, current_settings)
        briefs: list[str] = []
        for index, transcript_chunk in enumerate(chunks):
            brief = await llm_client.predigest_meeting(
                model=prepared_request.selected_model,
                title=prepared_request.title,
                transcript_chars=len(transcript_chunk),
                manual_notes_chars=min(
                    prepared_request.manual_notes_chars,
                    current_settings.meeting_predigest_manual_notes_max_chars,
                ),
                system_prompt=prepared_request.predigest_system_prompt,
                user_prompt=build_meeting_predigest_chunk_user_prompt(
                    prepared_request,
                    transcript_chunk,
                    chunk_index=index,
                    chunk_count=len(chunks),
                    manual_notes_max_chars=current_settings.meeting_predigest_manual_notes_max_chars,
                ),
            )
            briefs.append(f"### Chronological brief {index + 1}/{len(chunks)}\n\n{brief.content.strip()}")
        user_prompt = build_meeting_user_prompt_from_brief(prepared_request, "\n\n".join(briefs))
        generation_stages = len(chunks) + 1
        generation_analysis = build_meeting_generation_analysis(
            prepared_request,
            sections_count=len(chunks),
            section_titles=[f"Chronological brief {index + 1}" for index in range(len(chunks))],
            diarization_status=diarization_status,
        )
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
    return result.model, result.content, generation_stages, generation_analysis


def build_meeting_generation_analysis(
    prepared_request: PreparedMeetingRequest,
    *,
    sections_count: int | None,
    section_titles: list[str],
    diarization_status: str | None,
) -> MeetingGenerationAnalysisResponse:
    safe_diarization_status = diarization_status if diarization_status in {"disabled", "completed", "failed"} else None
    return MeetingGenerationAnalysisResponse(
        mode=prepared_request.generation_mode,
        sections_count=sections_count,
        section_titles=section_titles,
        transcript_chars=prepared_request.transcript_chars,
        manual_notes_chars=prepared_request.manual_notes_chars,
        template_chars=prepared_request.template_chars,
        output_language=resolve_prepared_output_language(prepared_request),
        diarization_status=safe_diarization_status,
    )


def resolve_prepared_output_language(prepared_request: PreparedMeetingRequest) -> str:
    if "Language instruction: the meeting minutes must be written in English." in prepared_request.system_prompt:
        return "en"
    if "Language instruction: the meeting minutes must be written in French." in prepared_request.system_prompt:
        return "fr"
    return "same_as_meeting"


@app.post("/v1/meetings/generate", tags=["meetings"], response_model=MeetingGenerateResponse)
async def generate_meeting_report(
    payload: MeetingGenerateRequest,
    token: Annotated[ApiToken, Depends(require_scope_with_quotas("meetings:generate", "llm"))],
    llm_client: Annotated[LlmClient, Depends(get_llm_client)],
) -> MeetingGenerateResponse:
    del token
    prepared_request = prepare_meeting_request(payload, get_settings())

    try:
        result_model, result_content, generation_stages, generation_analysis = await run_meeting_generation(prepared_request, llm_client)
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
        model=result_model,
        title=prepared_request.title,
        meeting_markdown=result_content,
        generation_mode=prepared_request.generation_mode,
        generation_stages=generation_stages,
        generation_analysis=generation_analysis,
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
    token: Annotated[ApiToken, Depends(require_scope_with_quotas("meetings:generate", "llm"))],
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
        result_model, result_content, generation_stages, generation_analysis = await run_meeting_generation(
            prepared_request,
            llm_client,
            diarization_status=transcript_payload.get("diarization_status") if isinstance(transcript_payload.get("diarization_status"), str) else None,
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
        model=result_model,
        title=prepared_request.title,
        meeting_markdown=result_content,
        generation_mode=prepared_request.generation_mode,
        generation_stages=generation_stages,
        generation_analysis=generation_analysis,
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
    token: Annotated[ApiToken, Depends(require_scope_with_quotas("vault:index", "embedding"))],
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
    token: Annotated[ApiToken, Depends(require_scope_with_quotas("vault:search", "embedding"))],
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
    token: Annotated[ApiToken, Depends(require_scope_with_quotas("vault:ask", "embedding", "llm"))],
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
    token: Annotated[ApiToken, Depends(require_scope_with_quotas("audio:transcribe", "audio"))],
    session: Annotated[Session, Depends(get_db_session)],
    queue: Annotated[AudioJobQueue, Depends(get_audio_job_queue)],
    transcription_language: Annotated[str, Form()] = "auto",
    diarization_enabled: Annotated[bool, Form()] = False,
) -> AudioTranscriptionQueuedResponse:
    settings = get_settings()
    ensure_audio_job_capacity(
        session,
        user_id=token.user_id,
        max_active_jobs=settings.max_active_audio_jobs_per_user,
    )
    requested_language = validate_transcription_language(transcription_language)
    input_path = await save_uploaded_audio(file, settings)
    job = create_audio_transcription_job(
        session,
        user_id=token.user_id,
        input_path=input_path,
        metadata={
            "transcription_language": requested_language,
            "diarization_enabled": diarization_enabled,
            "original_filename": sanitize_original_filename(file.filename),
        },
    )
    queue.enqueue_audio_transcription(job.id, input_path=input_path, transcription_language=requested_language)
    return AudioTranscriptionQueuedResponse(job_id=job.id, status=job.status)


def build_job_status_response(job) -> JobStatusResponse:
    current_settings = get_settings()
    metadata = decode_job_metadata(job)
    original_filename = metadata.get("original_filename")
    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        type=job.type,
        display_name=str(original_filename) if isinstance(original_filename, str) else None,
        phase=job.phase or job.status,
        progress=job.progress or 0,
        progress_message=job.progress_message,
        attempts=job.attempts or 0,
        stalled=job_is_stalled(job, stalled_after_seconds=current_settings.job_stalled_after_seconds),
        cancel_requested=job.cancel_requested_at is not None,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        started_at=job.started_at.isoformat() if job.started_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
        heartbeat_at=job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        error=job.error,
    )


@app.get("/v1/jobs", tags=["jobs"], response_model=JobListResponse)
def list_jobs(
    token: Annotated[ApiToken, Depends(get_current_token)],
    session: Annotated[Session, Depends(get_db_session)],
    limit: int | None = Query(default=None, ge=1, le=200),
) -> JobListResponse:
    selected_limit = min(limit or get_settings().job_history_limit, get_settings().job_history_limit)
    jobs = list_jobs_for_user(session, user_id=token.user_id, limit=selected_limit)
    return JobListResponse(jobs=[build_job_status_response(job) for job in jobs])


@app.get("/v1/jobs/{job_id}", tags=["jobs"], response_model=JobStatusResponse)
def get_job(
    job_id: str,
    token: Annotated[ApiToken, Depends(get_current_token)],
    session: Annotated[Session, Depends(get_db_session)],
) -> JobStatusResponse:
    job = require_job_for_user(session, job_id=job_id, user_id=token.user_id)
    return build_job_status_response(job)


@app.post("/v1/jobs/{job_id}/cancel", tags=["jobs"], response_model=JobCancelResponse)
def cancel_job(
    job_id: str,
    token: Annotated[ApiToken, Depends(get_current_token)],
    session: Annotated[Session, Depends(get_db_session)],
) -> JobCancelResponse:
    job = require_job_for_user(session, job_id=job_id, user_id=token.user_id)
    job = request_job_cancellation(session, job)
    return JobCancelResponse(
        job_id=job.id,
        status=job.status,
        cancel_requested=job.cancel_requested_at is not None,
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
