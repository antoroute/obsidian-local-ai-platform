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
Write every confirmed action as an Obsidian-compatible task: `- [ ] Concrete action — @Owner 📅 YYYY-MM-DD`.
Omit owner or due date when unknown; never invent either. Keep uncertain candidate actions under uncertainties, not in the task list.
Avoid generic prose, long introductions, and decorative formatting.
Ensure the final Markdown is concrete, useful, and supported by the sources."""

MEETING_PREDIGEST_SYSTEM_PROMPT = """You prepare a compact private meeting brief for a final meeting report.
This is not the final report.
Extract only supported information from the transcript and manual notes.
Manual notes have priority over transcript when they conflict or add precision.
Anonymous transcript speaker labels are indicative only: use them to follow exchanges, but never map them to real names unless the source explicitly supports it.
Return at most 350 words of compact Markdown with:
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


DEEP_THINK_CORE_SECTIONS = [
    "Resume detaille",
    "Sujets abordes",
    "Decisions",
    "Actions",
    "Points ouverts et incertitudes",
    "Participants et references utiles",
]

SOURCE_NOTE_SKELETON_TITLES = {"notes", "resume", "résumé", "actions", "personnes rencontrees", "personnes rencontrées"}


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


def split_transcript_for_predigest(transcript: str, settings: Settings) -> list[str]:
    text = transcript.strip()
    if not text:
        return []
    target_size = max(
        settings.meeting_predigest_chunk_chars,
        (len(text) + settings.meeting_predigest_max_chunks - 1) // settings.meeting_predigest_max_chunks + 1200,
    )
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        proposed_end = min(len(text), cursor + target_size)
        end = proposed_end
        if proposed_end < len(text):
            search_start = max(cursor + target_size // 2, proposed_end - 1200)
            boundary_candidates = [
                text.rfind("\n", search_start, proposed_end),
                text.rfind(". ", search_start, proposed_end),
                text.rfind("? ", search_start, proposed_end),
                text.rfind("! ", search_start, proposed_end),
            ]
            best_boundary = max(boundary_candidates)
            if best_boundary > cursor:
                end = best_boundary + 1
        chunk = text[cursor:end].strip()
        if chunk:
            chunks.append(chunk)
        cursor = max(end, cursor + 1)
    return chunks


def build_meeting_predigest_chunk_user_prompt(
    prepared_request: PreparedMeetingRequest,
    transcript_chunk: str,
    *,
    chunk_index: int,
    chunk_count: int,
    manual_notes_max_chars: int,
) -> str:
    participants_block = ", ".join(prepared_request.participants) if prepared_request.participants else "No participant list was provided."
    manual_notes = prepared_request.manual_notes[:manual_notes_max_chars]
    if len(prepared_request.manual_notes) > manual_notes_max_chars:
        manual_notes += "\n[Manual notes continue and will be supplied again to the final report.]"
    return (
        "Prepare a compact private brief for one chronological part of a meeting transcript.\n"
        "Keep decisions, concrete actions, owners, due dates, disagreements, names, numbers and open questions.\n"
        "Do not write the final meeting report yet. Do not invent. Return at most 350 words.\n\n"
        f"Meeting title: {prepared_request.title}\n"
        f"Chronological part: {chunk_index + 1}/{chunk_count}\n"
        f"Participants: {participants_block}\n\n"
        "Manual notes (priority source; possibly truncated here, complete notes are retained for the final pass):\n"
        f"{manual_notes or 'No manual notes were provided.'}\n\n"
        "Transcript (primary source for this chronological part):\n"
        f"{transcript_chunk}"
    )


def build_deep_think_sections(prepared_request: PreparedMeetingRequest, settings: Settings) -> list[DeepThinkSection]:
    manual_sections = extract_manual_note_sections(prepared_request.manual_notes)
    max_sections = max(1, settings.meeting_deep_think_max_sections)
    if should_use_core_deep_think_sections(manual_sections):
        manual_sections = build_core_deep_think_sections(prepared_request.manual_notes)
    elif not manual_sections:
        manual_sections = build_core_deep_think_sections(prepared_request.manual_notes)

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


def should_use_core_deep_think_sections(manual_sections: list[tuple[str, str]]) -> bool:
    if not manual_sections:
        return True
    normalized_titles = {normalize_section_title(title) for title, _content in manual_sections}
    return normalized_titles.issubset(SOURCE_NOTE_SKELETON_TITLES)


def normalize_section_title(title: str) -> str:
    title = re.sub(r"[*_`#]", "", title).strip().lower()
    title = title.replace("é", "e").replace("è", "e").replace("ê", "e").replace("à", "a")
    title = title.replace("ç", "c").replace("ù", "u")
    return re.sub(r"\s+", " ", title)


def extract_manual_note_sections(manual_notes: str) -> list[tuple[str, str]]:
    body = strip_frontmatter(manual_notes.strip())
    heading_matches = list(re.finditer(r"(?m)^(#{2,4})\s+(.+?)\s*$", body))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(heading_matches):
        title = re.sub(r"[*_`#]", "", match.group(2)).strip()
        start = match.end()
        end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(body)
        content = body[start:end].strip()
        content = remove_empty_meeting_note_placeholders(content)
        if title and content:
            sections.append((title, content))
    return sections


def build_core_deep_think_sections(manual_notes: str) -> list[tuple[str, str]]:
    cleaned = strip_frontmatter(manual_notes.strip())
    return [(title, cleaned) for title in DEEP_THINK_CORE_SECTIONS]


def remove_empty_meeting_note_placeholders(content: str) -> str:
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped in {"-", "- ", "- [ ]", "- [[ ]]", "- [[ ]]"}:
            continue
        if re.fullmatch(r"-\s*\[\[\s*\]\]", stripped):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


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
        "You write exactly one requested section of detailed, concrete Markdown meeting minutes.\n"
        "Manual notes are the priority source. Transcript excerpts can enrich them but must not replace them.\n"
        "Anonymous speaker labels can help follow the exchange, but do not convert them into real names unless explicitly supported.\n"
        "Preserve useful bullets, questions, answers, pillar names, decisions, actions and open points.\n"
        "Do not invent owners, dates, decisions or answers. Put uncertain items under uncertainties.\n"
        "Do not write a complete meeting report. Do not add unrelated sections such as summary, actions, participants, or transcript hints unless they are the requested section.\n"
        "Do not include source labels, prompt text, language hints, raw transcript labels, or triple backticks.\n"
        "Return only concise Markdown bullets or paragraphs for the requested section, without a top-level report title.\n\n"
        f"{language_instruction}"
    )


