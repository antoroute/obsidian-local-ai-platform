from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status

from app.config import Settings
from app.schemas import AssistantChatRequest

ASSISTANT_SYSTEM_PROMPT = """You are Note Compagnon, a private Obsidian assistant.
Answer in Markdown.
Do not invent facts.
If information is missing, say so briefly.
For correction and rewriting, preserve meaning and never add unsupported details.
For chat, answer the user question directly.
Do not fill or imitate an Obsidian template, person note, organization note, topic note, role note, or meeting note unless the user explicitly asks for that format.
If no context is provided, answer generally from the user message only.
If context is provided, use it only as reference material."""

MODE_INSTRUCTIONS = {
    "chat": (
        "Answer the user's question directly, clearly, and practically. Prefer a short direct answer by default. "
        "If no context is provided, do not invent context and answer generally from the user message only. "
        "If context is provided, use it only as reference material to answer the specific question. "
        "Do not automatically summarize the context. Do not transform the context into meeting minutes, a report, or an Obsidian note. "
        "Do not use, fill, or imitate a note template unless explicitly requested."
    ),
    "correct": (
        "Correct spelling, grammar, typography, and punctuation. Preserve the meaning. "
        "Keep the same language as the text to process when Language is same_as_input. Do not translate unless explicitly forced. "
        "Do not strongly rephrase. Do not add explanations. Do not write 'Here is the correction'. "
        "Return only the corrected text."
    ),
    "rewrite": (
        "Improve clarity, flow, and style. Preserve the meaning. Do not make the text unnecessarily longer. "
        "Keep the same language as the text to process when Language is same_as_input. Do not translate unless explicitly forced. "
        "If the user message specifies a tone or style such as professional, apply it while preserving the original intent. "
        "Do not add explanations. Do not write 'Here is a rewritten version'. "
        "Return only the rewritten text. For short texts, stay short."
    ),
    "summarize": (
        "Summarize in concise Markdown. Do not add an introduction. Do not write 'Here is the summary'. "
        "Use the main language of the text to process when Language is same_as_input. Do not translate unless explicitly forced. "
        "Return only the summary."
    ),
}

ACTION_PRESET_INSTRUCTIONS = {
    "professional": (
        "Professional rewrite preset: improve tone, clarity, and wording for a business context. "
        "Keep the same language as the text to process. Preserve the meaning. Do not make the text unnecessarily longer. "
        "Do not turn the text into a full email unless it already looks like an email. "
        "Return only the final rewritten text."
    ),
}

LANGUAGE_INSTRUCTIONS = {
    "fr": "Write the answer in French.",
    "en": "Write the answer in English.",
    "same_as_input": (
        "same_as_input: detect the main language of the text that must be processed or answered. "
        "For correction, rewrite, and summarize tasks, use the language of the TEXT block, not the language of the instruction. "
        "French text must receive French output. English text must receive English output."
    ),
}


@dataclass(frozen=True)
class PreparedAssistantRequest:
    selected_model: str
    mode: str
    system_prompt: str
    user_prompt: str
    message_chars: int
    context_chars: int


def prepare_assistant_request(payload: AssistantChatRequest, settings: Settings) -> PreparedAssistantRequest:
    message = payload.message.strip()
    context = (payload.context or "").strip()
    mode = payload.mode

    if not message and not context:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="message or context must be provided.",
        )
    if mode == "chat" and not message:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="message is required for chat mode.",
        )
    if mode in {"correct", "rewrite", "summarize"} and not context:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="context is required for this assistant mode.",
        )
    if payload.action_preset and not (mode == "rewrite" and payload.action_preset == "professional"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="action_preset=professional is only supported for rewrite mode.",
        )
    if len(message) > settings.max_assistant_message_chars:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"message exceeds the maximum of {settings.max_assistant_message_chars} characters.",
        )
    if len(context) > settings.max_assistant_context_chars:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"context exceeds the maximum of {settings.max_assistant_context_chars} characters.",
        )

    selected_model = payload.model or settings.default_model
    if selected_model not in settings.allowed_models:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requested model is not allowed.")
    response_style = payload.response_style or ("detailed" if mode == "chat" else "direct")

    return PreparedAssistantRequest(
        selected_model=selected_model,
        mode=mode,
        system_prompt=build_assistant_system_prompt(mode, payload.output_language, response_style, payload.action_preset),
        user_prompt=build_assistant_user_prompt(mode=mode, message=message, context=context, action_preset=payload.action_preset),
        message_chars=len(message),
        context_chars=len(context),
    )


def build_assistant_system_prompt(mode: str, output_language: str, response_style: str, action_preset: str | None = None) -> str:
    mode_instruction = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["chat"])
    preset_instruction = f"\nPreset: {ACTION_PRESET_INSTRUCTIONS[action_preset]}" if action_preset else ""
    language_instruction = LANGUAGE_INSTRUCTIONS.get(output_language, LANGUAGE_INSTRUCTIONS["same_as_input"])
    style_instruction = (
        "Response style: direct. Return only the final usable result with no preamble or explanation."
        if response_style == "direct"
        else "Response style: detailed. You may explain briefly when useful."
    )
    return f"{ASSISTANT_SYSTEM_PROMPT}\n\nTask: {mode_instruction}{preset_instruction}\nLanguage: {language_instruction}\n{style_instruction}"


def build_assistant_user_prompt(*, mode: str, message: str, context: str, action_preset: str | None = None) -> str:
    if mode in {"correct", "rewrite", "summarize"}:
        task_instruction = {
            "correct": "Correct only the text inside the TEXT block. Do not rewrite this instruction.",
            "rewrite": "Rewrite only the text inside the TEXT block. Do not rewrite this instruction.",
            "summarize": "Summarize only the text inside the TEXT block. Do not summarize this instruction.",
        }[mode]
        if action_preset == "professional":
            task_instruction += " Apply the professional rewrite preset."
        return (
            "Task instruction:\n"
            f"{task_instruction}\n"
            "Language rule: detect the main language of the TEXT block below and answer in that same language. "
            "Do not use the instruction language to choose the output language. Do not translate unless explicitly forced.\n"
            "Return only the final result. Do not include labels such as Original, Final, Task instruction, or Text to process.\n\n"
            "Text to process:\n"
            "<<<TEXT\n"
            f"{context}\n"
            "TEXT\n"
        )

    context_block = context or "No additional context was provided."
    message_block = message or "No explicit message was provided; use the context for the requested task."
    return (
        "User message:\n"
        f"{message_block}\n\n"
        "Context:\n"
        f"{context_block}\n"
    )
