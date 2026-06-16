"""First-boot seeding: env/yaml -> default connections; idempotent."""

import pytest

from app.connections_seed import seed_default_connections


@pytest.fixture
def fresh_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import source_connections_repo

    return source_connections_repo()


def test_seeds_keboola_from_env_normalized(fresh_registry, monkeypatch):
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.example.com/")
    monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
    seed_default_connections()
    row = fresh_registry.get_by_name("keboola")
    assert row["is_default"] is True
    assert row["config"]["stack_url"] == "https://connection.example.com"  # slash gone
    assert row["token_env"] == "KEBOOLA_STORAGE_TOKEN"
    assert fresh_registry.get_by_name("bigquery") is None


def test_seeding_is_idempotent(fresh_registry, monkeypatch):
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.example.com")
    seed_default_connections()
    seed_default_connections()  # second boot
    assert len(fresh_registry.list(source_type="keboola")) == 1


def test_existing_registry_not_overwritten(fresh_registry, monkeypatch):
    fresh_registry.create(
        id="c9",
        name="keboola",
        source_type="keboola",
        config={"stack_url": "https://admin-set.example.com"},
        is_default=True,
    )
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://env-says.example.com")
    seed_default_connections()  # must be a no-op + warn
    assert fresh_registry.get_by_name("keboola")["config"]["stack_url"] == "https://admin-set.example.com"


def test_bigquery_env_set_but_registry_exists_warns_no_create(fresh_registry, monkeypatch, caplog):
    fresh_registry.create(
        id="bq9",
        name="bigquery",
        source_type="bigquery",
        config={"project": "admin-proj"},
        is_default=True,
    )
    monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
    monkeypatch.setenv("BIGQUERY_PROJECT", "env-proj")
    with caplog.at_level("WARNING"):
        seed_default_connections()  # must be a no-op + warn
    assert len(fresh_registry.list(source_type="bigquery")) == 1
    assert fresh_registry.get_by_name("bigquery")["config"]["project"] == "admin-proj"
    assert any("BIGQUERY_PROJECT is set" in r.message for r in caplog.records)
