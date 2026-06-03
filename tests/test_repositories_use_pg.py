"""use_pg() precedence: instance.yaml::database.backend → env var."""
from __future__ import annotations
import pytest


@pytest.fixture(autouse=True)
def clear_envs(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)


def test_use_pg_true_when_yaml_says_side_car(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")

    from src.repositories import use_pg
    assert use_pg() is True


def test_use_pg_false_when_yaml_says_duckdb(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.DUCKDB)

    from src.repositories import use_pg
    assert use_pg() is False


def test_use_pg_falls_back_to_env_when_yaml_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/agnes")

    from src.repositories import use_pg
    assert use_pg() is True


def test_use_pg_false_when_nothing_set(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")

    from src.repositories import use_pg
    assert use_pg() is False
