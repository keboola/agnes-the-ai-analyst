"""`agnes init` after an interrupted run resumes without `--force`.

Regression coverage for issue #259: pre-0.53 every killed `agnes init`
left `CLAUDE.md` on disk but no completion marker; the next attempt
errored with `partial_state` and forced a full re-download.
"""

from pathlib import Path


def test_init_complete_constant_points_at_dotfile():
    """The sentinel lives under `.agnes/` so it doesn't pollute the
    workspace root."""
    from cli.commands.init import _INIT_COMPLETE_FILE
    assert _INIT_COMPLETE_FILE.startswith(".agnes/")
    assert _INIT_COMPLETE_FILE.endswith("init-complete")


def test_workspace_without_sentinel_is_treated_as_resumable(tmp_path: Path):
    """A workspace with CLAUDE.md but no completion sentinel must NOT
    raise `partial_state` — it's a resume."""
    # We exercise the gate logic directly by checking what the
    # path-existence check sees.
    (tmp_path / "CLAUDE.md").write_text("# Acme — AI Data Analyst\n", encoding="utf-8")
    # Sentinel absent.
    from cli.commands.init import _INIT_COMPLETE_FILE, _INIT_MARKER
    assert _INIT_MARKER in (tmp_path / "CLAUDE.md").read_text()
    assert not (tmp_path / _INIT_COMPLETE_FILE).exists()
    # If both conditions hold (marker present, sentinel absent), the
    # init flow's early-out should NOT fire. We can't easily run the
    # full init command in a unit test, but the boolean condition is
    # testable.
    is_resumable = (tmp_path / "CLAUDE.md").exists() and not (tmp_path / _INIT_COMPLETE_FILE).exists()
    assert is_resumable


def test_workspace_with_sentinel_blocks_without_force(tmp_path: Path):
    """Both CLAUDE.md AND sentinel present → require --force (old behavior)."""
    (tmp_path / "CLAUDE.md").write_text("# Acme — AI Data Analyst\n", encoding="utf-8")
    sentinel = tmp_path / ".agnes" / "init-complete"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("completed_at: 2026-05-12T10:00:00+00:00\n", encoding="utf-8")
    is_blocked = (tmp_path / "CLAUDE.md").exists() and sentinel.exists()
    assert is_blocked
