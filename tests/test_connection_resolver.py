"""Resolver: (source_type, connection_id|None) -> connection; token chain."""

import pytest

from src.connection_resolver import resolve_connection, resolve_token


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    # route the factory at a throwaway DuckDB system DB
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    repo.create(
        id="c1",
        name="kbc",
        source_type="keboola",
        config={"stack_url": "https://a.example.com"},
        token_env="MY_KBC_TOKEN",
        is_default=True,
    )
    repo.create(
        id="c2",
        name="kbc_eu",
        source_type="keboola",
        config={"stack_url": "https://eu.example.com"},
    )
    return repo


def test_resolves_explicit_then_default(seeded_repo):
    assert resolve_connection("keboola", "c2")["name"] == "kbc_eu"
    assert resolve_connection("keboola", None)["name"] == "kbc"  # default
    assert resolve_connection("bigquery", None) is None  # none registered


def test_token_chain_vault_then_env(seeded_repo, monkeypatch):
    conn = resolve_connection("keboola", None)
    monkeypatch.setenv("MY_KBC_TOKEN", "tok-from-env")
    assert resolve_token(conn) == "tok-from-env"  # env fallback
    from src.repositories import connection_secrets_repo

    connection_secrets_repo().upsert("c1", "tok-from-vault")
    assert resolve_token(conn) == "tok-from-vault"  # vault wins
