"""#754 — the Keboola extractor subprocess's per-table failures must reach
``sync_state`` (not just a 500-char stdout tail in server logs).

The subprocess can't write ``system.duckdb`` itself (the parent holds the
lock for the duration of the sync — see ``app.api.sync``'s module
docstring on ``_sync_lock``), so it prints its stats dict as the final
line of stdout. Pre-fix, ``_run_sync`` read only the subprocess exit code
and discarded that line beyond a truncated log — an admin staring at
"5 total, 0 synced" in the dashboard had no way to learn WHY without
trawling container logs.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_sync_lock():
    from app.api import sync as sync_mod

    if sync_mod._sync_lock.locked():
        sync_mod._sync_lock.release()
    sync_mod._recent_trigger_at = 0.0
    yield
    if sync_mod._sync_lock.locked():
        sync_mod._sync_lock.release()
    sync_mod._recent_trigger_at = 0.0


def _fake_popen(stdout: str, stderr: str = "", returncode: int = 2):
    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 4242
            self.returncode = returncode

        def communicate(self, input=None, timeout=None):
            return (stdout, stderr)

    return _FakePopen


def _patch_common(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "test-token")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://test.example")

    from app import instance_config as ic_mod

    monkeypatch.setattr(ic_mod, "get_data_source_type", lambda: "keboola")
    monkeypatch.setattr(ic_mod, "get_value", lambda *a, **kw: "")

    from src.repositories.table_registry import TableRegistryRepository

    monkeypatch.setattr(
        TableRegistryRepository,
        "list_local",
        lambda self, *a, **kw: [
            {
                "id": "bad_table",
                "name": "bad_table",
                "source_type": "keboola",
                "bucket": "in.c-x",
                "source_table": "y",
                "query_mode": "local",
            }
        ],
    )

    from src import orchestrator as orch_mod

    monkeypatch.setattr(
        orch_mod,
        "SyncOrchestrator",
        lambda *a, **kw: MagicMock(rebuild=MagicMock(return_value={})),
        raising=False,
    )


def test_extractor_per_table_error_persisted_to_sync_state(tmp_path, monkeypatch):
    """A `tables_failed=1` stats line with a real per-table error must land
    in sync_state as `status='error'` with that exact message — not the
    generic "see server logs" placeholder."""
    _patch_common(monkeypatch, tmp_path)

    stats = {
        "tables_extracted": 0,
        "tables_failed": 1,
        "errors": [{"table": "bad_table", "error": "unsafe identifier: bad_table"}],
    }
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(json.dumps(stats)))

    from app.api import sync as sync_mod

    sync_mod._run_sync()

    from src.repositories import sync_state_repo

    state = sync_state_repo().get_table_state("bad_table")

    assert state is not None
    assert state["status"] == "error"
    assert state["error"] == "unsafe identifier: bad_table"


def test_extractor_stats_errors_reach_webhook_notifier(tmp_path, monkeypatch):
    """The real per-table error (not the generic placeholder) is what the
    operator webhook alert carries too."""
    _patch_common(monkeypatch, tmp_path)

    stats = {
        "tables_extracted": 0,
        "tables_failed": 1,
        "errors": [{"table": "bad_table", "error": "boom"}],
    }
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(json.dumps(stats)))

    captured = {}

    def _spy_notify(*, failed_tables, fatal):
        captured["failed_tables"] = failed_tables
        captured["fatal"] = fatal

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    from app.api import sync as sync_mod

    sync_mod._run_sync()

    assert {"table": "bad_table", "error": "boom"} in captured["failed_tables"]
    # The generic "(keboola extractor)" placeholder must NOT also appear —
    # the real per-table entry supersedes it.
    assert not any(e["table"] == "(keboola extractor)" for e in captured["failed_tables"])


def test_malformed_stdout_falls_back_to_generic_message(tmp_path, monkeypatch):
    """A garbled/truncated stdout line (subprocess killed mid-flush) must
    not blow up the sync — falls back to the pre-existing generic
    exit-code message instead of a per-table one."""
    _patch_common(monkeypatch, tmp_path)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen("not json {{{", returncode=1))

    captured = {}

    def _spy_notify(*, failed_tables, fatal):
        captured["failed_tables"] = failed_tables
        captured["fatal"] = fatal

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    from app.api import sync as sync_mod

    sync_mod._run_sync()  # must not raise

    assert any(e["table"] == "(keboola extractor)" for e in captured["failed_tables"])


def test_clean_stats_no_errors_records_nothing(tmp_path, monkeypatch):
    """A clean `tables_failed=0` stats line must not touch sync_state or
    fire the webhook notifier."""
    _patch_common(monkeypatch, tmp_path)

    stats = {"tables_extracted": 1, "tables_failed": 0, "errors": []}
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(json.dumps(stats), returncode=0))

    called = {"n": 0}
    monkeypatch.setattr(
        "app.services.sync_notifier.notify_sync_failure",
        lambda **kw: called.__setitem__("n", called["n"] + 1),
    )

    from app.api import sync as sync_mod

    sync_mod._run_sync()

    assert called["n"] == 0
