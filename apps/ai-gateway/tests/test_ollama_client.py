from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services.ollama_client import (
    ERROR_KIND_CONNECTION_REFUSED,
    ERROR_KIND_MODEL_NOT_FOUND,
    OllamaClient,
    OllamaRequestLimiter,
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


def test_summarize_markdown_bounds_context_and_model_lifetime() -> None:
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(200, json={"model": "qwen3:8b", "message": {"content": "Resume"}})

    client = OllamaClient(
        base_url="http://ollama:11434",
        timeout_seconds=10,
        num_ctx=8192,
        keep_alive="5m",
        async_transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.summarize_markdown(
            model="qwen3:8b",
            system_prompt="Systeme",
            user_prompt="Contenu",
        )
    )

    assert result.content == "Resume"
    assert captured_payload["options"] == {"num_ctx": 8192}
    assert captured_payload["keep_alive"] == "5m"


def test_request_limiter_serializes_ollama_work() -> None:
    async def scenario() -> None:
        limiter = OllamaRequestLimiter(1)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first_request() -> None:
            async with limiter.slot():
                first_entered.set()
                await release_first.wait()

        async def second_request() -> None:
            await first_entered.wait()
            async with limiter.slot():
                second_entered.set()

        first_task = asyncio.create_task(first_request())
        second_task = asyncio.create_task(second_request())
        await first_entered.wait()
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert second_entered.is_set()

    asyncio.run(scenario())


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
