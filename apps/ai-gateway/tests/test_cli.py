from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app import cli
from app.services.ollama_client import OllamaCheckResult, OllamaUnavailableError


def test_create_token_command_handles_outdated_schema(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(name="dev-token", scopes="models:list", expires_at=None)

    monkeypatch.setattr(cli, "init_db", lambda: None)

    class FakeSession:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeSessionFactory:
        def __call__(self):
            return FakeSession()

    monkeypatch.setattr(cli, "get_session_factory", lambda: FakeSessionFactory())

    def raise_outdated_schema(*args, **kwargs):
        raise OperationalError("INSERT INTO api_tokens ...", {}, Exception("table api_tokens has no column named user_id"))

    monkeypatch.setattr(cli, "create_api_token", raise_outdated_schema)

    exit_code = cli.create_token_command(args)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Local database schema appears outdated." in captured.err


def test_create_token_command_does_not_mask_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    args = argparse.Namespace(name="dev-token", scopes="models:list", expires_at=None)

    monkeypatch.setattr(cli, "init_db", lambda: None)

    class FakeSession:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeSessionFactory:
        def __call__(self):
            return FakeSession()

    monkeypatch.setattr(cli, "get_session_factory", lambda: FakeSessionFactory())

    def raise_unexpected(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "create_api_token", raise_unexpected)

    with pytest.raises(RuntimeError, match="boom"):
        cli.create_token_command(args)


def test_check_ollama_command_reports_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(model=None, base_url=None)
    settings = SimpleNamespace(ollama_base_url="http://host.docker.internal:11434", default_model="mistral:latest", ollama_timeout_seconds=10)

    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    class FakeOllamaClient:
        def __init__(self, *, base_url: str, timeout_seconds: int) -> None:
            assert base_url == settings.ollama_base_url
            assert timeout_seconds == settings.ollama_timeout_seconds

        def check_connectivity(self, *, model: str) -> OllamaCheckResult:
            assert model == settings.default_model
            return OllamaCheckResult(
                base_url=settings.ollama_base_url,
                model=model,
                available_models=["mistral:latest"],
                chat_model="mistral:latest",
                chat_content="OK",
            )

    monkeypatch.setattr(cli, "OllamaClient", FakeOllamaClient)

    exit_code = cli.check_ollama_command(args)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "OLLAMA_BASE_URL: http://host.docker.internal:11434" in captured.out
    assert "Ollama connectivity OK" in captured.out


def test_check_ollama_command_reports_connection_refused(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(model="mistral:latest", base_url="http://host.docker.internal:11434")
    settings = SimpleNamespace(ollama_base_url="http://unused", default_model="unused", ollama_timeout_seconds=10)

    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    class FakeOllamaClient:
        def __init__(self, *, base_url: str, timeout_seconds: int) -> None:
            del timeout_seconds
            self.base_url = base_url

        def check_connectivity(self, *, model: str) -> OllamaCheckResult:
            raise OllamaUnavailableError(
                "Connection refused.",
                kind="connection_refused",
                base_url=self.base_url,
                model=model,
            )

    monkeypatch.setattr(cli, "OllamaClient", FakeOllamaClient)

    exit_code = cli.check_ollama_command(args)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Connection refused." in captured.err
    assert "OLLAMA_HOST=0.0.0.0:11434 ollama serve" in captured.err
