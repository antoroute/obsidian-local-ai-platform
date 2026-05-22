from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status

from app.config import Settings
from app.schemas import NoteSummarizeRequest

SYSTEM_PROMPT = """You are a careful assistant that summarizes Obsidian notes into structured Markdown.
Never invent facts that are not supported by the source note.
When information is missing, uncertain, ambiguous, or implied, say so explicitly.
Follow the user-provided template exactly when it is present.
Keep the output concise, readable, and grounded in the provided note only."""


@dataclass(frozen=True)
class PreparedSummaryRequest:
    title: str
    selected_model: str
    system_prompt: str
    user_prompt: str
    prompt_chars: int
    template_chars: int


def prepare_summary_request(payload: NoteSummarizeRequest, settings: Settings) -> PreparedSummaryRequest:
    note_content = payload.note_content.strip()
    template = payload.template.strip()

    if not note_content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="note_content must not be empty.",
        )

    if len(note_content) > settings.max_note_chars:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"note_content exceeds the maximum of {settings.max_note_chars} characters.",
        )

    if len(template) > settings.max_template_chars:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"template exceeds the maximum of {settings.max_template_chars} characters.",
        )

    selected_model = payload.model or settings.default_model
    if selected_model not in settings.allowed_models:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requested model is not allowed.",
        )

    user_prompt = build_user_prompt(title=payload.title.strip(), note_content=note_content, template=template)
    return PreparedSummaryRequest(
        title=payload.title,
        selected_model=selected_model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        prompt_chars=len(note_content),
        template_chars=len(template),
    )


def build_user_prompt(*, title: str, note_content: str, template: str) -> str:
    title_block = title if title else "(untitled note)"
    template_block = template if template else "No custom template was provided. Return a structured Markdown summary."

    return (
        "Summarize the following Obsidian note into structured Markdown.\n\n"
        f"Note title:\n{title_block}\n\n"
        "Template or instructions:\n"
        f"{template_block}\n\n"
        "Source note Markdown:\n"
        f"{note_content}\n"
    )
