"""Tests for ``cli.lib.initial_workspace.write_agnes_env``.

Covers:
  * Atomic write (temp file + os.replace; no partial state visible)
  * chmod 600 on the resulting file
  * schema_version + content_sha256 header present
  * Globals override per-connector keys on collision
  * Dotenv quoting for values with shell metacharacters / whitespace
  * Empty payload → None (no file written)
  * Server 404 → None (older server, silent skip)
  * Idempotent: re-run with same params produces identical content_sha256
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "ws"


def _patch_api_get(payload: dict | None, status: int = 200):
    """Patch the api_get call inside write_agnes_env to return a fake
    response without hitting the network.
    """
    def _fake_get(path: str, *args, **kwargs):
        return _FakeResponse(status, payload)
    # Patch _override_server_env to be a no-op context manager so the
    # api_get call goes straight to our patched stub.
    from contextlib import contextmanager

    @contextmanager
    def _noop_env(*args, **kwargs):
        yield

    return [
        patch("cli.lib.pull._override_server_env", _noop_env),
        patch("cli.lib.initial_workspace.api_get", _fake_get),
    ]


def test_write_agnes_env_writes_file_with_chmod_600(workspace: Path):
    payload = {
        "schema_version": 1,
        "params": {"connector-atlassian": {"ATLASSIAN_BASE_URL": "https://acme.atlassian.net"}},
        "globals": {"AGNES_INSTANCE_BRAND": "Acme"},
    }
    patchers = _patch_api_get(payload)
    for p in patchers:
        p.start()
    try:
        from cli.lib.initial_workspace import write_agnes_env

        env_path = write_agnes_env(workspace, "https://srv", "tok")
    finally:
        for p in patchers:
            p.stop()

    assert env_path is not None
    assert env_path == workspace / ".claude" / "agnes" / ".env"
    assert env_path.is_file()
    # POSIX-only: chmod 600
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    body = env_path.read_text(encoding="utf-8")
    assert "schema_version=1" in body
    assert "AGNES_INSTANCE_BRAND=Acme" in body
    assert "ATLASSIAN_BASE_URL=https://acme.atlassian.net" in body
    assert "DO NOT EDIT" in body
    assert "content_sha256=" in body


def test_write_agnes_env_quotes_values_with_whitespace(workspace: Path):
    payload = {
        "params": {},
        "globals": {"AGNES_INSTANCE_BRAND": "Acme Analytics"},
    }
    patchers = _patch_api_get(payload)
    for p in patchers:
        p.start()
    try:
        from cli.lib.initial_workspace import write_agnes_env

        write_agnes_env(workspace, "https://srv", "tok")
    finally:
        for p in patchers:
            p.stop()

    body = (workspace / ".claude" / "agnes" / ".env").read_text(encoding="utf-8")
    # Whitespace forces quoting.
    assert 'AGNES_INSTANCE_BRAND="Acme Analytics"' in body


def test_write_agnes_env_escapes_embedded_quotes(workspace: Path):
    payload = {
        "params": {"connector-foo": {"WEIRD_VAL": 'a"b\\c'}},
        "globals": {},
    }
    patchers = _patch_api_get(payload)
    for p in patchers:
        p.start()
    try:
        from cli.lib.initial_workspace import write_agnes_env

        write_agnes_env(workspace, "https://srv", "tok")
    finally:
        for p in patchers:
            p.stop()

    body = (workspace / ".claude" / "agnes" / ".env").read_text(encoding="utf-8")
    # Backslash + quote both escape.
    assert 'WEIRD_VAL="a\\"b\\\\c"' in body


def test_write_agnes_env_globals_win_over_per_connector(workspace: Path):
    """When the same key appears in both `globals` and a per-connector
    block, the global value wins. Operators using `connectors:globals` for
    instance-wide overrides shouldn't be silently shadowed by a stray
    per-connector key with the same name.
    """
    payload = {
        "params": {"connector-foo": {"AGNES_INSTANCE_BRAND": "FromConnector"}},
        "globals": {"AGNES_INSTANCE_BRAND": "FromGlobal"},
    }
    patchers = _patch_api_get(payload)
    for p in patchers:
        p.start()
    try:
        from cli.lib.initial_workspace import write_agnes_env

        write_agnes_env(workspace, "https://srv", "tok")
    finally:
        for p in patchers:
            p.stop()

    body = (workspace / ".claude" / "agnes" / ".env").read_text(encoding="utf-8")
    assert "AGNES_INSTANCE_BRAND=FromGlobal" in body
    assert "AGNES_INSTANCE_BRAND=FromConnector" not in body


def test_write_agnes_env_empty_payload_returns_none(workspace: Path):
    """No params + no globals → don't write a file. The dir might still
    be created (mkdir is idempotent) but the .env file itself stays
    absent so seed skills know to fall back to prompts.
    """
    payload = {"params": {}, "globals": {}}
    patchers = _patch_api_get(payload)
    for p in patchers:
        p.start()
    try:
        from cli.lib.initial_workspace import write_agnes_env

        result = write_agnes_env(workspace, "https://srv", "tok")
    finally:
        for p in patchers:
            p.stop()

    assert result is None
    env_path = workspace / ".claude" / "agnes" / ".env"
    assert not env_path.exists()


def test_write_agnes_env_returns_none_on_404(workspace: Path):
    """Older server without /api/connectors/params → 404 → silent skip."""
    patchers = _patch_api_get(payload=None, status=404)
    for p in patchers:
        p.start()
    try:
        from cli.lib.initial_workspace import write_agnes_env

        result = write_agnes_env(workspace, "https://srv", "tok")
    finally:
        for p in patchers:
            p.stop()

    assert result is None
    assert not (workspace / ".claude" / "agnes" / ".env").exists()


def test_write_agnes_env_idempotent(workspace: Path):
    """Same payload twice → same content_sha256 (no spurious diff)."""
    payload = {
        "params": {"connector-atlassian": {"ATLASSIAN_BASE_URL": "https://x"}},
        "globals": {"AGNES_INSTANCE_BRAND": "Brand"},
    }
    patchers = _patch_api_get(payload)
    for p in patchers:
        p.start()
    try:
        from cli.lib.initial_workspace import write_agnes_env

        write_agnes_env(workspace, "https://srv", "tok")
        first = (workspace / ".claude" / "agnes" / ".env").read_text(encoding="utf-8")
        write_agnes_env(workspace, "https://srv", "tok")
        second = (workspace / ".claude" / "agnes" / ".env").read_text(encoding="utf-8")
    finally:
        for p in patchers:
            p.stop()

    assert first == second


def test_write_agnes_env_omits_none_values(workspace: Path):
    """Per-connector blocks with a None value (operator left field blank
    in instance.yaml) skip the key rather than writing literal `None`.
    """
    payload = {
        "params": {"connector-gws": {"AGNES_GWS_CLIENT_ID": "abc", "AGNES_GWS_PROJECT_ID": None}},
        "globals": {},
    }
    patchers = _patch_api_get(payload)
    for p in patchers:
        p.start()
    try:
        from cli.lib.initial_workspace import write_agnes_env

        write_agnes_env(workspace, "https://srv", "tok")
    finally:
        for p in patchers:
            p.stop()

    body = (workspace / ".claude" / "agnes" / ".env").read_text(encoding="utf-8")
    assert "AGNES_GWS_CLIENT_ID=abc" in body
    assert "AGNES_GWS_PROJECT_ID" not in body
