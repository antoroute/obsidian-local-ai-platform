from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

ERROR_KIND_CONNECTION_REFUSED = "connection_refused"
ERROR_KIND_NETWORK_UNREACHABLE = "network_unreachable"
ERROR_KIND_DNS_ERROR = "dns_error"
ERROR_KIND_TIMEOUT = "timeout"
ERROR_KIND_HTTP_ERROR = "http_error"
ERROR_KIND_INVALID_JSON = "invalid_json"
ERROR_KIND_EMPTY_RESPONSE = "empty_response"
ERROR_KIND_MODEL_NOT_FOUND = "model_not_found"
ERROR_KIND_UNAVAILABLE = "unavailable"


class OllamaServiceError(Exception):
    """Base exception for controlled Ollama failures."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = ERROR_KIND_UNAVAILABLE,
        base_url: str = "",
        model: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.base_url = base_url
        self.model = model
        self.status_code = status_code


class OllamaUnavailableError(OllamaServiceError):
    """Raised when Ollama cannot be reached or times out."""


class OllamaResponseError(OllamaServiceError):
    """Raised when Ollama returns an invalid or unsuccessful response."""


@dataclass(frozen=True)
class OllamaChatResult:
    model: str
    content: str


@dataclass(frozen=True)
class OllamaCheckResult:
    base_url: str
    model: str
    available_models: list[str]
    chat_model: str
    chat_content: str


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: int,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._async_transport = async_transport

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
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._async_transport,
            ) as client:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise self._build_transport_error(exc, model=model) from exc
        except httpx.ConnectError as exc:
            raise self._build_transport_error(exc, model=model) from exc
        except httpx.HTTPStatusError as exc:
            raise self._build_http_error(exc, model=model) from exc
        except httpx.HTTPError as exc:
            raise self._build_transport_error(exc, model=model) from exc

        response_model, content = self._parse_chat_response(response, model=model)
        return OllamaChatResult(model=response_model, content=content)

    def check_connectivity(self, *, model: str) -> OllamaCheckResult:
        try:
            with httpx.Client(base_url=self._base_url, timeout=self._timeout, transport=self._transport) as client:
                tags_response = client.get("/api/tags")
                tags_response.raise_for_status()
                available_models = self._parse_tags_response(tags_response)

                if model not in available_models:
                    raise OllamaResponseError(
                        "Model not found in Ollama.",
                        kind=ERROR_KIND_MODEL_NOT_FOUND,
                        base_url=self._base_url,
                        model=model,
                    )

                chat_response = client.post(
                    "/api/chat",
                    json={
                        "model": model,
                        "stream": False,
                        "messages": [{"role": "user", "content": "Reponds seulement OK"}],
                    },
                )
                chat_response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise self._build_transport_error(exc, model=model) from exc
        except httpx.ConnectError as exc:
            raise self._build_transport_error(exc, model=model) from exc
        except httpx.HTTPStatusError as exc:
            raise self._build_http_error(exc, model=model) from exc
        except httpx.HTTPError as exc:
            raise self._build_transport_error(exc, model=model) from exc

        chat_model, chat_content = self._parse_chat_response(chat_response, model=model)
        return OllamaCheckResult(
            base_url=self._base_url,
            model=model,
            available_models=available_models,
            chat_model=chat_model,
            chat_content=chat_content,
        )

    def _parse_tags_response(self, response: httpx.Response) -> list[str]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise OllamaResponseError(
                "Ollama returned invalid JSON for /api/tags.",
                kind=ERROR_KIND_INVALID_JSON,
                base_url=self._base_url,
            ) from exc

        models = payload.get("models")
        if not isinstance(models, list):
            raise OllamaResponseError(
                "Ollama returned an invalid /api/tags payload.",
                kind=ERROR_KIND_INVALID_JSON,
                base_url=self._base_url,
            )

        available_models: list[str] = []
        for item in models:
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
                available_models.append(item["name"].strip())

        return available_models

    def _parse_chat_response(self, response: httpx.Response, *, model: str) -> tuple[str, str]:
        try:
            response_payload = response.json()
            message = response_payload["message"]
            content = message["content"]
            response_model = str(response_payload.get("model", model))
        except (KeyError, TypeError, ValueError) as exc:
            raise OllamaResponseError(
                "Ollama returned an invalid response payload.",
                kind=ERROR_KIND_INVALID_JSON,
                base_url=self._base_url,
                model=model,
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise OllamaResponseError(
                "Ollama returned an empty response.",
                kind=ERROR_KIND_EMPTY_RESPONSE,
                base_url=self._base_url,
                model=model,
            )

        return response_model, content

    def _build_transport_error(self, exc: httpx.HTTPError, *, model: str | None) -> OllamaUnavailableError:
        if isinstance(exc, httpx.TimeoutException):
            error = OllamaUnavailableError(
                "Ollama request timed out.",
                kind=ERROR_KIND_TIMEOUT,
                base_url=self._base_url,
                model=model,
            )
            self._log_unavailable_error(error)
            return error

        messages = collect_exception_messages(exc)
        if any("network is unreachable" in message or "no route to host" in message for message in messages):
            error = OllamaUnavailableError(
                "Network unreachable.",
                kind=ERROR_KIND_NETWORK_UNREACHABLE,
                base_url=self._base_url,
                model=model,
            )
            self._log_unavailable_error(error)
            return error

        if any("connection refused" in message or "actively refused" in message for message in messages):
            error = OllamaUnavailableError(
                "Connection refused.",
                kind=ERROR_KIND_CONNECTION_REFUSED,
                base_url=self._base_url,
                model=model,
            )
            self._log_unavailable_error(error)
            return error

        if any("name or service not known" in message or "getaddrinfo failed" in message or "nodename nor servname provided" in message for message in messages):
            error = OllamaUnavailableError(
                "Ollama host name could not be resolved.",
                kind=ERROR_KIND_DNS_ERROR,
                base_url=self._base_url,
                model=model,
            )
            self._log_unavailable_error(error)
            return error

        error = OllamaUnavailableError(
            "Failed to contact Ollama.",
            kind=ERROR_KIND_UNAVAILABLE,
            base_url=self._base_url,
            model=model,
        )
        self._log_unavailable_error(error)
        return error

    def _build_http_error(self, exc: httpx.HTTPStatusError, *, model: str | None) -> OllamaResponseError:
        error = OllamaResponseError(
            "Ollama returned an unsuccessful response.",
            kind=ERROR_KIND_HTTP_ERROR,
            base_url=self._base_url,
            model=model,
            status_code=exc.response.status_code,
        )
        logger.warning(
            "Ollama HTTP error kind=%s model=%s target=%s status=%s",
            error.kind,
            model or "-",
            self._target_host(),
            exc.response.status_code,
        )
        return error

    def _log_unavailable_error(self, error: OllamaUnavailableError) -> None:
        logger.warning(
            "Ollama connectivity error kind=%s model=%s target=%s",
            error.kind,
            error.model or "-",
            self._target_host(),
        )

    def _target_host(self) -> str:
        parsed = urlparse(self._base_url)
        return parsed.netloc or self._base_url


def collect_exception_messages(exc: BaseException) -> list[str]:
    messages: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip().lower()
        if message:
            messages.append(message)
        current = current.__cause__ or current.__context__
    return messages


def format_ollama_diagnostic_error(error: OllamaServiceError) -> str:
    prefix = f"Ollama check failed for {error.base_url}"
    if error.kind == ERROR_KIND_CONNECTION_REFUSED:
        return (
            f"{prefix}\n"
            "Connection refused. Ollama is reachable at network level but no service is accepting connections on this address/port. "
            "On Windows, stop Ollama and restart it with OLLAMA_HOST=0.0.0.0:11434 ollama serve, or use the containerized Ollama with a model installed."
        )
    if error.kind == ERROR_KIND_NETWORK_UNREACHABLE:
        return (
            f"{prefix}\n"
            "Network unreachable. The container cannot route to the configured Ollama host. Check Docker networks, host_access network, and host.docker.internal mapping."
        )
    if error.kind == ERROR_KIND_DNS_ERROR:
        return (
            f"{prefix}\n"
            "DNS resolution failed. The configured Ollama host name could not be resolved from the container. Check host.docker.internal mapping and extra_hosts."
        )
    if error.kind == ERROR_KIND_TIMEOUT:
        return (
            f"{prefix}\n"
            "Timeout. Ollama accepted the connection too slowly or did not answer in time. Check whether the service is running and whether the model is loading."
        )
    if error.kind == ERROR_KIND_MODEL_NOT_FOUND:
        return (
            f"{prefix}\n"
            "Model not found in Ollama. DEFAULT_MODEL/ALLOWED_MODELS do not install models. Install the model in the Ollama instance used by ai-gateway."
        )
    if error.kind == ERROR_KIND_HTTP_ERROR:
        return (
            f"{prefix}\n"
            f"Ollama returned HTTP {error.status_code}. The upstream service responded but did not accept the request. Check the Ollama logs and the requested model."
        )
    if error.kind == ERROR_KIND_INVALID_JSON:
        return (
            f"{prefix}\n"
            "Ollama returned invalid JSON. The service responded, but the payload was not in the expected format."
        )
    if error.kind == ERROR_KIND_EMPTY_RESPONSE:
        return (
            f"{prefix}\n"
            "Ollama returned an empty content response. The service answered, but no usable text was returned."
        )
    return f"{prefix}\nOllama is unavailable. Check the configured base URL and the upstream service."
