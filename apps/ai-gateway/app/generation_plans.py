"""Snapshot business prompts before a generation is accepted.

Only local embeddings perform network I/O here. Once accepted, every prompt,
source citation and intermediate output needed for recovery is stored in SQL.
"""
from dataclasses import asdict
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.assistant import prepare_assistant_request
from app.config import Settings
from app.jobs import read_transcript_result, require_job_for_user
from app.meetings import (
    DeepThinkRenderedSection, PreparedMeetingRequest, assemble_deep_think_report,
    build_deep_think_section_system_prompt, build_deep_think_section_user_prompt,
    build_deep_think_sections, build_meeting_predigest_chunk_user_prompt,
    build_meeting_user_prompt_from_brief, extract_transcript_text_from_result,
    prepare_meeting_from_job_request, prepare_meeting_request,
    split_transcript_for_predigest, validate_audio_job_for_meeting,
)
from app.notes import prepare_summary_request
from app.schemas import (
    AssistantChatRequest, MeetingGenerateFromJobRequest, MeetingGenerateRequest,
    NoteSummarizeRequest, VaultAskRequest, VaultSearchRequest, VaultSourceResponse,
)
from app.vault_rag import build_insufficient_sources_answer, build_vault_answer_prompt, ensure_rag_enabled, search_vault

SCOPES = {
    "assistant": "assistant:chat", "note_summary": "notes:summarize",
    "meeting": "meetings:generate", "meeting_from_job": "meetings:generate",
    "vault_answer": "vault:ask",
}
INPUT_TYPES = {
    "assistant": AssistantChatRequest, "note_summary": NoteSummarizeRequest,
    "meeting": MeetingGenerateRequest, "meeting_from_job": MeetingGenerateFromJobRequest,
    "vault_answer": VaultAskRequest,
}


def parse_input(kind: str, raw: dict[str, object], model_id: str) -> BaseModel:
    schema = INPUT_TYPES[kind]
    if set(raw) - set(schema.model_fields):
        raise HTTPException(422, "Unknown generation input fields.")
    if raw.get("model") not in (None, "", model_id):
        raise HTTPException(422, "Input model and explicit target must agree.")
    try:
        return schema.model_validate({**raw, "model": model_id})
    except ValidationError as exc:
        # Never echo note contents or credentials in validation errors.
        raise HTTPException(422, "Invalid generation input.") from exc


