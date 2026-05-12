"""Regression coverage for cli.lib.session_health.capture_session_health.

Issue #244 — flag silently-broken `agnes capture-session` by comparing
session files in `~/.claude/projects/<encoded>/` against entries in
`<workspace>/.claude/agnes-sessions-uploaded.txt` within a sliding
window.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _set_home(monkeypatch, tmp_path):
    """Override the module-level ``_PROJECTS_DIR`` (evaluated once at
    import via ``Path.home()``) so the check reads from a controlled
    ``~/.claude/projects/`` tree under tmp_path."""
    import cli.lib.claude_sessions as cs
    monkeypatch.setattr(cs, "_PROJECTS_DIR", tmp_path / ".claude" / "projects")


def _make_session_file(home: Path, workspace: Path, name: str, age_days: float) -> Path:
    """Write an empty jsonl into one of the candidate encoded dirs and
    backdate its mtime."""
    # Use variant-a encoding (slash→dash) — matches the macOS-friendly
    # form cli/lib/claude_sessions.py emits first.
    encoded = str(workspace.resolve()).replace("/", "-")
    target = home / ".claude" / "projects" / encoded
    target.mkdir(parents=True, exist_ok=True)
    f = target / name
    f.write_text("{}\n", encoding="utf-8")
    # Backdate mtime
    age = time.time() - (age_days * 86400)
    os.utime(f, (age, age))
    return f


def _append_uploaded_log(workspace: Path, when: datetime, transcript_path: str) -> None:
    (workspace / ".claude").mkdir(parents=True, exist_ok=True)
    log = workspace / ".claude" / "agnes-sessions-uploaded.txt"
    line = f"{when.strftime('%Y-%m-%dT%H:%M:%SZ')}\t{transcript_path}\n"
    with open(log, "a", encoding="utf-8") as f:
        f.write(line)


def test_no_sessions_returns_info(tmp_path, monkeypatch):
    """Fresh workspace with no SessionStart events → info, not warning."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _set_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()

    from cli.lib.session_health import capture_session_health
    r = capture_session_health(workspace)
    assert r["status"] == "info"
    assert r["expected_sessions"] == 0
    assert r["uploaded_entries"] == 0


def test_aligned_counts_returns_ok(tmp_path, monkeypatch):
    """SessionStart events match uploaded-log entries → ok."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _set_home(monkeypatch, home)

    # 3 recent sessions
    for i in range(3):
        _make_session_file(home, workspace, f"s{i}.jsonl", age_days=2)
    now = datetime.now(timezone.utc)
    for i in range(3):
        _append_uploaded_log(workspace, now - timedelta(days=2, hours=i),
                             f"/path/s{i}.jsonl")

    from cli.lib.session_health import capture_session_health
    r = capture_session_health(workspace)
    assert r["status"] == "ok"
    assert r["expected_sessions"] == 3
    assert r["uploaded_entries"] == 3


def test_silent_breakage_returns_warning(tmp_path, monkeypatch):
    """SessionStart events ≫ uploaded entries (delta > threshold) → warning."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _set_home(monkeypatch, home)

    # 10 recent SessionStart events
    for i in range(10):
        _make_session_file(home, workspace, f"s{i}.jsonl", age_days=2)
    # only 2 uploads — capture-session silently dropped 8
    now = datetime.now(timezone.utc)
    for i in range(2):
        _append_uploaded_log(workspace, now - timedelta(days=1), f"/p{i}.jsonl")

    from cli.lib.session_health import capture_session_health
    r = capture_session_health(workspace)
    assert r["status"] == "warning"
    assert r["expected_sessions"] == 10
    assert r["uploaded_entries"] == 2
    assert "capture-session may be silently failing" in r["detail"]


def test_older_sessions_outside_window_ignored(tmp_path, monkeypatch):
    """Sessions outside the window must not count toward expected."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _set_home(monkeypatch, home)

    # 5 ancient sessions (60d ago) + 1 recent
    for i in range(5):
        _make_session_file(home, workspace, f"old{i}.jsonl", age_days=60)
    _make_session_file(home, workspace, "recent.jsonl", age_days=2)
    now = datetime.now(timezone.utc)
    _append_uploaded_log(workspace, now - timedelta(days=2), "/p/recent.jsonl")

    from cli.lib.session_health import capture_session_health
    r = capture_session_health(workspace, window_days=7)
    assert r["status"] == "ok"
    assert r["expected_sessions"] == 1
    assert r["uploaded_entries"] == 1


def test_uploaded_entries_outside_window_ignored(tmp_path, monkeypatch):
    """Old uploaded-log entries don't count even if SessionStart count is high."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _set_home(monkeypatch, home)

    for i in range(10):
        _make_session_file(home, workspace, f"s{i}.jsonl", age_days=1)
    # 8 uploads but ancient — outside window
    now = datetime.now(timezone.utc)
    for i in range(8):
        _append_uploaded_log(workspace, now - timedelta(days=60),
                             f"/p{i}.jsonl")

    from cli.lib.session_health import capture_session_health
    r = capture_session_health(workspace, window_days=7)
    assert r["status"] == "warning"
    assert r["expected_sessions"] == 10
    assert r["uploaded_entries"] == 0


def test_threshold_respected(tmp_path, monkeypatch):
    """Delta within threshold stays ok (a couple unsynced sessions is fine)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _set_home(monkeypatch, home)

    for i in range(5):
        _make_session_file(home, workspace, f"s{i}.jsonl", age_days=1)
    now = datetime.now(timezone.utc)
    # 3 uploads of 5 events → delta=2, threshold=3 → still ok
    for i in range(3):
        _append_uploaded_log(workspace, now - timedelta(days=1), f"/p{i}.jsonl")

    from cli.lib.session_health import capture_session_health
    r = capture_session_health(workspace, window_days=7, threshold=3)
    assert r["status"] == "ok"
    assert r["expected_sessions"] == 5
    assert r["uploaded_entries"] == 3


def test_malformed_uploaded_log_lines_skipped(tmp_path, monkeypatch):
    """Garbage in uploaded-log doesn't crash the check; only well-formed
    timestamped lines count."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".claude").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _set_home(monkeypatch, home)

    for i in range(3):
        _make_session_file(home, workspace, f"s{i}.jsonl", age_days=1)

    log = workspace / ".claude" / "agnes-sessions-uploaded.txt"
    now = datetime.now(timezone.utc)
    log.write_text(
        "totally bogus line\n"
        "\n"  # blank
        "no-tab-just-a-path\n"
        f"{(now - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}\t/p.jsonl\n"
        "not-a-timestamp\tstill-has-a-tab\n",
        encoding="utf-8",
    )

    from cli.lib.session_health import capture_session_health
    r = capture_session_health(workspace, window_days=7, threshold=3)
    assert r["expected_sessions"] == 3
    assert r["uploaded_entries"] == 1
