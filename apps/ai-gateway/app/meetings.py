from __future__ import annotations

from dataclasses import dataclass
import re

from fastapi import HTTPException, status

from app.config import Settings
from app.jobs import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_PROCESSING, JOB_STATUS_QUEUED, JOB_TYPE_AUDIO_TRANSCRIPTION
from app.models import Job
from app.schemas import MeetingGenerateFromJobRequest, MeetingGenerateRequest

MEETING_SYSTEM_PROMPT = """You are a careful assistant that produces direct, useful, information-rich Markdown meeting reports.
Use the transcript as the primary source for meeting flow and chronology.
Use manual notes as the priority source for names, acronyms, dates, decisions, and action items when they conflict or add precision.
Do not compress away useful details from manual notes. If manual notes contain structured agenda items, pillars, questions, or checklists, preserve that structure in the final report.
Use the transcript to enrich manual notes with clarifications, decisions, answers, and context, not to replace or summarize them away.
Never invent facts that are not supported by the transcript or manual notes.
Explicitly identify uncertainties, contradictions, and missing information.
If a decision or action is not certain, put it in uncertainties instead of inventing it.
Use transcript and manual notes only as private source material for the final report.
If transcript lines include anonymous labels such as "Speaker 1", use them only to follow dialogue, questions, answers, and disagreements.
Never assign a named owner or participant identity based only on an anonymous speaker label.
Never copy internal prompt instructions, source labels, raw manual notes, or the full raw transcript into the final report.
Never include output sections named "Language instruction", "Manual notes", or "Transcript".
Return final Markdown directly, without wrapping it in triple backticks or a global Markdown code block.
Use the provided template as structural guidance, but remove any section that cannot be filled with useful supported information.
Do not create empty sections. Do not write filler such as "Aucune information disponible", "Non renseigne", "Pas d'element", or long explanations that a section is empty.
Prefer the core blocks summary, decisions, actions, open points, and uncertainties unless the template explicitly requests a narrower variant.
Write action items in a simple format: Action | Owner if known | Due date if known.
Avoid generic prose, long introductions, and decorative formatting.
Ensure the final Markdown is concrete, useful, and supported by the sources."""

