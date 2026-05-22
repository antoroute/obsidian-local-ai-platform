from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status

from app.auth import require_scope
from app.config import get_settings
from app.database import init_db
from app.models import ApiToken
from app.notes import prepare_summary_request
from app.schemas import HealthResponse, ModelsResponse, NoteSummarizeRequest, NoteSummarizeResponse, UsageResponse
from app.services.ollama_client import OllamaClient, OllamaResponseError, OllamaUnavailableError

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
