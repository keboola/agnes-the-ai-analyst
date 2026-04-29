"""Tests for the scheduled-script runner — repo claim/release primitives,
the run-due endpoint, and Pydantic validation on DeployScriptRequest."""

from datetime import datetime, timezone

import pytest

from src.db import get_system_db
from src.repositories.notifications import ScriptRepository


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """Fresh system.duckdb in a tmp dir — uses real schema, no mocks."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    c = get_system_db()
    yield c
    c.close()


def _deploy(repo: ScriptRepository, script_id="s1", schedule="every 1h"):
    repo.deploy(id=script_id, name=script_id, owner="u1",
                schedule=schedule, source="print('hi')")


# ---------------- claim_for_run ---------------------------------------------

def test_claim_for_run_succeeds_when_idle(conn):
    repo = ScriptRepository(conn)
    _deploy(repo)
    assert repo.claim_for_run("s1") is True
    row = repo.get("s1")
    assert row["last_status"] == "running"
    assert row["last_run"] is not None


def test_claim_for_run_fails_when_already_running(conn):
    repo = ScriptRepository(conn)
    _deploy(repo)
    assert repo.claim_for_run("s1") is True
    # Second claim should fail because last_status is still 'running'.
    assert repo.claim_for_run("s1") is False


def test_claim_for_run_succeeds_after_completion(conn):
    repo = ScriptRepository(conn)
    _deploy(repo)
    repo.claim_for_run("s1")
    repo.record_run_result("s1", status="success")
    # Now claimable again.
    assert repo.claim_for_run("s1") is True


def test_claim_for_run_returns_false_for_unknown_script(conn):
    repo = ScriptRepository(conn)
    assert repo.claim_for_run("does-not-exist") is False


# ---------------- record_run_result -----------------------------------------

@pytest.mark.parametrize("status", ["success", "failure"])
def test_record_run_result_writes_terminal_status(conn, status):
    repo = ScriptRepository(conn)
    _deploy(repo)
    repo.claim_for_run("s1")
    repo.record_run_result("s1", status=status)
    row = repo.get("s1")
    assert row["last_status"] == status


def test_record_run_result_rejects_running_as_terminal(conn):
    """The 'running' string is reserved for claim_for_run; record_run_result
    must reject it so a caller can't accidentally re-arm the running flag
    instead of clearing it."""
    repo = ScriptRepository(conn)
    _deploy(repo)
    repo.claim_for_run("s1")
    with pytest.raises(ValueError):
        repo.record_run_result("s1", status="running")
