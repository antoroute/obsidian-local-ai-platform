from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status

from app.config import Settings
from app.schemas import MeetingGenerateRequest

MEETING_SYSTEM_PROMPT = """You are a careful assistant that produces structured Markdown meeting reports.
Use the transcript as the primary source for meeting flow and chronology.
Use manual notes as the priority source for names, acronyms, dates, decisions, and action items when they conflict or add precision.
Never invent facts that are not supported by the transcript or manual notes.
Explicitly identify uncertainties, contradictions, and missing information.
Follow the provided template exactly while ensuring the final Markdown includes sections for:
Resume executif, Participants, Sujets abordes, Decisions prises, Actions a suivre, Points ouverts, Risques / blocages, Incertitudes ou contradictions, Annexes / notes complementaires if useful."""


@dataclass(frozen=True)
class PreparedMeetingRequest:
    title: str
    selected_model: str
    system_prompt: str
    user_prompt: str
    transcript_chars: int
    manual_notes_chars: int
    template_chars: int
    participants_count: int


def prepare_meeting_request(payload: MeetingGenerateRequest, settings: Settings) -> PreparedMeetingRequest:
    title = payload.title.strip()
    template = payload.template.strip()
    transcript = (payload.transcript or "").strip()
    manual_notes = (payload.manual_notes or "").strip()
    participants = [participant.strip() for participant in payload.participants if participant.strip()]

    if not title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="title must not be empty.")
    if not template:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="template must not be empty.")
    if not transcript and not manual_notes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one of transcript or manual_notes must be provided.",
        )
    if len(transcript) > settings.max_transcript_chars:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"transcript exceeds the maximum of {settings.max_transcript_chars} characters.",
        )
    if len(manual_notes) > settings.max_manual_notes_chars:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"manual_notes exceeds the maximum of {settings.max_manual_notes_chars} characters.",
        )
    if len(template) > settings.max_template_chars:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"template exceeds the maximum of {settings.max_template_chars} characters.",
        )
    if len(participants) > settings.max_participants:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"participants exceeds the maximum of {settings.max_participants} entries.",
        )

    selected_model = payload.model or settings.default_model
    if selected_model not in settings.allowed_models:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requested model is not allowed.")

    return PreparedMeetingRequest(
        title=title,
        selected_model=selected_model,
        system_prompt=MEETING_SYSTEM_PROMPT,
        user_prompt=build_meeting_user_prompt(
            title=title,
            transcript=transcript,
            manual_notes=manual_notes,
            participants=participants,
            template=template,
        ),
        transcript_chars=len(transcript),
        manual_notes_chars=len(manual_notes),
        template_chars=len(template),
        participants_count=len(participants),
    )


def build_meeting_user_prompt(
    *,
    title: str,
    transcript: str,
    manual_notes: str,
    participants: list[str],
    template: str,
) -> str:
    transcript_block = transcript or "No transcript was provided."
    manual_notes_block = manual_notes or "No manual notes were provided."
    participants_block = ", ".join(participants) if participants else "No participant list was provided."

    return (
        "Generate a structured Markdown meeting report from the following material.\n\n"
        f"Meeting title:\n{title}\n\n"
        "Participants:\n"
        f"{participants_block}\n\n"
        "Template or instructions:\n"
        f"{template}\n\n"
        "Manual notes (priority source for names, acronyms, dates, decisions, and action items):\n"
        f"{manual_notes_block}\n\n"
        "Transcript (primary source for chronology and discussion flow):\n"
        f"{transcript_block}\n"
    )
