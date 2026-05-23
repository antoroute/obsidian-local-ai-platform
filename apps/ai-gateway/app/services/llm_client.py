from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.services.ollama_client import OllamaChatResult, OllamaClient


class LlmClient(Protocol):
    async def summarize_note(
        self,
        *,
        model: str,
        title: str,
        note_chars: int,
        template_chars: int,
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult: ...

    async def generate_meeting(
        self,
        *,
        model: str,
        title: str,
        transcript_chars: int,
        manual_notes_chars: int,
        template_chars: int,
        participants: list[str],
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult: ...


@dataclass(frozen=True)
class FakeLlmClient:
    async def summarize_note(
        self,
        *,
        model: str,
        title: str,
        note_chars: int,
        template_chars: int,
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult:
        del system_prompt, user_prompt
        content = (
            "# Resume fake\n"
            "Ceci est une reponse fake generee pour valider le workflow de developpement.\n\n"
            "## Informations recues\n"
            f"- Titre : {title or '(sans titre)'}\n"
            f"- Modele demande : {model}\n"
            f"- Longueur note : {note_chars} caracteres\n"
            f"- Longueur template : {template_chars} caracteres\n"
        )
        return OllamaChatResult(model=model, content=content)

    async def generate_meeting(
        self,
        *,
        model: str,
        title: str,
        transcript_chars: int,
        manual_notes_chars: int,
        template_chars: int,
        participants: list[str],
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult:
        del system_prompt, user_prompt
        participants_block = ", ".join(participants) if participants else "Aucun participant fourni."
        content = (
            "# Compte rendu fake\n"
            "Ceci est une reponse fake generee pour valider le workflow Obsidian.\n\n"
            "## Resume executif\n"
            "Le pipeline de generation fonctionne en mode fake.\n\n"
            "## Participants\n"
            f"{participants_block}\n\n"
            "## Decisions prises\n"
            "- Decision fake basee sur les notes manuelles.\n\n"
            "## Actions a suivre\n"
            "- Verifier que la note AI Summaries est bien creee.\n\n"
            "## Incertitudes\n"
            "- Ceci n'est pas une vraie generation LLM.\n\n"
            "## Informations recues\n"
            f"- Titre : {title}\n"
            f"- Modele demande : {model}\n"
            f"- Longueur transcript : {transcript_chars} caracteres\n"
            f"- Longueur notes manuelles : {manual_notes_chars} caracteres\n"
            f"- Longueur template : {template_chars} caracteres\n"
        )
        return OllamaChatResult(model=model, content=content)


@dataclass(frozen=True)
class OllamaLlmClient:
    ollama_client: OllamaClient

    async def summarize_note(
        self,
        *,
        model: str,
        title: str,
        note_chars: int,
        template_chars: int,
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult:
        del title, note_chars, template_chars
        return await self.ollama_client.summarize_markdown(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    async def generate_meeting(
        self,
        *,
        model: str,
        title: str,
        transcript_chars: int,
        manual_notes_chars: int,
        template_chars: int,
        participants: list[str],
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult:
        del title, transcript_chars, manual_notes_chars, template_chars, participants
        return await self.ollama_client.summarize_markdown(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
