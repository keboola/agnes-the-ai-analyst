"""Workspace-scoped Claude Code statusline installer.

Companion to ``cli/lib/hooks.py``. Where hooks surface short-lived
``systemMessage`` toasts at SessionStart (often missed by users), the
statusline puts a persistent line at the bottom of the Claude Code UI.
``agnes refresh-marketplace`` writes ``~/.agnes/refresh.status`` when it
actually installs/updates something, and the bash script reads that file
on every assistant message to surface "agnes ⟳ <summary>" until the
entry ages out (default 30 min).

Layout — same as ``install_claude_hooks``: workspace-scoped (writes to
``<workspace>/.claude/settings.json`` + ``<workspace>/.claude/agnes-statusline.sh``),
NOT user-home. So a workspace that never ran ``agnes init`` doesn't get
the statusline injected, and uninstalling agnes from one workspace
leaves another's settings untouched.

Idempotency:
  - The script file is written when absent. A hand-edited script
    survives re-init (we don't trash operator customizations).
  - The settings.json ``statusLine`` block is set when absent OR when
    its existing ``command`` already points at our script. A foreign
    statusLine (operator's own) is left in place with a stderr
    warning — never overwrite the operator's choice silently.
"""

from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path
from typing import Optional


# Resource location of the bash template inside the installed wheel.
# ``importlib.resources`` reads the file regardless of whether agnes is
# running from a wheel install or an editable source checkout.
_STATUSLINE_PACKAGE = "cli.data"
_STATUSLINE_RESOURCE = "agnes-statusline.sh"

# Filename where we materialize the script inside <workspace>/.claude/.
# The settings.json `statusLine.command` points here. Workspace-scoped
# so two workspaces with different agnes versions don't share a single
# script file (matters when the bash script's contract evolves).
_STATUSLINE_SCRIPT_NAME = "agnes-statusline.sh"

# Substring used to recognize "our" statusLine command on re-install or
# reset. The script filename is enough — operators don't typically name
# their own statusline scripts the same as ours.
_OUR_STATUSLINE_MARKER = _STATUSLINE_SCRIPT_NAME


def _read_statusline_template() -> str:
    """Return the bash script body packaged at ``cli/data/agnes-statusline.sh``.

    Uses ``importlib.resources`` so the lookup works in both
    wheel-installed (``site-packages/cli/data/...``) and editable
    (``./cli/data/...``) layouts.
    """
    return (resources.files(_STATUSLINE_PACKAGE) / _STATUSLINE_RESOURCE).read_text(
        encoding="utf-8"
    )


def install_claude_statusline(workspace: Path) -> None:
    """Install the agnes statusline into ``<workspace>/.claude/``.

    Two artifacts:
      1. ``<workspace>/.claude/agnes-statusline.sh`` — bash script
         (mode 755 best-effort; chmod is a no-op on Windows NTFS via
         Git Bash, harmless).
      2. ``<workspace>/.claude/settings.json:statusLine`` — Claude Code
         config pointing ``type: command`` at the script.

    Behavior:
      - Script file: written when absent. A hand-edited script is
        preserved (we don't overwrite). Re-init is therefore a no-op
        on the script.
      - settings.json statusLine: set when absent OR when an existing
        statusLine already points at our script (re-install case —
        we re-affirm the path). A foreign statusLine (operator's
        own custom one) is left intact and a stderr warning explains
        that agnes notifications won't surface until the operator
        either removes their statusLine or pipes our script's output
        into their own.
    """
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    script_path = claude_dir / _STATUSLINE_SCRIPT_NAME
    settings_path = claude_dir / "settings.json"

    # 1. Materialize the script if absent. Use the wheel-bundled template
    #    rather than re-deriving from source, so the runtime version
    #    matches the agnes binary that's installed.
    if not script_path.exists():
        script_path.write_text(_read_statusline_template(), encoding="utf-8")
        # Best-effort chmod 755. On Windows NTFS via Git Bash this is
        # a no-op; on POSIX it's needed so Claude Code can execute the
        # file directly. `try/except OSError` instead of branching on
        # platform — simpler, same effect.
        try:
            script_path.chmod(0o755)
        except OSError:
            pass

    # 2. Read existing settings, decide what to do with statusLine.
    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(
                f"Warning: {settings_path} is not valid JSON; skipping statusline install.",
                file=sys.stderr,
            )
            return
    else:
        cfg = {}

    existing = cfg.get("statusLine")
    if isinstance(existing, dict):
        existing_command = existing.get("command", "")
        if isinstance(existing_command, str) and _OUR_STATUSLINE_MARKER in existing_command:
            # Already ours — re-affirm to absorb any path changes (e.g.
            # workspace dir got moved / re-symlinked).
            cfg["statusLine"] = _build_statusline_config(script_path)
        else:
            # Foreign statusLine, don't touch. Warn so the operator
            # knows agnes notifications won't surface.
            print(
                f"Warning: {settings_path} already has a custom statusLine "
                f"({existing_command!r}); leaving it intact. agnes refresh "
                f"notifications will write to ~/.agnes/refresh.status — "
                f"add `cat ~/.agnes/refresh.status` to your statusline if "
                f"you want them surfaced in the UI.",
                file=sys.stderr,
            )
            return
    else:
        # Not set (or malformed type) → install ours.
        cfg["statusLine"] = _build_statusline_config(script_path)

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def _build_statusline_config(script_path: Path) -> dict:
    """Shape Claude Code expects for ``settings.json:statusLine``.

    No ``refreshInterval`` — see ``cli/data/agnes-statusline.sh`` header
    for why. Statusline runs naturally on each assistant message, which
    is the only time the user is reading the UI; idle polling would
    burn CPU for output nobody sees.
    """
    return {
        "type": "command",
        "command": str(script_path),
    }


def uninstall_claude_statusline(workspace: Path) -> None:
    """Reverse ``install_claude_statusline`` for THIS workspace.

    Removes the script file unconditionally and strips ``statusLine`` from
    settings.json ONLY if it currently points at our script — so a foreign
    statusLine that the operator added later survives.

    Used by tests; ``scripts/dev/agnes-client-reset.sh`` does the same
    cleanup at shell level for one user-home script + one user-home
    settings.json, but workspace-scoped resets stay a manual step (the
    reset script can't enumerate every workspace dir the user ever
    init'd in).
    """
    claude_dir = workspace / ".claude"
    script_path = claude_dir / _STATUSLINE_SCRIPT_NAME
    settings_path = claude_dir / "settings.json"

    if script_path.exists():
        try:
            script_path.unlink()
        except OSError:
            pass

    if not settings_path.exists():
        return
    try:
        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    existing = cfg.get("statusLine")
    if not isinstance(existing, dict):
        return
    cmd = existing.get("command", "")
    if isinstance(cmd, str) and _OUR_STATUSLINE_MARKER in cmd:
        cfg.pop("statusLine", None)
        settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def statusline_template_for_tests() -> Optional[str]:
    """Test-only helper: return the bash template body so tests can
    pin its content invariants (auto-hide TTL, status-file path) without
    re-implementing the resource lookup."""
    try:
        return _read_statusline_template()
    except (FileNotFoundError, ModuleNotFoundError):
        return None
