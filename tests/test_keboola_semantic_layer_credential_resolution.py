"""Credential resolution for the Keboola semantic-layer sync.

Verified live (2026-07-22, agnes-dev): `sync_semantic_layer()` only ever
checked the legacy `KEBOOLA_STACK_URL`/`KEBOOLA_STORAGE_TOKEN` env-or-vault
slot — never the named `source_connections` + `connection_secrets` vault
the "Add Keboola project" admin wizard (`/admin/data-sources`) manages. An
instance that connects a project only through that wizard (the modern
path, multi-project capable) silently fails every semantic-layer sync with
"credentials not configured", even though the same connection's regular
table syncs and its own `/test` endpoint both work fine.

`_resolve_keboola_credentials()` adds the named-connection as a fallback,
never changing behavior when the legacy slot is already populated (every
pre-existing `sync_semantic_layer()` test patches env directly and must
keep passing unchanged).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())


def _make_keboola_connection(*, is_default: bool, with_vault_secret: bool, token_env: str = ""):
    from src.repositories import connection_secrets_repo, source_connections_repo
    from uuid import uuid4

    conn_id = str(uuid4())
    source_connections_repo().create(
        id=conn_id,
        name=f"conn-{conn_id[:8]}",
        source_type="keboola",
        config={"stack_url": "https://connection.named.keboola.com"},
        token_env=token_env or None,
        is_default=is_default,
        created_by="test",
    )
    if with_vault_secret:
        connection_secrets_repo().upsert(conn_id, "named-conn-vault-token")
    return conn_id


class TestResolveKeboolaCredentials:
    def test_explicit_args_take_precedence(self, e2e_env, monkeypatch):
        from connectors.keboola.semantic_layer import _resolve_keboola_credentials

        monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.env.keboola.com")
        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "env-token")
        _make_keboola_connection(is_default=True, with_vault_secret=True)

        url, token = _resolve_keboola_credentials("https://explicit.example.com", "explicit-token")

        assert (url, token) == ("https://explicit.example.com", "explicit-token")

    def test_legacy_env_takes_precedence_over_named_connection(self, e2e_env, monkeypatch):
        from connectors.keboola.semantic_layer import _resolve_keboola_credentials

        monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.env.keboola.com")
        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "env-token")
        _make_keboola_connection(is_default=True, with_vault_secret=True)

        url, token = _resolve_keboola_credentials(None, None)

        assert (url, token) == ("https://connection.env.keboola.com", "env-token")

    def test_falls_back_to_default_named_connection_vault_secret(self, e2e_env, monkeypatch):
        from connectors.keboola.semantic_layer import _resolve_keboola_credentials

        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)
        _make_keboola_connection(is_default=True, with_vault_secret=True)

        url, token = _resolve_keboola_credentials(None, None)

        assert url == "https://connection.named.keboola.com"
        assert token == "named-conn-vault-token"

    def test_falls_back_to_first_connection_when_none_marked_default(self, e2e_env, monkeypatch):
        from connectors.keboola.semantic_layer import _resolve_keboola_credentials

        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)
        _make_keboola_connection(is_default=False, with_vault_secret=True)

        url, token = _resolve_keboola_credentials(None, None)

        assert url == "https://connection.named.keboola.com"
        assert token == "named-conn-vault-token"

    def test_falls_back_to_connections_own_token_env_when_no_vault_secret(self, e2e_env, monkeypatch):
        from connectors.keboola.semantic_layer import _resolve_keboola_credentials

        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)
        monkeypatch.setenv("CUSTOM_KBC_TOKEN_ENV", "token-from-custom-env")
        _make_keboola_connection(is_default=True, with_vault_secret=False, token_env="CUSTOM_KBC_TOKEN_ENV")

        url, token = _resolve_keboola_credentials(None, None)

        assert url == "https://connection.named.keboola.com"
        assert token == "token-from-custom-env"

    def test_partial_legacy_url_falls_through_to_named_connection_pair(self, e2e_env, monkeypatch):
        """Only ``KEBOOLA_STACK_URL`` set (no token) — a partial legacy pair
        must not mix with the named connection's token; the whole legacy
        tier is discarded and the named connection resolves as a coherent
        pair (Devin Review on #992)."""
        from connectors.keboola.semantic_layer import _resolve_keboola_credentials

        monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.env.keboola.com")
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)
        _make_keboola_connection(is_default=True, with_vault_secret=True)

        url, token = _resolve_keboola_credentials(None, None)

        assert url == "https://connection.named.keboola.com"
        assert token == "named-conn-vault-token"

    def test_partial_legacy_token_falls_through_to_named_connection_pair(self, e2e_env, monkeypatch):
        """Only ``KEBOOLA_STORAGE_TOKEN`` set (no URL) — same as above,
        mirrored on the token half."""
        from connectors.keboola.semantic_layer import _resolve_keboola_credentials

        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "env-token")
        _make_keboola_connection(is_default=True, with_vault_secret=True)

        url, token = _resolve_keboola_credentials(None, None)

        assert url == "https://connection.named.keboola.com"
        assert token == "named-conn-vault-token"

    def test_returns_empty_when_nothing_resolves(self, e2e_env, monkeypatch):
        from connectors.keboola.semantic_layer import _resolve_keboola_credentials

        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)

        url, token = _resolve_keboola_credentials(None, None)

        assert (url, token) == ("", "")

    def test_non_keboola_connections_are_ignored(self, e2e_env, monkeypatch):
        """A BigQuery-type named connection must never be picked up as a
        Keboola credential source."""
        from connectors.keboola.semantic_layer import _resolve_keboola_credentials
        from src.repositories import source_connections_repo, connection_secrets_repo
        from uuid import uuid4

        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)

        bq_id = str(uuid4())
        source_connections_repo().create(
            id=bq_id,
            name="bq-conn",
            source_type="bigquery",
            config={"project_id": "some-project"},
            is_default=True,
            created_by="test",
        )
        connection_secrets_repo().upsert(bq_id, "bq-secret")

        url, token = _resolve_keboola_credentials(None, None)

        assert (url, token) == ("", "")
