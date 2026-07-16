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


def test_use_pg_parses_overlay_once_across_many_calls(tmp_path, monkeypatch):
    """read_backend_state memoizes the parsed overlay, so a burst
    of repo-factory calls (each of which runs use_pg) parses the YAML ONCE,
    not once per call. Pre-fix this was the ~2 req/s catalog throughput
    ceiling — dozens of yaml.safe_load per request holding the GIL."""
    import src.db_state_machine as sm
    from src.db_state_machine import BackendState, reset_backend_state_cache, write_backend_state

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")
    reset_backend_state_cache()  # simulate a fresh process (cold cache)

    parses = {"n": 0}
    real_safe_load = sm.yaml.safe_load

    def _counting_safe_load(*args, **kwargs):
        parses["n"] += 1
        return real_safe_load(*args, **kwargs)

    monkeypatch.setattr(sm.yaml, "safe_load", _counting_safe_load)

    from src.repositories import use_pg

    for _ in range(25):
        assert use_pg() is True

    assert parses["n"] == 1, f"overlay parsed {parses['n']}x across 25 use_pg() calls; expected 1"
