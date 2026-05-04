"""Tests for ``cli.lib.claude_sessions`` — Claude Code session locator.

The locator must handle the two encoding schemes Claude Code uses to map a
workspace cwd to a directory name under ``~/.claude/projects/``:

- **Variant A** (older): replace ``/`` with ``-``, preserve everything else.
- **Variant B** (newer / Windows): replace every non-alphanumeric with ``-``,
  collapse consecutive ``-``.

Tests use ``monkeypatch`` to redirect ``Path.home()`` at the module level so
we can fabricate either encoding under a tmp dir and verify the helper finds
it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.lib import claude_sessions


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~/.claude/projects/`` to a tmp location.

    The module captures ``Path.home() / ".claude" / "projects"`` as a module
    constant at import time, so we patch the constant directly rather than
    monkeypatching ``Path.home``.
    """
    home = tmp_path / "home"
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr(claude_sessions, "_PROJECTS_DIR", projects)
    return home


def test_encode_variant_a_replaces_slashes_only():
    enc = claude_sessions._encode_variant_a("/Users/foo/My Workspace")
    assert enc == "-Users-foo-My Workspace"


def test_encode_variant_b_replaces_all_nonalnum_and_collapses():
    enc = claude_sessions._encode_variant_b("/Users/foo/My Workspace.dir")
    # Spaces, slashes, and dots all become single dashes (collapsed).
    assert enc == "-Users-foo-My-Workspace-dir"


def test_encode_variant_b_handles_windows_path():
    enc = claude_sessions._encode_variant_b("C:\\Users\\foo\\workspace")
    # Backslashes + colon all → '-'; collapsed.
    assert enc == "C-Users-foo-workspace"


def test_find_claude_sessions_dir_variant_a_match(
    fake_home: Path, tmp_path: Path
):
    """Workspace cwd encodes via variant A on disk → helper returns it."""
    workspace = tmp_path / "My Workspace"
    workspace.mkdir()
    encoded = claude_sessions._encode_variant_a(str(workspace.resolve()))
    target = claude_sessions._PROJECTS_DIR / encoded
    target.mkdir()

    found = claude_sessions.find_claude_sessions_dir(workspace)
    assert found == target


def test_find_claude_sessions_dir_variant_b_match(
    fake_home: Path, tmp_path: Path
):
    """Workspace cwd encodes via variant B → helper returns it."""
    workspace = tmp_path / "My.Workspace"  # has dots → variant A and B differ
    workspace.mkdir()

    encoded_b = claude_sessions._encode_variant_b(str(workspace.resolve()))
    encoded_a = claude_sessions._encode_variant_a(str(workspace.resolve()))
    # Sanity: the two encodings really do differ for this fixture.
    assert encoded_a != encoded_b

    target = claude_sessions._PROJECTS_DIR / encoded_b
    target.mkdir()

    found = claude_sessions.find_claude_sessions_dir(workspace)
    assert found == target


def test_find_claude_sessions_dir_no_match_returns_none(
    fake_home: Path, tmp_path: Path
):
    """No encoded dir exists → returns None (caller falls back to legacy)."""
    workspace = tmp_path / "untouched"
    workspace.mkdir()
    assert claude_sessions.find_claude_sessions_dir(workspace) is None


def test_find_claude_sessions_dirs_returns_all_when_both_exist(
    fake_home: Path, tmp_path: Path
):
    """When both encoded dirs exist on disk (older + newer Claude Code
    versions sharing the same cwd), the helper returns BOTH so the caller
    can union their session files.  This matches reality: users who have
    upgraded Claude Code mid-project end up with two sibling project dirs,
    each holding a slice of their session history."""
    workspace = tmp_path / "My.Wkspace"  # ensure A != B
    workspace.mkdir()
    enc_a = claude_sessions._encode_variant_a(str(workspace.resolve()))
    enc_b = claude_sessions._encode_variant_b(str(workspace.resolve()))
    assert enc_a != enc_b
    (claude_sessions._PROJECTS_DIR / enc_a).mkdir()
    (claude_sessions._PROJECTS_DIR / enc_b).mkdir()

    dirs = claude_sessions.find_claude_sessions_dirs(workspace)
    assert set(dirs) == {
        claude_sessions._PROJECTS_DIR / enc_a,
        claude_sessions._PROJECTS_DIR / enc_b,
    }


def test_list_session_files_unions_both_variants(
    fake_home: Path, tmp_path: Path
):
    """When the same workspace has both encoded dirs, files from both must
    surface in the listing — that's the whole point of probing both."""
    workspace = tmp_path / "My.Wkspace"
    workspace.mkdir()
    enc_a = claude_sessions._encode_variant_a(str(workspace.resolve()))
    enc_b = claude_sessions._encode_variant_b(str(workspace.resolve()))
    assert enc_a != enc_b
    dir_a = claude_sessions._PROJECTS_DIR / enc_a
    dir_b = claude_sessions._PROJECTS_DIR / enc_b
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "old.jsonl").write_text("{}\n")
    (dir_b / "new.jsonl").write_text("{}\n")

    files = claude_sessions.list_session_files(workspace)
    assert sorted(f.name for f in files) == ["new.jsonl", "old.jsonl"]


