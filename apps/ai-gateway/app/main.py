from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.audio import save_uploaded_audio
from app.auth import get_current_token, require_scope
from app.config import get_settings
from app.database import init_db
from app.models import ApiToken
from app.jobs import (
    JOB_STATUS_COMPLETED,
    create_audio_transcription_job,
    read_transcript_result,
    require_job_for_user,
)
from app.meetings import (
    extract_transcript_text_from_result,
    prepare_meeting_from_job_request,
    prepare_meeting_request,
    validate_audio_job_for_meeting,
)
from app.notes import prepare_summary_request
from app.queue import AudioJobQueue, get_audio_job_queue
from app.schemas import (
    AudioTranscriptionQueuedResponse,
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
)
from app.services.ollama_client import OllamaClient, OllamaResponseError, OllamaUnavailableError
from app.database import get_db_session

settings = get_settings()

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


def get_ollama_client() -> OllamaClient:
    current_settings = get_settings()
    return OllamaClient(
        base_url=current_settings.ollama_base_url,
        timeout_seconds=current_settings.ollama_timeout_seconds,
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
    ollama_client: Annotated[OllamaClient, Depends(get_ollama_client)],
) -> NoteSummarizeResponse:
    del token
    prepared_request = prepare_summary_request(payload, get_settings())

    try:
        result = await ollama_client.summarize_markdown(
            model=prepared_request.selected_model,
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


@app.post("/v1/meetings/generate", tags=["meetings"], response_model=MeetingGenerateResponse)
async def generate_meeting_report(
    payload: MeetingGenerateRequest,
    token: Annotated[ApiToken, Depends(require_scope("meetings:generate"))],
    ollama_client: Annotated[OllamaClient, Depends(get_ollama_client)],
) -> MeetingGenerateResponse:
    del token
    prepared_request = prepare_meeting_request(payload, get_settings())

    try:
        result = await ollama_client.summarize_markdown(
            model=prepared_request.selected_model,
            system_prompt=prepared_request.system_prompt,
            user_prompt=prepared_request.user_prompt,
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
    ollama_client: Annotated[OllamaClient, Depends(get_ollama_client)],
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
        result = await ollama_client.summarize_markdown(
            model=prepared_request.selected_model,
            system_prompt=prepared_request.system_prompt,
            user_prompt=prepared_request.user_prompt,
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


@app.post("/v1/audio/transcribe", tags=["audio"], response_model=AudioTranscriptionQueuedResponse)
async def transcribe_audio(
    file: Annotated[UploadFile, File(...)],
    token: Annotated[ApiToken, Depends(require_scope("audio:transcribe"))],
    session: Annotated[Session, Depends(get_db_session)],
    queue: Annotated[AudioJobQueue, Depends(get_audio_job_queue)],
) -> AudioTranscriptionQueuedResponse:
    settings = get_settings()
    input_path = await save_uploaded_audio(file, settings)
    job = create_audio_transcription_job(session, user_id=token.user_id, input_path=input_path)
    queue.enqueue_audio_transcription(job.id)
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
