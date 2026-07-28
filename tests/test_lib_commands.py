"""Tests for cli/lib/commands.py:install_claude_commands."""

from __future__ import annotations

from pathlib import Path

from cli.lib.commands import install_claude_commands


def _read_managed_command(workspace: Path) -> str:
    return (workspace / ".claude" / "commands" / "update-agnes-plugins.md").read_text(
        encoding="utf-8"
    )


def test_install_writes_slash_command_file(tmp_path):
    """install_claude_commands writes update-agnes-plugins.md into
    <workspace>/.claude/commands/. The file is the bundled template
    (frontmatter + body) — the test just pins that *something* lands and
    that frontmatter shape is intact, so a future template edit doesn't
    silently lose its description metadata."""
    install_claude_commands(tmp_path)
    body = _read_managed_command(tmp_path)
    assert body.startswith("---"), body[:120]
    assert "description:" in body.split("---", 2)[1], body[:200]
    # The slash command's whole point: invoke `agnes refresh-marketplace`.
    # Pin that the bundled body actually references the command.
    assert "agnes refresh-marketplace" in body


def test_install_creates_commands_dir_when_missing(tmp_path):
    """Workspace without a .claude/ tree at all → install creates both
    .claude/ and .claude/commands/."""
    assert not (tmp_path / ".claude").exists()
    install_claude_commands(tmp_path)
    assert (tmp_path / ".claude" / "commands").is_dir()


def test_install_overwrites_existing_managed_file(tmp_path):
    """Even if the workspace has a hand-edited copy of the managed slash
    command, install overwrites with the canonical template — server-
    managed by design (Q2 of the design discussion). Users who want to
    customize copy to a different filename, which is preserved by the
    third-party-untouched test below."""
    cmd_dir = tmp_path / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "update-agnes-plugins.md").write_text(
        "USER EDIT THAT MUST BE OVERWRITTEN", encoding="utf-8",
    )
    install_claude_commands(tmp_path)
    body = _read_managed_command(tmp_path)
    assert "USER EDIT" not in body
    assert "agnes refresh-marketplace" in body


def test_install_does_not_touch_other_command_files(tmp_path):
    """User's own slash commands under .claude/commands/ (e.g.
    my-custom.md, project-specific helpers) must survive install
    untouched. Only the Agnes-managed command files are overwritten."""
    cmd_dir = tmp_path / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    custom_path = cmd_dir / "my-custom.md"
    custom_body = "---\ndescription: my own slash command\n---\n\nhello"
    custom_path.write_text(custom_body, encoding="utf-8")

    install_claude_commands(tmp_path)
    assert custom_path.read_text(encoding="utf-8") == custom_body


def test_install_idempotent(tmp_path):
    """Two consecutive installs produce identical state. Important
    because `agnes init --force` re-runs the installer and the
    SessionStart hook chain (in some future world where we wire it up)
    might too — neither should accumulate stray files or change content
    on a no-op invocation."""
    install_claude_commands(tmp_path)
    first = _read_managed_command(tmp_path)
    install_claude_commands(tmp_path)
    second = _read_managed_command(tmp_path)
    assert first == second
    # Exactly the managed files, no strays.
    cmd_dir_files = sorted(p.name for p in (tmp_path / ".claude" / "commands").iterdir())
    assert cmd_dir_files == ["agnes-private.md", "update-agnes-plugins.md"]


def test_install_writes_agnes_private_slash_command(tmp_path):
    """The /agnes-private slash command is shipped alongside update-agnes-plugins
    and triggers `agnes mark-private` deterministically (no AI in the loop).
    Pin the marker so a template rewrite that drops the `!`-prefix line is caught."""
    install_claude_commands(tmp_path)
    body = (tmp_path / ".claude" / "commands" / "agnes-private.md").read_text(
        encoding="utf-8"
    )
    assert body.startswith("---"), body[:120]
    frontmatter = body.split("---", 2)[1]
    assert "description:" in frontmatter
    # User-only: the model must never invoke /agnes-private on its own —
    # marking a session private is the analyst's deliberate action. The
    # frontmatter flag hides the command from the model entirely while
    # keeping it typeable by the user.
    assert "disable-model-invocation: true" in frontmatter
    # `!`-prefix runs as bash directly — deterministic, no AI tokens.
    assert "agnes mark-private" in body
