from __future__ import annotations

import httpx
import pytest

from app.services.ollama_client import (
    ERROR_KIND_CONNECTION_REFUSED,
    ERROR_KIND_MODEL_NOT_FOUND,
    OllamaClient,
    OllamaResponseError,
    OllamaUnavailableError,
    format_ollama_diagnostic_error,
)


def test_check_connectivity_succeeds_with_tags_and_chat() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "mistral:latest"}, {"name": "llama3:latest"}]})
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"model": "mistral:latest", "message": {"content": "OK"}})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    client = OllamaClient(
        base_url="http://host.docker.internal:11434",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )

    result = client.check_connectivity(model="mistral:latest")

    assert result.available_models == ["mistral:latest", "llama3:latest"]
    assert result.chat_model == "mistral:latest"
    assert result.chat_content == "OK"


def test_check_connectivity_reports_missing_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:14b"}]})

    client = OllamaClient(
        base_url="http://host.docker.internal:11434",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(OllamaResponseError) as exc_info:
        client.check_connectivity(model="mistral:latest")

    assert exc_info.value.kind == ERROR_KIND_MODEL_NOT_FOUND
    assert "Model not found in Ollama." in format_ollama_diagnostic_error(exc_info.value)


def test_check_connectivity_reports_connection_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[WinError 10061] No connection could be made because the target machine actively refused it", request=request)

    client = OllamaClient(
        base_url="http://host.docker.internal:11434",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(OllamaUnavailableError) as exc_info:
        client.check_connectivity(model="mistral:latest")

    assert exc_info.value.kind == ERROR_KIND_CONNECTION_REFUSED
    assert "Connection refused." in format_ollama_diagnostic_error(exc_info.value)