def test_list_session_files_picks_newest_when_same_name_in_both_variants(
    fake_home: Path, tmp_path: Path
):
    """Same session id under both encoded dirs → take the most recently
    modified copy.  Models the case where Claude Code was upgraded mid-
    session and re-wrote the same id under the new encoding."""
    import os
    import time

    workspace = tmp_path / "My.Wkspace"
    workspace.mkdir()
    enc_a = claude_sessions._encode_variant_a(str(workspace.resolve()))
    enc_b = claude_sessions._encode_variant_b(str(workspace.resolve()))
    assert enc_a != enc_b
    dir_a = claude_sessions._PROJECTS_DIR / enc_a
    dir_b = claude_sessions._PROJECTS_DIR / enc_b
    dir_a.mkdir()
    dir_b.mkdir()

    older = dir_a / "shared.jsonl"
    older.write_text('{"src":"a-old"}\n')
    # Push older mtime back so the newer write is unambiguously newer.
    past = time.time() - 3600
    os.utime(older, (past, past))

    newer = dir_b / "shared.jsonl"
    newer.write_text('{"src":"b-new"}\n')

    files = claude_sessions.list_session_files(workspace)
    assert len(files) == 1
    assert files[0].read_text() == '{"src":"b-new"}\n'


def test_list_session_files_reads_from_claude_dir(
    fake_home: Path, tmp_path: Path
):
    """When Claude Code wrote sessions to ~/.claude/projects/<enc>/, they
    show up in the list — even though <workspace>/user/sessions/ is empty."""
    workspace = tmp_path / "wkspace"
    workspace.mkdir()
    enc = claude_sessions._encode_variant_a(str(workspace.resolve()))
    target = claude_sessions._PROJECTS_DIR / enc
    target.mkdir()
    (target / "session-1.jsonl").write_text('{"event":"hi"}\n')
    (target / "session-2.jsonl").write_text('{"event":"there"}\n')

    files = claude_sessions.list_session_files(workspace)
    assert [f.name for f in files] == ["session-1.jsonl", "session-2.jsonl"]
    # Each file must come from the Claude dir, not legacy.
    for f in files:
        assert str(f).startswith(str(target))


def test_list_session_files_falls_back_to_legacy(
    fake_home: Path, tmp_path: Path
):
    """No Claude dir exists, but <workspace>/user/sessions/ does → legacy
    files are returned (back-compat for hook-managed mirrors)."""
    workspace = tmp_path / "wkspace"
    workspace.mkdir()
    legacy = workspace / "user" / "sessions"
    legacy.mkdir(parents=True)
    (legacy / "old.jsonl").write_text('{"event":"legacy"}\n')

    files = claude_sessions.list_session_files(workspace)
    assert [f.name for f in files] == ["old.jsonl"]


def test_list_session_files_dedupes_by_name_claude_wins(
    fake_home: Path, tmp_path: Path
):
    """Both Claude dir and legacy dir contain a same-named jsonl. Helper
    returns one entry, sourced from the Claude dir (live writer)."""
    workspace = tmp_path / "wkspace"
    workspace.mkdir()
    enc = claude_sessions._encode_variant_a(str(workspace.resolve()))
    target = claude_sessions._PROJECTS_DIR / enc
    target.mkdir()
    (target / "shared.jsonl").write_text('{"src":"claude"}\n')

    legacy = workspace / "user" / "sessions"
    legacy.mkdir(parents=True)
    (legacy / "shared.jsonl").write_text('{"src":"legacy"}\n')

    files = claude_sessions.list_session_files(workspace)
    assert len(files) == 1
    assert files[0].read_text() == '{"src":"claude"}\n'


def test_list_session_files_unions_when_disjoint(
    fake_home: Path, tmp_path: Path
):
    """Different filenames in each dir → both surface in the result."""
    workspace = tmp_path / "wkspace"
    workspace.mkdir()
    enc = claude_sessions._encode_variant_a(str(workspace.resolve()))
    target = claude_sessions._PROJECTS_DIR / enc
    target.mkdir()
    (target / "fresh.jsonl").write_text("{}\n")

    legacy = workspace / "user" / "sessions"
    legacy.mkdir(parents=True)
    (legacy / "old.jsonl").write_text("{}\n")

    files = claude_sessions.list_session_files(workspace)
    assert sorted(f.name for f in files) == ["fresh.jsonl", "old.jsonl"]


def test_list_session_files_empty_returns_empty_list(
    fake_home: Path, tmp_path: Path
):
    """No sources exist at all → empty list, no mkdir side effect."""
    workspace = tmp_path / "wkspace"
    workspace.mkdir()

    files = claude_sessions.list_session_files(workspace)
    assert files == []
    assert not (workspace / "user").exists()
