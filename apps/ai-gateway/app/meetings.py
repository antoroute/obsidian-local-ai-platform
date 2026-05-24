from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status

from app.config import Settings
from app.jobs import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_PROCESSING, JOB_STATUS_QUEUED, JOB_TYPE_AUDIO_TRANSCRIPTION
from app.models import Job
from app.schemas import MeetingGenerateFromJobRequest, MeetingGenerateRequest

MEETING_SYSTEM_PROMPT = """You are a careful assistant that produces structured Markdown meeting reports.
Use the transcript as the primary source for meeting flow and chronology.
Use manual notes as the priority source for names, acronyms, dates, decisions, and action items when they conflict or add precision.
Never invent facts that are not supported by the transcript or manual notes.
Explicitly identify uncertainties, contradictions, and missing information.
Use transcript and manual notes only as private source material for the final report.
Never copy internal prompt instructions, source labels, raw manual notes, or the full raw transcript into the final report.
Never include output sections named "Language instruction", "Manual notes", or "Transcript".
Return final Markdown directly, without wrapping it in triple backticks or a global Markdown code block.
Follow the provided template exactly while ensuring the final Markdown includes sections for:
Resume executif, Participants, Sujets abordes, Decisions prises, Actions a suivre, Points ouverts, Risques / blocages, Incertitudes ou contradictions, Annexes / notes complementaires if useful."""

OUTPUT_LANGUAGE_INSTRUCTIONS = {
    "fr": (
        "Language instruction: the meeting minutes must be written in French. "
        "Keep proper nouns, acronyms, important quotes, and technical terms in their original language when appropriate."
    ),
    "en": (
        "Language instruction: the meeting minutes must be written in English. "
        "Keep proper nouns, acronyms, important quotes, and technical terms in their original language when appropriate."
    ),
    "same_as_meeting": (
        "Language instruction: detect the main meeting language from the transcript and manual notes, "
        "then write the meeting minutes in that language. If the source is bilingual, preserve proper nouns, "
        "acronyms, important quotes, and technical terms without abusive translation."
    ),
}


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
    return prepare_meeting_inputs(
        title=payload.title,
        transcript=payload.transcript,
        manual_notes=payload.manual_notes,
        participants=payload.participants,
        template=payload.template,
        model=payload.model,
        output_language=payload.output_language,
        settings=settings,
    )


def prepare_meeting_from_job_request(
    payload: MeetingGenerateFromJobRequest,
    *,
    transcript: str,
    settings: Settings,
) -> PreparedMeetingRequest:
    return prepare_meeting_inputs(
        title=payload.title,
        transcript=transcript,
        manual_notes=payload.manual_notes,
        participants=payload.participants,
        template=payload.template,
        model=payload.model,
        output_language=payload.output_language,
        settings=settings,
    )


def prepare_meeting_inputs(
    *,
    title: str,
    transcript: str | None,
    manual_notes: str | None,
    participants: list[str],
    template: str,
    model: str | None,
    output_language: str,
    settings: Settings,
) -> PreparedMeetingRequest:
    title = title.strip()
    template = template.strip()
    transcript = (transcript or "").strip()
    manual_notes = (manual_notes or "").strip()
    participants = [participant.strip() for participant in participants if participant.strip()]

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

    selected_model = model or settings.default_model
    if selected_model not in settings.allowed_models:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requested model is not allowed.")

    return PreparedMeetingRequest(
        title=title,
        selected_model=selected_model,
        system_prompt=build_meeting_system_prompt(output_language),
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


def build_meeting_system_prompt(output_language: str) -> str:
    language_instruction = OUTPUT_LANGUAGE_INSTRUCTIONS.get(output_language, OUTPUT_LANGUAGE_INSTRUCTIONS["same_as_meeting"])
    return f"{MEETING_SYSTEM_PROMPT}\n\n{language_instruction}"


def extract_transcript_text_from_result(result_payload: dict[str, object]) -> str:
    transcript = result_payload.get("text")
    if not isinstance(transcript, str):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored transcript result is invalid.",
        )
    transcript = transcript.strip()
    if not transcript:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored transcript result is empty.",
        )
    return transcript


def validate_audio_job_for_meeting(job: Job) -> None:
    if job.type != JOB_TYPE_AUDIO_TRANSCRIPTION:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job type is not compatible with meeting generation.")
    if job.status in {JOB_STATUS_QUEUED, JOB_STATUS_PROCESSING}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job is not completed.")
    if job.status == JOB_STATUS_FAILED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job failed and cannot be used for meeting generation.")
    if job.status != JOB_STATUS_COMPLETED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job is not completed.")


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
