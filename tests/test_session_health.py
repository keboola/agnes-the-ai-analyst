"""Regression coverage for cli.lib.session_health.session_upload_health.

Issue #244, adapted to scan-based push: flag sessions on disk in the
workspace's encoded Claude Code folder that aren't recorded in
``<workspace>/.claude/agnes-sessions-uploaded.txt`` within a sliding window.
We point ``CLAUDE_CONFIG_DIR`` at a tmp tree so ``session_paths`` resolves the
session folder there, and write ledger rows in the current
``<session_id>\\t<size>\\t<iso>`` format (with a legacy ``<iso>\\t<path>`` row
proving backward-compatible parsing).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli.lib.session_health import session_upload_health
from cli.lib.session_paths import encode_workspace


def _set_projects_root(monkeypatch, root: Path) -> None:
    """Make `session_paths.projects_root()` resolve under *root*/projects."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))


def _make_session_file(cc_root: Path, workspace: Path, name: str, age_days: float) -> Path:
    target = cc_root / "projects" / encode_workspace(workspace)
    target.mkdir(parents=True, exist_ok=True)
    f = target / name
    f.write_text("{}\n", encoding="utf-8")
    age = time.time() - (age_days * 86400)
    os.utime(f, (age, age))
    return f


def _append_uploaded(workspace: Path, when: datetime, session_id: str, size: int = 10) -> None:
    (workspace / ".claude").mkdir(parents=True, exist_ok=True)
    log = workspace / ".claude" / "agnes-sessions-uploaded.txt"
    line = f"{session_id}\t{size}\t{when.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
    with open(log, "a", encoding="utf-8") as f:
        f.write(line)


def test_no_sessions_returns_info(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _set_projects_root(monkeypatch, tmp_path / "cc")
    r = session_upload_health(workspace)
    assert r["status"] == "info"
    assert r["expected_sessions"] == 0
    assert r["uploaded_entries"] == 0


def test_aligned_counts_returns_ok(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cc = tmp_path / "cc"
    _set_projects_root(monkeypatch, cc)
    for i in range(3):
        _make_session_file(cc, workspace, f"s{i}.jsonl", age_days=2)
    now = datetime.now(timezone.utc)
    for i in range(3):
        _append_uploaded(workspace, now - timedelta(days=2, hours=i), f"s{i}")
    r = session_upload_health(workspace)
    assert r["status"] == "ok"
    assert r["expected_sessions"] == 3
    assert r["uploaded_entries"] == 3


def test_silent_breakage_returns_warning(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cc = tmp_path / "cc"
    _set_projects_root(monkeypatch, cc)
    for i in range(10):
        _make_session_file(cc, workspace, f"s{i}.jsonl", age_days=2)
    now = datetime.now(timezone.utc)
    for i in range(2):
        _append_uploaded(workspace, now - timedelta(days=1), f"s{i}")
    r = session_upload_health(workspace)
    assert r["status"] == "warning"
    assert r["expected_sessions"] == 10
    assert r["uploaded_entries"] == 2
    assert "session upload may be failing" in r["detail"]


def test_older_sessions_outside_window_ignored(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cc = tmp_path / "cc"
    _set_projects_root(monkeypatch, cc)
    for i in range(5):
        _make_session_file(cc, workspace, f"old{i}.jsonl", age_days=60)
    _make_session_file(cc, workspace, "recent.jsonl", age_days=2)
    now = datetime.now(timezone.utc)
    _append_uploaded(workspace, now - timedelta(days=2), "recent")
    r = session_upload_health(workspace, window_days=7)
    assert r["status"] == "ok"
    assert r["expected_sessions"] == 1
    assert r["uploaded_entries"] == 1


def test_uploaded_entries_outside_window_ignored(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cc = tmp_path / "cc"
    _set_projects_root(monkeypatch, cc)
    for i in range(10):
        _make_session_file(cc, workspace, f"s{i}.jsonl", age_days=1)
    now = datetime.now(timezone.utc)
    for i in range(8):
        _append_uploaded(workspace, now - timedelta(days=60), f"s{i}")
    r = session_upload_health(workspace, window_days=7)
    assert r["status"] == "warning"
    assert r["expected_sessions"] == 10
    assert r["uploaded_entries"] == 0


def test_threshold_respected(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cc = tmp_path / "cc"
    _set_projects_root(monkeypatch, cc)
    for i in range(5):
        _make_session_file(cc, workspace, f"s{i}.jsonl", age_days=1)
    now = datetime.now(timezone.utc)
    for i in range(3):
        _append_uploaded(workspace, now - timedelta(days=1), f"s{i}")
    r = session_upload_health(workspace, window_days=7, threshold=3)
    assert r["status"] == "ok"
    assert r["expected_sessions"] == 5
    assert r["uploaded_entries"] == 3


def test_malformed_and_legacy_uploaded_log_lines(tmp_path, monkeypatch):
    """Garbage rows don't crash the check; both the current
    ``sid\\tsize\\tiso`` format and the legacy ``iso\\tpath`` format count
    (the timestamp is found whether it's the last or the first field)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".claude").mkdir()
    cc = tmp_path / "cc"
    _set_projects_root(monkeypatch, cc)
    for i in range(3):
        _make_session_file(cc, workspace, f"s{i}.jsonl", age_days=1)

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = workspace / ".claude" / "agnes-sessions-uploaded.txt"
    log.write_text(
        "totally bogus line\n"
        "\n"
        f"s0\t10\t{recent}\n"          # current format
        f"{recent}\t/p/legacy.jsonl\n"  # legacy format — still counted
        "not-a-timestamp\tnope\n",
        encoding="utf-8",
    )
    r = session_upload_health(workspace, window_days=7, threshold=0)
    assert r["expected_sessions"] == 3
    assert r["uploaded_entries"] == 2
