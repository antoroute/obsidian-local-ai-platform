from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from app.services.ollama_client import (
    ERROR_KIND_EMPTY_RESPONSE,
    ERROR_KIND_INVALID_JSON,
    OllamaResponseError,
    OllamaRequestLimiter,
    OllamaUnavailableError,
)


@dataclass(frozen=True)
class EmbeddingResult:
    model: str
    embedding: list[float]


class OllamaEmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: int,
        model: str,
        keep_alive: str | None = None,
        request_limiter: OllamaRequestLimiter | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._model = model
        self._keep_alive = keep_alive
        self._request_limiter = request_limiter
        self._async_transport = async_transport

    async def embed_text(self, text: str) -> EmbeddingResult:
        payload = {"model": self._model, "input": text}
        if self._keep_alive:
            payload["keep_alive"] = self._keep_alive
        try:
            async with self._request_slot():
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout,
                    transport=self._async_transport,
                ) as client:
                    response = await client.post("/api/embed", json=payload)
                    response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise OllamaUnavailableError("Ollama embedding request timed out.", base_url=self._base_url, model=self._model) from exc
        except httpx.ConnectError as exc:
            raise OllamaUnavailableError("Ollama embeddings are unavailable.", base_url=self._base_url, model=self._model) from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaResponseError(
                "Ollama embedding model returned an error.",
                base_url=self._base_url,
                model=self._model,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError("Failed to contact Ollama embeddings.", base_url=self._base_url, model=self._model) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise OllamaResponseError(
                "Ollama returned invalid JSON for embeddings.",
                kind=ERROR_KIND_INVALID_JSON,
                base_url=self._base_url,
                model=self._model,
            ) from exc

        embedding = parse_embedding_payload(data)
        if not embedding:
            raise OllamaResponseError(
                "Ollama returned an empty embedding.",
                kind=ERROR_KIND_EMPTY_RESPONSE,
                base_url=self._base_url,
                model=self._model,
            )
        return EmbeddingResult(model=self._model, embedding=embedding)

    @asynccontextmanager
    async def _request_slot(self) -> AsyncIterator[None]:
        if self._request_limiter is None:
            yield
            return
        async with self._request_limiter.slot():
            yield


def parse_embedding_payload(data: object) -> list[float]:
    if not isinstance(data, dict):
        return []
    candidate = data.get("embedding")
    if candidate is None:
        embeddings = data.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            candidate = embeddings[0]
    if not isinstance(candidate, list):
        return []
    values: list[float] = []
    for item in candidate:
        if not isinstance(item, int | float):
            return []
        values.append(float(item))
    return values
