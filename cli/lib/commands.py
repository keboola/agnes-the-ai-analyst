"""Workspace-scoped Claude Code slash-command installer.

Sibling to `cli/lib/hooks.py`. Where hooks live in
`<workspace>/.claude/settings.json`, slash commands live as one
markdown file per command in `<workspace>/.claude/commands/`. This
module installs the Agnes-managed slash commands into a workspace.

Design notes:
- Workspace-scoped (`<workspace>/.claude/commands/<name>.md`), NOT
  user-home. The slash commands appear only when Claude Code opens
  this workspace, matching the hook scoping in `hooks.py`.
- Idempotent: always overwrites *our* files (server-managed canonical
  content, naturally evolves with the CLI version) but never touches
  third-party slash commands the user (or another tool) may have
  authored under `.claude/commands/`. Listing files individually
  rather than wiping the directory keeps custom commands safe.
- Templates ship inside the wheel under `cli/templates/commands/`.
  `pyproject.toml` declares `cli` as a hatch wheel package, so
  hatchling includes the markdown bodies during the build the same
  way it ships `config/agnes_workspace_template.txt`.
"""

from __future__ import annotations

import sys
from pathlib import Path


# Slash commands managed by `agnes init`. Source (template name on
# disk under `cli/templates/commands/`) → destination filename in
# `<workspace>/.claude/commands/`. Today both names match; the indirection
# keeps the door open for renaming (e.g. internal template name vs the
# `/<command>` slug exposed to Claude Code).
_MANAGED_COMMANDS: tuple[tuple[str, str], ...] = (
    ("update-agnes-plugins.md", "update-agnes-plugins.md"),
    ("agnes-private.md", "agnes-private.md"),
)


# Defensive fallbacks used when the bundled template is missing on disk
# (broken install, stripped-down test environment). Keyed by source
# template filename so a missing `agnes-private.md` doesn't get
# clobbered with `update-agnes-plugins` content.
_FALLBACK_BODIES: dict[str, str] = {
    "update-agnes-plugins.md": (
        "---\n"
        "description: Update Agnes marketplace plugins to latest versions\n"
        "---\n"
        "\n"
        "Run `agnes refresh-marketplace` and report the output.\n"
    ),
    "agnes-private.md": (
        "---\n"
        "description: Mark the current Claude Code session as private\n"
        "disable-model-invocation: true\n"
        "---\n"
        "\n"
        "!`agnes mark-private`\n"
    ),
}


def _templates_dir() -> Path:
    """Locate the bundled-template directory.

    `cli/lib/commands.py` → `cli/templates/commands/`.
    Two `.parent` hops: lib/ → cli/, then descend into templates/commands/.
    """
    return Path(__file__).parent.parent / "templates" / "commands"


def install_claude_commands(workspace: Path) -> None:
    """Install Agnes-managed slash commands into `<workspace>/.claude/commands/`.

    Always writes (overwrites) the managed command files; never touches
    other files the user may have under `.claude/commands/`. Idempotent.

    Override-sentinel handling lives at the call site, not here. The
    init-time caller (`cli/commands/init.py`, gated by `override_active`)
    decides whether to skip this writer for admin-templated workspaces.
    Future runtime callers can invoke us unconditionally.
    """
    commands_dir = workspace / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    templates_dir = _templates_dir()
    for source_name, dest_name in _MANAGED_COMMANDS:
        source_path = templates_dir / source_name
        try:
            body = source_path.read_text(encoding="utf-8")
        except OSError:
            print(
                f"Warning: bundled slash-command template "
                f"{source_path} missing; writing defensive fallback.",
                file=sys.stderr,
            )
            body = _FALLBACK_BODIES.get(source_name, "")
        (commands_dir / dest_name).write_text(body, encoding="utf-8")
