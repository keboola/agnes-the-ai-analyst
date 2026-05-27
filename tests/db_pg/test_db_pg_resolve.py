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
