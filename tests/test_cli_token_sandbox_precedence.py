"""Token resolution precedence for the chat-sandbox context.

``get_token()`` historically preferred ``~/.config/agnes/token.json`` over
the ``AGNES_TOKEN`` env var — correct on an analyst laptop, where the file
is the canonical credential written by ``agnes init``. Inside a chat
sandbox it is inverted: ChatManager._spawn_runner mints a FRESH short-lived
session JWT into ``AGNES_TOKEN`` on every spawn, so any token file found
there (e.g. left by an in-session ``agnes init``, or replayed workspace
state) is by definition stale and must not shadow the fresh credential.
``AGNES_SESSION_ID`` — set only by the runner env — is the discriminator.
"""
from __future__ import annotations

import json


def _write_token_file(tmp_path, token: str):
    (tmp_path / "token.json").write_text(json.dumps({"access_token": token, "email": "u@x"}))


def test_env_wins_inside_sandbox(tmp_path, monkeypatch):
    """AGNES_SESSION_ID set (chat sandbox) → fresh AGNES_TOKEN env beats a
    stale persisted token file."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    _write_token_file(tmp_path, "stale-file-token")
    monkeypatch.setenv("AGNES_TOKEN", "fresh-env-token")
    monkeypatch.setenv("AGNES_SESSION_ID", "chat_abc123")
    from cli.config import get_token

    assert get_token() == "fresh-env-token"


def test_file_still_wins_on_analyst_laptop(tmp_path, monkeypatch):
    """No AGNES_SESSION_ID (normal CLI use) → unchanged behavior: the
    token.json written by `agnes init` wins over a lingering env var."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    _write_token_file(tmp_path, "file-token")
    monkeypatch.setenv("AGNES_TOKEN", "env-token")
    monkeypatch.delenv("AGNES_SESSION_ID", raising=False)
    from cli.config import get_token

    assert get_token() == "file-token"


def test_sandbox_falls_back_to_file_when_env_empty(tmp_path, monkeypatch):
    """AGNES_SESSION_ID set but AGNES_TOKEN empty/unset (e.g. the
    JWT_SEED fallback minted '') → fall back to the file rather than
    returning an empty credential."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    _write_token_file(tmp_path, "file-token")
    monkeypatch.setenv("AGNES_SESSION_ID", "chat_abc123")
    monkeypatch.setenv("AGNES_TOKEN", "")
    from cli.config import get_token

    assert get_token() == "file-token"


def test_override_still_beats_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    _write_token_file(tmp_path, "file-token")
    monkeypatch.setenv("AGNES_TOKEN", "env-token")
    monkeypatch.setenv("AGNES_SESSION_ID", "chat_abc123")
    from cli.config import _with_token_override, get_token

    with _with_token_override("override-token"):
        assert get_token() == "override-token"
