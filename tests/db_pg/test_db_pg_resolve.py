"""URL resolution precedence: instance.yaml → DATABASE_URL → AGNES_DB_URL."""
from __future__ import annotations
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clear_envs(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)


def test_resolve_prefers_instance_yaml(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR, url="postgresql://from-yaml/agnes")

    # Also set env to a different value — yaml must win
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-env/agnes")

    from src.db_pg import _resolve_url
    assert _resolve_url() == "postgresql://from-yaml/agnes"


def test_resolve_falls_back_to_database_url_env(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-env/agnes")

    from src.db_pg import _resolve_url
    assert _resolve_url() == "postgresql://from-env/agnes"


def test_resolve_falls_back_to_agnes_db_url_env(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    monkeypatch.setenv("AGNES_DB_URL", "postgresql://legacy/agnes")

    from src.db_pg import _resolve_url
    assert _resolve_url() == "postgresql://legacy/agnes"


def test_resolve_raises_when_all_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    from src.db_pg import _resolve_url
    with pytest.raises(RuntimeError, match="Postgres URL is unset"):
        _resolve_url()


def test_dispose_engine_clears_singleton(pg_engine, monkeypatch):
    """After dispose_engine(), next get_engine() re-resolves URL."""
    # Point the singleton's URL resolver at the per-test pg_engine
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))

    import src.db_pg as db_pg
    db_pg.dispose()  # ensure clean starting state

    first = db_pg.get_engine()
    db_pg.dispose_engine()
    # Internal singleton should be None now; get_engine recreates
    second = db_pg.get_engine()
    # Different engine instances (re-resolution happened)
    assert first is not second

    # Cleanup so following tests start with no singleton bound to this URL
    db_pg.dispose()
