from __future__ import annotations

from dataclasses import dataclass
import re

from fastapi import HTTPException, status

from app.config import Settings
from app.jobs import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_PROCESSING, JOB_STATUS_QUEUED, JOB_TYPE_AUDIO_TRANSCRIPTION
from app.models import Job
from app.schemas import MeetingGenerateFromJobRequest, MeetingGenerateRequest

MEETING_SYSTEM_PROMPT = """You are a careful assistant that produces direct, useful, compact Markdown meeting reports.
Use the transcript as the primary source for meeting flow and chronology.
Use manual notes as the priority source for names, acronyms, dates, decisions, and action items when they conflict or add precision.
Never invent facts that are not supported by the transcript or manual notes.
Explicitly identify uncertainties, contradictions, and missing information.
If a decision or action is not certain, put it in uncertainties instead of inventing it.
Use transcript and manual notes only as private source material for the final report.
Never copy internal prompt instructions, source labels, raw manual notes, or the full raw transcript into the final report.
Never include output sections named "Language instruction", "Manual notes", or "Transcript".
Return final Markdown directly, without wrapping it in triple backticks or a global Markdown code block.
Use the provided template as structural guidance, but remove any section that cannot be filled with useful supported information.
Do not create empty sections. Do not write filler such as "Aucune information disponible", "Non renseigne", "Pas d'element", or long explanations that a section is empty.
Prefer the core blocks summary, decisions, actions, open points, and uncertainties unless the template explicitly requests a narrower variant.
Write action items in a simple format: Action | Owner if known | Due date if known.
Avoid generic prose, long introductions, and decorative formatting.
Ensure the final Markdown remains compact, useful, and supported by the sources."""

MEETING_PREDIGEST_SYSTEM_PROMPT = """You prepare a compact private meeting brief for a final meeting report.
This is not the final report.
Extract only supported information from the transcript and manual notes.
Manual notes have priority over transcript when they conflict or add precision.
Return compact Markdown with:
- Themes
- Confirmed decisions
- Candidate actions
- People, organizations, products, and acronyms
- Open questions and uncertainties
Do not invent. Do not include raw transcript. Do not use triple backticks."""

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
    predigest_system_prompt: str
    predigest_user_prompt: str
    should_predigest: bool
    cleaned_transcript: str
    manual_notes: str
    participants: list[str]
    template: str
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
    transcript = clean_meeting_transcript(transcript or "", enabled=settings.meeting_transcript_cleanup_enabled)
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

    should_predigest = settings.meeting_predigest_enabled and len(transcript) >= settings.meeting_predigest_min_chars

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
        predigest_system_prompt=build_meeting_predigest_system_prompt(output_language),
        predigest_user_prompt=build_meeting_predigest_user_prompt(
            title=title,
            transcript=transcript,
            manual_notes=manual_notes,
            participants=participants,
        ),
        should_predigest=should_predigest,
        cleaned_transcript=transcript,
        manual_notes=manual_notes,
        participants=participants,
        template=template,
        transcript_chars=len(transcript),
        manual_notes_chars=len(manual_notes),
        template_chars=len(template),
        participants_count=len(participants),
    )


def build_meeting_system_prompt(output_language: str) -> str:
    language_instruction = OUTPUT_LANGUAGE_INSTRUCTIONS.get(output_language, OUTPUT_LANGUAGE_INSTRUCTIONS["same_as_meeting"])
    return f"{MEETING_SYSTEM_PROMPT}\n\n{language_instruction}"


def build_meeting_predigest_system_prompt(output_language: str) -> str:
    language_instruction = OUTPUT_LANGUAGE_INSTRUCTIONS.get(output_language, OUTPUT_LANGUAGE_INSTRUCTIONS["same_as_meeting"])
    return f"{MEETING_PREDIGEST_SYSTEM_PROMPT}\n\n{language_instruction}"


def clean_meeting_transcript(transcript: str, *, enabled: bool = True) -> str:
    original = transcript.strip()
    if not enabled or not original:
        return original

    normalized = original.replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines: list[str] = []
    previous_key = ""
    filler_only = {"euh", "heu", "hum", "hmm", "uh", "um", "..."}

    for raw_line in normalized.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            continue
        key = line.lower()
        if key == previous_key:
            continue
        if key.strip(" .,!?:;").lower() in filler_only:
            continue
        cleaned_lines.append(line)
        previous_key = key

    cleaned = "\n".join(cleaned_lines).strip()
    return cleaned or original


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
        "Generate a direct, useful Markdown meeting report from the following material.\n"
        "Priority order: manual notes > transcript > participants > template.\n"
        "Use the template as guidance, not as a checklist of mandatory empty sections.\n"
        "If a section has no useful supported content, remove it.\n\n"
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


def build_meeting_predigest_user_prompt(
    *,
    title: str,
    transcript: str,
    manual_notes: str,
    participants: list[str],
) -> str:
    transcript_block = transcript or "No transcript was provided."
    manual_notes_block = manual_notes or "No manual notes were provided."
    participants_block = ", ".join(participants) if participants else "No participant list was provided."
    return (
        "Prepare a compact private brief for the final meeting report.\n"
        "This brief must reduce noise and keep only supported, useful facts.\n"
        "Do not write the final meeting report yet.\n\n"
        f"Meeting title:\n{title}\n\n"
        "Participants:\n"
        f"{participants_block}\n\n"
        "Manual notes (priority source for names, acronyms, dates, decisions, and action items):\n"
        f"{manual_notes_block}\n\n"
        "Transcript (primary source for chronology and discussion flow):\n"
        f"{transcript_block}\n"
    )


def build_meeting_user_prompt_from_brief(prepared_request: PreparedMeetingRequest, brief: str) -> str:
    brief_block = brief.strip() or "No prepared meeting brief was produced."
    participants_block = ", ".join(prepared_request.participants) if prepared_request.participants else "No participant list was provided."
    manual_notes_block = prepared_request.manual_notes or "No manual notes were provided."
    return (
        "Generate a direct, useful Markdown meeting report from the prepared meeting brief and source hints.\n"
        "Priority order: manual notes > prepared meeting brief > participants > template.\n"
        "Use the template as guidance, not as a checklist of mandatory empty sections.\n"
        "If a section has no useful supported content, remove it.\n"
        "Do not mention the predigest step in the final report.\n\n"
        f"Meeting title:\n{prepared_request.title}\n\n"
        "Participants:\n"
        f"{participants_block}\n\n"
        "Template or instructions:\n"
        f"{prepared_request.template}\n\n"
        "Manual notes (priority source for names, acronyms, dates, decisions, and action items):\n"
        f"{manual_notes_block}\n\n"
        "Prepared meeting brief (compressed source derived from the transcript):\n"
        f"{brief_block}\n"
    )