MEETING_PREDIGEST_SYSTEM_PROMPT = """You prepare a compact private meeting brief for a final meeting report.
This is not the final report.
Extract only supported information from the transcript and manual notes.
Manual notes have priority over transcript when they conflict or add precision.
Anonymous transcript speaker labels are indicative only: use them to follow exchanges, but never map them to real names unless the source explicitly supports it.
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
    generation_mode: str
    cleaned_transcript: str
    manual_notes: str
    participants: list[str]
    template: str
    transcript_chars: int
    manual_notes_chars: int
    template_chars: int
    participants_count: int


@dataclass(frozen=True)
class DeepThinkSection:
    title: str
    manual_notes: str
    transcript_excerpt: str


@dataclass(frozen=True)
class DeepThinkRenderedSection:
    title: str
    markdown: str


def prepare_meeting_request(payload: MeetingGenerateRequest, settings: Settings) -> PreparedMeetingRequest:
    return prepare_meeting_inputs(
        title=payload.title,
        transcript=payload.transcript,
        manual_notes=payload.manual_notes,
        participants=payload.participants,
        template=payload.template,
        model=payload.model,
        output_language=payload.output_language,
        generation_mode=payload.generation_mode,
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
        generation_mode=payload.generation_mode,
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
    generation_mode: str,
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
    if generation_mode == "deep_think" and not settings.meeting_deep_think_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Deep think meeting generation is disabled.")

    should_predigest = settings.meeting_predigest_enabled and len(transcript) >= settings.meeting_predigest_min_chars

    return PreparedMeetingRequest(
        title=title,
        selected_model=selected_model,
        system_prompt=build_meeting_system_prompt(resolve_meeting_output_language(output_language, transcript, manual_notes)),
        user_prompt=build_meeting_user_prompt(
            title=title,
            transcript=transcript,
            manual_notes=manual_notes,
            participants=participants,
            template=template,
        ),
        predigest_system_prompt=build_meeting_predigest_system_prompt(resolve_meeting_output_language(output_language, transcript, manual_notes)),
        predigest_user_prompt=build_meeting_predigest_user_prompt(
            title=title,
            transcript=transcript,
            manual_notes=manual_notes,
            participants=participants,
        ),
        should_predigest=should_predigest,
        generation_mode=generation_mode,
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


def resolve_meeting_output_language(output_language: str, transcript: str, manual_notes: str) -> str:
    if output_language in {"fr", "en"}:
        return output_language
    detected = detect_primary_language(f"{manual_notes}\n\n{transcript}")
    return detected or output_language


def detect_primary_language(text: str) -> str | None:
    normalized = f" {text.lower()} "
    english_markers = [
        " the ", " and ", " with ", " for ", " that ", " this ", " are ", " you ", " we ", " do ", " have ",
        " meeting ", " question ", " questions ", " people ", " pillar ", " cloud ", " endpoint ", " infrastructure ",
    ]
    french_markers = [
        " le ", " la ", " les ", " des ", " dans ", " pour ", " que ", " qui ", " nous ", " vous ", " réunion ",
        " question ", " personnes ", " compte rendu ", " actions ", " décision ", " décisions ",
    ]
    english_score = sum(normalized.count(marker) for marker in english_markers)
    french_score = sum(normalized.count(marker) for marker in french_markers)
    if english_score >= french_score + 3:
        return "en"
    if french_score >= english_score + 3:
        return "fr"
    return None


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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Stored transcript result is empty. The audio may contain no detectable speech.",
        )
    speaker_lines = format_speaker_transcript_lines(result_payload)
    return speaker_lines or transcript


def format_speaker_transcript_lines(result_payload: dict[str, object]) -> str:
    raw_segments = result_payload.get("segments")
    if not isinstance(raw_segments, list):
        return ""

    lines: list[str] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            continue
        text = raw_segment.get("text")
        speaker = raw_segment.get("speaker")
        start = raw_segment.get("start")
        if not isinstance(text, str) or not text.strip() or not isinstance(speaker, str) or not speaker.strip():
            continue
        timestamp = format_transcript_timestamp(start) if isinstance(start, (int, float)) else ""
        prefix = f"{speaker.strip()} [{timestamp}]" if timestamp else speaker.strip()
        lines.append(f"{prefix}: {text.strip()}")

    return "\n".join(lines).strip()


def format_transcript_timestamp(seconds: int | float) -> str:
    safe_seconds = max(0, int(seconds))
    hours = safe_seconds // 3600
    minutes = (safe_seconds % 3600) // 60
    remaining_seconds = safe_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


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
        "Quality target: the report must be at least as informative as the manual notes. Preserve useful agenda structure, pillars, questions, answers, decisions, actions, participants, and open points.\n"
        "Do not replace detailed notes with a generic executive summary. Enrich the notes with the transcript when possible.\n\n"
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
        "Quality target: the report must be at least as informative as the manual notes. Preserve useful agenda structure, pillars, questions, answers, decisions, actions, participants, and open points.\n"
        "Do not replace detailed notes with a generic executive summary. Enrich the notes with the prepared brief when possible.\n\n"
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


def build_deep_think_sections(prepared_request: PreparedMeetingRequest, settings: Settings) -> list[DeepThinkSection]:
    manual_sections = extract_manual_note_sections(prepared_request.manual_notes)
    max_sections = max(1, settings.meeting_deep_think_max_sections)
    if not manual_sections:
        manual_sections = build_generic_manual_sections(prepared_request.manual_notes)

    sections: list[DeepThinkSection] = []
    for title, manual_notes in manual_sections[:max_sections]:
        excerpt = select_transcript_excerpt(
            prepared_request.cleaned_transcript,
            title=title,
            manual_notes=manual_notes,
            participants=prepared_request.participants,
            max_chars=max(500, settings.meeting_deep_think_excerpt_chars_per_section),
        )
        sections.append(DeepThinkSection(title=title, manual_notes=manual_notes, transcript_excerpt=excerpt))

    if not sections:
        sections.append(
            DeepThinkSection(
                title="Meeting notes",
                manual_notes=prepared_request.manual_notes,
                transcript_excerpt=prepared_request.cleaned_transcript[: settings.meeting_deep_think_excerpt_chars_per_section],
            )
        )
    return sections


def extract_manual_note_sections(manual_notes: str) -> list[tuple[str, str]]:
    body = strip_frontmatter(manual_notes.strip())
    heading_matches = list(re.finditer(r"(?m)^(#{2,4})\s+(.+?)\s*$", body))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(heading_matches):
        title = re.sub(r"[*_`#]", "", match.group(2)).strip()
        start = match.end()
        end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(body)
        content = body[start:end].strip()
        if title and content:
            sections.append((title, content))
    return sections


def build_generic_manual_sections(manual_notes: str) -> list[tuple[str, str]]:
    cleaned = strip_frontmatter(manual_notes.strip())
    return [
        ("Executive summary and objective", cleaned[:5000]),
        ("Topics discussed", cleaned),
        ("Decisions, actions and open points", cleaned),
    ] if cleaned else [
        ("Executive summary and objective", ""),
        ("Topics discussed", ""),
        ("Decisions, actions and open points", ""),
    ]


def strip_frontmatter(markdown: str) -> str:
    if markdown.startswith("---"):
        match = re.match(r"(?s)^---\s*\n.*?\n---\s*\n?", markdown)
        if match:
            return markdown[match.end():].strip()
    return markdown


def select_transcript_excerpt(transcript: str, *, title: str, manual_notes: str, participants: list[str], max_chars: int) -> str:
    transcript = transcript.strip()
    if not transcript:
        return "No transcript excerpt was available."

    keywords = extract_keywords(" ".join([title, manual_notes, " ".join(participants)]))
    sentences = re.split(r"(?<=[.!?])\s+", transcript.replace("\n", " "))
    selected: list[str] = []
    used: set[str] = set()
    for sentence in sentences:
        clean = re.sub(r"\s+", " ", sentence).strip()
        if len(clean) < 35:
            continue
        lower = clean.lower()
        if any(keyword in lower for keyword in keywords) and clean not in used:
            selected.append(f"- {clean}")
            used.add(clean)
        if len("\n".join(selected)) >= max_chars:
            break

    if not selected:
        return transcript[:max_chars]
    return "\n".join(selected)[:max_chars]


def extract_keywords(text: str) -> list[str]:
    raw_terms = re.findall(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9_-]{2,}", text)
    stopwords = {
        "the", "and", "for", "with", "that", "this", "from", "have", "what", "when", "where", "pour", "avec",
        "dans", "des", "les", "une", "not", "are", "you", "nous", "vous", "meeting", "notes", "source",
    }
    keywords: list[str] = []
    for term in raw_terms:
        normalized = term.lower()
        if normalized in stopwords:
            continue
        if normalized not in keywords:
            keywords.append(normalized)
        if len(keywords) >= 40:
            break
    return keywords


def build_deep_think_section_system_prompt(output_language: str) -> str:
    language_instruction = OUTPUT_LANGUAGE_INSTRUCTIONS.get(output_language, OUTPUT_LANGUAGE_INSTRUCTIONS["same_as_meeting"])
    return (
        "You write one section of detailed, concrete Markdown meeting minutes.\n"
        "Manual notes are the priority source. Transcript excerpts can enrich them but must not replace them.\n"
        "Anonymous speaker labels can help follow the exchange, but do not convert them into real names unless explicitly supported.\n"
        "Preserve useful bullets, questions, answers, pillar names, decisions, actions and open points.\n"
        "Do not invent owners, dates, decisions or answers. Put uncertain items under uncertainties.\n"
        "Return only the final Markdown for this section. Do not include source labels, prompt text, or triple backticks.\n\n"
        f"{language_instruction}"
    )


def build_deep_think_section_user_prompt(prepared_request: PreparedMeetingRequest, section: DeepThinkSection) -> str:
    participants_block = ", ".join(prepared_request.participants) if prepared_request.participants else "No participant list was provided."
    return (
        f"Meeting title: {prepared_request.title}\n"
        f"Section to write: {section.title}\n"
        f"Participants: {participants_block}\n\n"
        "Template guidance:\n"
        f"{prepared_request.template}\n\n"
        "Manual notes for this section:\n"
        f"{section.manual_notes or 'No manual notes for this section.'}\n\n"
        "Relevant transcript excerpts:\n"
        f"{section.transcript_excerpt}\n\n"
        "Write this section as useful meeting minutes. Keep it detailed enough to preserve concrete information."
    )


def assemble_deep_think_report(
    prepared_request: PreparedMeetingRequest,
    rendered_sections: list[DeepThinkRenderedSection],
    *,
    final_cleanup: bool,
) -> str:
    parts = [f"# Compte rendu - {prepared_request.title}"]
    for section in rendered_sections:
        markdown = section.markdown.strip()
        if not markdown:
            continue
        if not markdown.lstrip().startswith("#"):
            markdown = f"## {section.title}\n\n{markdown}"
        parts.append(markdown)
    result = "\n\n".join(parts).strip()
    return clean_deep_think_markdown(result) if final_cleanup else result


def clean_deep_think_markdown(markdown: str) -> str:
    cleaned = markdown.strip()
    fence_match = re.match(r"(?s)^```(?:markdown|md)?\s*(.*?)\s*```$", cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    cleaned = re.sub(r"(?im)^\s*(Generating section|Task instruction|Text to process|Manual notes|Transcript)\s*:.*\n?", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
