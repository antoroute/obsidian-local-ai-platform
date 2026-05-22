from __future__ import annotations

from dataclasses import dataclass

import httpx


class OllamaServiceError(Exception):
    """Base exception for controlled Ollama failures."""


class OllamaUnavailableError(OllamaServiceError):
    """Raised when Ollama cannot be reached or times out."""


class OllamaResponseError(OllamaServiceError):
    """Raised when Ollama returns an invalid or unsuccessful response."""


@dataclass(frozen=True)
class OllamaChatResult:
    model: str
    content: str


class OllamaClient:
    def __init__(self, *, base_url: str, timeout_seconds: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def summarize_markdown(self, *, model: str, system_prompt: str, user_prompt: str) -> OllamaChatResult:
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise OllamaUnavailableError("Ollama request timed out.") from exc
        except httpx.ConnectError as exc:
            raise OllamaUnavailableError("Ollama is unavailable.") from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaResponseError("Ollama returned an unsuccessful response.") from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError("Failed to contact Ollama.") from exc

        try:
            response_payload = response.json()
            message = response_payload["message"]
            content = message["content"]
            response_model = str(response_payload.get("model", model))
        except (KeyError, TypeError, ValueError) as exc:
            raise OllamaResponseError("Ollama returned an invalid response payload.") from exc

        if not isinstance(content, str) or not content.strip():
            raise OllamaResponseError("Ollama returned an empty response.")

        return OllamaChatResult(model=response_model, content=content)
