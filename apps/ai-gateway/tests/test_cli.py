from __future__ import annotations

import argparse

import pytest
from sqlalchemy.exc import OperationalError

from app import cli


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