def messages(system: str, user: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def check_messages(value: list[dict[str, str]], settings: Settings) -> None:
    if sum(len(message["content"]) for message in value) > settings.enterprise_max_prompt_chars:
        raise HTTPException(413, "Prepared prompt exceeds the enterprise input limit.")


async def prepare_plan(
    kind: str, raw: dict[str, object], model_id: str, user_id: str,
    session: Session, settings: Settings, embedding_client: Any,
) -> dict[str, Any]:
    payload = parse_input(kind, raw, model_id)
    # Reuse existing business validators with the independently validated
    # enterprise allowlist. No change to the personal model configuration.
    business_settings = settings.model_copy(update={"allowed_models_raw": model_id, "default_model": model_id})
    plan: dict[str, Any] = {"version": 1, "mode": "single", "steps": [], "result": {"model": model_id}}
    if kind == "assistant":
        prepared = prepare_assistant_request(payload, business_settings)
        plan.update(output_key="answer_markdown", steps=[messages(prepared.system_prompt, prepared.user_prompt)])
        plan["result"].update(mode=prepared.mode, usage={"message_chars": prepared.message_chars, "context_chars": prepared.context_chars})
    elif kind == "note_summary":
        prepared = prepare_summary_request(payload, business_settings)
        plan.update(output_key="summary_markdown", steps=[messages(prepared.system_prompt, prepared.user_prompt)])
        plan["result"].update(title=payload.title, usage={"prompt_chars": prepared.prompt_chars, "template_chars": prepared.template_chars})
    elif kind in ("meeting", "meeting_from_job"):
        diarization_status = None
        if kind == "meeting_from_job":
            job = require_job_for_user(session, job_id=payload.job_id, user_id=user_id)
            validate_audio_job_for_meeting(job)
            transcript_payload = read_transcript_result(job)
            transcript = extract_transcript_text_from_result(transcript_payload)
            diarization_status = transcript_payload.get("diarization_status")
            prepared = prepare_meeting_from_job_request(payload, transcript=transcript, settings=business_settings)
            plan["result"]["job_id"] = job.id
        else:
            prepared = prepare_meeting_request(payload, business_settings)
        plan.update(output_key="meeting_markdown", prepared=asdict(prepared), final_cleanup=settings.meeting_deep_think_final_cleanup)
        plan["result"].update(
            title=prepared.title, generation_mode=prepared.generation_mode,
            generation_stages=None, generation_analysis=None,
            usage={"transcript_chars": prepared.transcript_chars, "manual_notes_chars": prepared.manual_notes_chars,
                   "template_chars": prepared.template_chars, "participants_count": prepared.participants_count},
        )
        section_titles: list[str] = []
        if prepared.generation_mode == "deep_think":
            sections = build_deep_think_sections(prepared, settings)
            section_titles = [section.title for section in sections]
            plan["mode"] = "deep_think"
            plan["steps"] = [messages(build_deep_think_section_system_prompt(payload.output_language), build_deep_think_section_user_prompt(prepared, section)) for section in sections]
            plan["section_titles"] = section_titles
        elif prepared.should_predigest:
            chunks = split_transcript_for_predigest(prepared.cleaned_transcript, settings)
            section_titles = [f"Chronological brief {index + 1}" for index in range(len(chunks))]
            plan["mode"] = "predigest"
            plan["steps"] = [messages(prepared.predigest_system_prompt, build_meeting_predigest_chunk_user_prompt(
                prepared, chunk, chunk_index=index, chunk_count=len(chunks),
                manual_notes_max_chars=settings.meeting_predigest_manual_notes_max_chars,
            )) for index, chunk in enumerate(chunks)] + [None]
        else:
            plan["steps"] = [messages(prepared.system_prompt, prepared.user_prompt)]
        if section_titles:
            plan["result"]["generation_stages"] = len(plan["steps"])
            plan["result"]["generation_analysis"] = {
                "mode": prepared.generation_mode, "sections_count": len(section_titles), "section_titles": section_titles,
                "transcript_chars": prepared.transcript_chars, "manual_notes_chars": prepared.manual_notes_chars,
                "template_chars": prepared.template_chars, "output_language": payload.output_language,
                "diarization_status": diarization_status if diarization_status in {"disabled", "completed", "failed"} else None,
            }
    elif kind == "vault_answer":
        ensure_rag_enabled(settings)
        if len(payload.question) > settings.max_assistant_message_chars:
            raise HTTPException(413, "Question exceeds the input limit.")
        # Existing installations may explicitly configure one shared workspace.
        # A caller cannot select another user's arbitrary workspace via this API.
        workspace_id = settings.rag_workspace_id.strip() or user_id
        if payload.workspace_id and payload.workspace_id != workspace_id:
            raise HTTPException(403, "Requested workspace is not authorized.")
        async def embed(text: str) -> list[float]:
            return (await embedding_client.embed_text(text)).embedding
        session.commit()  # release authentication reads before local embedding I/O
        hits = await search_vault(session, user_id=user_id, workspace_id=workspace_id,
            payload=VaultSearchRequest(vault_id=payload.vault_id, workspace_id=workspace_id,
                query=payload.question, top_k=payload.top_k, path_prefix=payload.path_prefix, tags=payload.tags),
            settings=settings, embed_text=embed)
        sources = [VaultSourceResponse(path=hit.document.path, title=hit.document.title,
            heading_path=hit.chunk.heading_path, chunk_index=hit.chunk.chunk_index,
            score=round(hit.score, 6), vector_score=round(hit.vector_score, 6),
            keyword_bonus=round(hit.keyword_bonus, 6), matched_terms=hit.matched_terms).model_dump() for hit in hits]
        plan.update(output_key="answer_markdown")
        plan["result"].update(sources=sources, debug_info=None)
        if payload.debug:
            plan["result"]["debug_info"] = {"selected_sources_count": len(hits), "selected_paths": [source["path"] for source in sources]}
        if not hits or all(hit.score < settings.rag_min_score for hit in hits):
            plan["result"]["answer_markdown"] = build_insufficient_sources_answer(hits)
        else:
            system, user = build_vault_answer_prompt(question=payload.question, hits=hits, settings=settings, answer_language=payload.answer_language)
            plan["steps"] = [messages(system, user)]
    for step in plan["steps"]:
        if step is not None:
            check_messages(step, settings)
    return plan


def next_messages(plan: dict[str, Any], completed: list[str], settings: Settings) -> list[dict[str, str]]:
    prepared = PreparedMeetingRequest(**plan["prepared"])
    briefs = "\n\n".join(f"### Chronological brief {index + 1}/{len(completed)}\n\n{content.strip()}" for index, content in enumerate(completed))
    value = messages(prepared.system_prompt, build_meeting_user_prompt_from_brief(prepared, briefs))
    check_messages(value, settings)
    return value


def finish_plan(plan: dict[str, Any], contents: list[str], models: list[str]) -> dict[str, Any]:
    result = dict(plan["result"])
    if not contents:
        return result
    result["model"] = models[-1]
    if plan["mode"] == "deep_think":
        prepared = PreparedMeetingRequest(**plan["prepared"])
        rendered = [DeepThinkRenderedSection(title=title, markdown=content) for title, content in zip(plan["section_titles"], contents, strict=True)]
        result[plan["output_key"]] = assemble_deep_think_report(prepared, rendered, final_cleanup=plan["final_cleanup"])
    else:
        result[plan["output_key"]] = contents[-1]
    return result
