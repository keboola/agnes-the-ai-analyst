"""Tests for the scheduled-script runner — repo claim/release primitives,
the run-due endpoint, and Pydantic validation on DeployScriptRequest."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.api.scripts import DeployScriptRequest
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


# ---------------- DeployScriptRequest.schedule validation -------------------

def test_deploy_request_accepts_valid_schedule():
    req = DeployScriptRequest(name="report", source="print(1)", schedule="every 1h")
    assert req.schedule == "every 1h"


def test_deploy_request_accepts_no_schedule():
    req = DeployScriptRequest(name="report", source="print(1)")
    assert req.schedule is None


def test_deploy_request_rejects_malformed_schedule():
    with pytest.raises(ValidationError):
        DeployScriptRequest(name="report", source="print(1)", schedule="weekly")


# ---------------- /api/scripts/run-due endpoint -----------------------------

def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_run_due_skips_scripts_without_schedule(seeded_app, monkeypatch):
    """A script with schedule=NULL is never picked up by run-due (those
    are run only via explicit POST /api/scripts/{id}/run)."""
    monkeypatch.setattr(
        "app.api.scripts._execute_script",
        lambda src, name: {"name": name, "exit_code": 0, "stdout": "", "stderr": "", "truncated": False},
    )
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    deploy = c.post(
        "/api/scripts/deploy",
        json={"name": "manual-only", "source": "print(1)"},
        headers=_auth(token),
    )
    assert deploy.status_code == 201
    resp = c.post("/api/scripts/run-due", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["claimed"] == []


def test_run_due_claims_due_scripts(seeded_app, monkeypatch):
    """A script on 'every 1h' that has never run gets claimed and executed."""
    calls = []

    def _fake_exec(source, name):
        calls.append(name)
        return {"name": name, "exit_code": 0, "stdout": "", "stderr": "", "truncated": False}

    monkeypatch.setattr("app.api.scripts._execute_script", _fake_exec)
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    deploy = c.post(
        "/api/scripts/deploy",
        json={"name": "report", "source": "print(1)", "schedule": "every 1h"},
        headers=_auth(token),
    )
    assert deploy.status_code == 201
    script_id = deploy.json()["id"]
    resp = c.post("/api/scripts/run-due", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["claimed"] == [script_id]
    # BackgroundTasks runs synchronously inside TestClient, so the call
    # has happened by now.
    assert "report" in calls


def test_run_due_records_failure_when_script_exits_nonzero(seeded_app, monkeypatch):
    """`_execute_script` returns `{exit_code: N, ...}` for non-zero exits +
    timeouts (only safety violations RAISE). `_run_claimed_script` must
    inspect exit_code rather than treat "no exception" as success — see
    Devin review BUG_0001."""
    monkeypatch.setattr(
        "app.api.scripts._execute_script",
        lambda src, name: {
            "name": name, "exit_code": 1,
            "stdout": "", "stderr": "boom", "truncated": False,
        },
    )
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    deploy = c.post(
        "/api/scripts/deploy",
        json={"name": "broken", "source": "print(1)", "schedule": "every 1h"},
        headers=_auth(token),
    )
    assert deploy.status_code == 201
    script_id = deploy.json()["id"]
    resp = c.post("/api/scripts/run-due", headers=_auth(token))
    assert resp.json()["claimed"] == [script_id]
    # BackgroundTasks runs synchronously inside TestClient — by now the
    # terminal status must be 'failure', not 'success'.
    listing = c.get("/api/scripts", headers=_auth(token)).json()["scripts"]
    row = next(s for s in listing if s["id"] == script_id)
    assert row["last_status"] == "failure", (
        f"non-zero exit_code must record 'failure', got {row['last_status']!r}"
    )


def test_run_due_records_success_when_script_exits_zero(seeded_app, monkeypatch):
    """Mirror of the failure test — exit_code=0 must record 'success'."""
    monkeypatch.setattr(
        "app.api.scripts._execute_script",
        lambda src, name: {
            "name": name, "exit_code": 0,
            "stdout": "ok", "stderr": "", "truncated": False,
        },
    )
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    deploy = c.post(
        "/api/scripts/deploy",
        json={"name": "good", "source": "print(1)", "schedule": "every 1h"},
        headers=_auth(token),
    )
    assert deploy.status_code == 201
    script_id = deploy.json()["id"]
    c.post("/api/scripts/run-due", headers=_auth(token))
    listing = c.get("/api/scripts", headers=_auth(token)).json()["scripts"]
    row = next(s for s in listing if s["id"] == script_id)
    assert row["last_status"] == "success"


def test_run_due_skips_scripts_already_running(seeded_app, monkeypatch):
    """A script in 'running' state must not be re-claimed by a second
    sidecar tick that arrives while the previous run is still going."""
    monkeypatch.setattr(
        "app.api.scripts._execute_script",
        # Simulate a slow run by NOT updating last_status — repo.claim_for_run
        # already wrote 'running'; we leave it that way.
        lambda src, name: {"name": name, "exit_code": 0, "stdout": "", "stderr": "", "truncated": False},
    )
    # Patch out record_run_result so the run never "completes".
    monkeypatch.setattr(
        "src.repositories.notifications.ScriptRepository.record_run_result",
        lambda self, *a, **kw: None,
    )
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    deploy = c.post(
        "/api/scripts/deploy",
        json={"name": "long", "source": "print(1)", "schedule": "every 1h"},
        headers=_auth(token),
    )
    assert deploy.status_code == 201
    script_id = deploy.json()["id"]
    first = c.post("/api/scripts/run-due", headers=_auth(token))
    assert first.json()["claimed"] == [script_id]
    second = c.post("/api/scripts/run-due", headers=_auth(token))
    assert second.json()["claimed"] == []
