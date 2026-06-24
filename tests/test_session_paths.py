"""Tests for cli/lib/session_paths.py — the Claude Code session-folder encoder.

The encoding is the load-bearing fix: every non-alphanumeric char becomes ``-``
with NO run-collapsing. The ground-truth case is a Windows path, where ``C:\\``
must encode to ``C--`` (drive-letter colon + backslash, two dashes) — an older
collapse-runs variant produced ``C-`` and pointed at a non-existent folder.
"""

from __future__ import annotations

from pathlib import Path

from cli.lib.session_paths import (
    encode_workspace,
    list_session_files,
    projects_root,
    session_dir,
)


def test_encode_windows_path_no_collapse():
    # Ground truth: this is how Claude Code names the folder on Windows — the
    # drive-letter `:` and the first `\` each become a `-`, so `C:\` yields
    # `C--`. An older collapse-runs variant produced `C-` and pointed at a
    # non-existent folder. (Generic placeholder paths; vendor-agnostic repo.)
    assert encode_workspace(r"C:\Users\analyst\Workspace") == "C--Users-analyst-Workspace"
    assert encode_workspace(r"C:\Work\ExampleOrg\Data") == "C--Work-ExampleOrg-Data"


def test_encode_posix_path():
    assert encode_workspace("/Users/me/Workspace") == "-Users-me-Workspace"
    assert encode_workspace("/home/me/work space") == "-home-me-work-space"


def test_encode_does_not_collapse_consecutive_dashes():
    # Two adjacent non-alnum chars → two dashes, never one.
    assert encode_workspace("a..b") == "a--b"
    assert encode_workspace("a__b") == "a--b"


def test_encode_strips_single_trailing_separator():
    assert encode_workspace("/a/b/") == encode_workspace("/a/b")
    assert encode_workspace("C:\\a\\b\\") == encode_workspace("C:\\a\\b")


def test_encode_accepts_path_object():
    assert encode_workspace(Path("/a/b")) == "-a-b"


def test_projects_root_honors_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert projects_root() == tmp_path / "cc" / "projects"


def test_projects_root_defaults_to_home(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert projects_root() == Path.home() / ".claude" / "projects"


def test_list_session_files_reads_encoded_folder(monkeypatch, tmp_path):
    cc = tmp_path / "cc"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
    workspace = tmp_path / "ws"
    folder = session_dir(workspace)
    folder.mkdir(parents=True)
    (folder / "b.jsonl").write_text("{}\n")
    (folder / "a.jsonl").write_text("{}\n")
    (folder / "ignore.txt").write_text("nope")

    files = list_session_files(workspace)
    # Sorted by filename, only *.jsonl.
    assert [p.name for p in files] == ["a.jsonl", "b.jsonl"]


def test_list_session_files_missing_folder_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert list_session_files(tmp_path / "ws") == []
