"""``_probe_caller_user_id``'s oauth-aware extension (2026-07-30 outbound
MCP OAuth sources spec §5).

Admin connect probes (introspect/classify/test) run under the calling
admin's own credential when they have one. For a secret-backed per_user
source that's ``mcp_user_secrets``; for an oauth source there is no such
row at all — the probe must instead consult ``mcp_user_oauth_tokens``.
Direct unit tests against the pure helper (no HTTP, no full app import).
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.api.admin_mcp import _probe_caller_user_id


class _FakeSecretsRepo:
    def __init__(self, has_row: bool):
        self._has_row = has_row

    def get(self, source_id, user_id):
        return "secret-value" if self._has_row else None


class _FakeOAuthTokensRepo:
    def __init__(self, has_row: bool):
        self._has_row = has_row

    def has(self, source_id, user_id):
        return self._has_row


def _admin_user():
    return {"id": "admin1"}


def test_shared_scope_never_probes(monkeypatch):
    monkeypatch.setattr("app.api.admin_mcp.per_user_secrets_repo", lambda: _FakeSecretsRepo(True))
    monkeypatch.setattr("src.repositories.mcp_user_oauth_tokens_repo", lambda: _FakeOAuthTokensRepo(True))
    src = {"id": "src1", "scope": "shared", "auth_method": "oauth"}
    assert _probe_caller_user_id(src, _admin_user()) is None


def test_oauth_source_with_admin_connection_probes_as_admin(monkeypatch):
    monkeypatch.setattr("app.api.admin_mcp.per_user_secrets_repo", lambda: _FakeSecretsRepo(False))
    monkeypatch.setattr("src.repositories.mcp_user_oauth_tokens_repo", lambda: _FakeOAuthTokensRepo(True))
    src = {"id": "src1", "scope": "per_user", "auth_method": "oauth"}
    assert _probe_caller_user_id(src, _admin_user()) == "admin1"


def test_oauth_source_without_admin_connection_falls_back_caller_less(monkeypatch):
    monkeypatch.setattr("app.api.admin_mcp.per_user_secrets_repo", lambda: _FakeSecretsRepo(False))
    monkeypatch.setattr("src.repositories.mcp_user_oauth_tokens_repo", lambda: _FakeOAuthTokensRepo(False))
    src = {"id": "src1", "scope": "per_user", "auth_method": "oauth"}
    assert _probe_caller_user_id(src, _admin_user()) is None


def test_secret_backed_per_user_source_still_prefers_secret_row(monkeypatch):
    """Non-oauth per_user sources keep the pre-existing behavior — the
    oauth lookup should not even be reached when the secret row exists."""

    def _boom():
        raise AssertionError("must not consult mcp_user_oauth_tokens for a non-oauth source")

    monkeypatch.setattr("app.api.admin_mcp.per_user_secrets_repo", lambda: _FakeSecretsRepo(True))
    monkeypatch.setattr("src.repositories.mcp_user_oauth_tokens_repo", _boom)
    src = {"id": "src1", "scope": "per_user", "auth_method": "bearer"}
    assert _probe_caller_user_id(src, _admin_user()) == "admin1"


def test_secret_backed_per_user_source_with_no_secret_still_checks_oauth_and_finds_none(monkeypatch):
    """A non-oauth source has no oauth row either — falls back caller-less,
    same as before this feature existed (belt-and-suspenders: the extra
    oauth check is harmless for a bearer/basic source since it will just
    return False)."""
    monkeypatch.setattr("app.api.admin_mcp.per_user_secrets_repo", lambda: _FakeSecretsRepo(False))
    monkeypatch.setattr("src.repositories.mcp_user_oauth_tokens_repo", lambda: _FakeOAuthTokensRepo(False))
    src = {"id": "src1", "scope": "per_user", "auth_method": "bearer"}
    assert _probe_caller_user_id(src, _admin_user()) is None