def build_deep_think_section_user_prompt(prepared_request: PreparedMeetingRequest, section: DeepThinkSection) -> str:
    participants_block = ", ".join(prepared_request.participants) if prepared_request.participants else "No participant list was provided."
    return (
        f"Meeting title: {prepared_request.title}\n"
        f"Section to write: {section.title}\n"
        f"Participants: {participants_block}\n\n"
        "Global template guidance. Use only as style guidance; do not reproduce the full template in this section:\n"
        f"{prepared_request.template}\n\n"
        "Manual notes for this section:\n"
        f"{section.manual_notes or 'No manual notes for this section.'}\n\n"
        "Relevant transcript excerpts:\n"
        f"{section.transcript_excerpt}\n\n"
        f"Write only the section named '{section.title}'. If there is no useful supported information for this exact section, return an empty string."
    )


def assemble_deep_think_report(
    prepared_request: PreparedMeetingRequest,
    rendered_sections: list[DeepThinkRenderedSection],
    *,
    final_cleanup: bool,
) -> str:
    parts = [f"# Compte rendu - {prepared_request.title}"]
    for section in rendered_sections:
        markdown = clean_deep_think_section_markdown(section.markdown, section.title)
        if not markdown:
            continue
        parts.append(f"## {section.title}\n\n{markdown}")
    result = "\n\n".join(parts).strip()
    return clean_deep_think_markdown(result) if final_cleanup else result


def clean_deep_think_section_markdown(markdown: str, section_title: str) -> str:
    cleaned = markdown.strip()
    fence_match = re.match(r"(?s)^```(?:markdown|md)?\s*(.*?)\s*```$", cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    cleaned = re.sub(r"(?im)^\s*(Generating section|Task instruction|Text to process|Manual notes|Transcript|Language instruction|Transcription language hint)\s*:.*\n?", "", cleaned)
    cleaned = re.sub(r"(?im)^#{1,2}\s*(compte rendu|meeting minutes|compte-rendu|note compagnon).*$\n?", "", cleaned)
    cleaned = re.sub(r"(?im)^#{1,3}\s*transcription language hint\s*$\n?(?:[\s\S]*?)(?=\n#{1,3}\s|\Z)", "", cleaned)
    cleaned = strip_redundant_outer_heading(cleaned, section_title)
    cleaned = re.sub(r"(?im)^#{1,3}\s*transcription language hint\s*$\n?(?:[\s\S]*?)(?=\n#{1,3}\s|\Z)", "", cleaned)
    cleaned = remove_empty_or_unsupported_lines(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_redundant_outer_heading(markdown: str, section_title: str) -> str:
    lines = markdown.splitlines()
    while lines and re.match(r"^\s*#{1,4}\s+", lines[0]):
        heading = re.sub(r"^\s*#{1,4}\s+", "", lines[0]).strip()
        if normalize_section_title(heading) == normalize_section_title(section_title) or len(lines) > 1:
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
            continue
        break
    return "\n".join(lines).strip()


def remove_empty_or_unsupported_lines(markdown: str) -> str:
    banned_phrases = [
        "aucune information disponible",
        "aucune decision formelle",
        "aucune décision formelle",
        "aucune liste de participants",
        "aucun point ouvert",
        "aucune incertitude",
        "no information available",
        "no formal decision",
        "no participants",
        "no open point",
    ]
    kept: list[str] = []
    for line in markdown.splitlines():
        lowered = line.strip().lower()
        if not lowered:
            kept.append(line)
            continue
        if any(phrase in lowered for phrase in banned_phrases):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def clean_deep_think_markdown(markdown: str) -> str:
    cleaned = markdown.strip()
    fence_match = re.match(r"(?s)^```(?:markdown|md)?\s*(.*?)\s*```$", cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    cleaned = re.sub(r"(?im)^\s*(Generating section|Task instruction|Text to process|Manual notes|Transcript|Language instruction|Transcription language hint)\s*:.*\n?", "", cleaned)
    cleaned = re.sub(r"(?im)^#{1,3}\s*transcription language hint\s*$\n?(?:[\s\S]*?)(?=\n#{1,3}\s|\Z)", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
