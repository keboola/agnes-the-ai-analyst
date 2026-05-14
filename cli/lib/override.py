"""Single source of truth for "is this an override workspace?".

When the operator has configured an Initial Workspace Template on
``/admin/server-config``, ``agnes init`` extracts the admin's repo zip
into the analyst's workspace and writes an extended sentinel:

    # .claude/init-complete
    completed_at: 2026-05-13T14:32:00Z
    agnes_version: 0.53.0
    server_url: https://agnes.example.com
    override: true
    template_source: https://github.com/example/agnes-workspace-template
    template_sha: 1a2b3c4d

Init-time writers in ``cli/commands/init.py`` call
:func:`is_override_workspace` to decide whether to skip default-workspace
seeding (hooks, slash commands, ``settings.json`` defaults,
``CLAUDE.local.md`` stub) when the analyst's workspace was already
materialised from an admin template. The check sits at the single
init-time call site (the ``if not override_active:`` block in init.py)
rather than scattered across each writer.

Runtime writers — ``agnes refresh-marketplace``, ``agnes self-upgrade``'s
``maybe_refresh_claude_hooks``, and any future runtime CLI command —
do NOT consult the sentinel. The Initial Workspace Template feature
governs *initial* workspace contents only; subsequent CLI commands must
keep the workspace in sync with their runtime data (plugin stack, new
Agnes hook layouts, etc.) regardless of how the workspace was seeded.

NB: this module is intentionally tiny. The CLI is widely imported and
the override check fires on init paths, so we keep imports cheap
(stdlib only — no YAML library).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


# OVERRIDE MODE — init-time only.
#
# The sentinel below carries `override: true` for workspaces materialised
# from an admin-configured Initial Workspace Template. The init-time path
# in `cli/commands/init.py` reads the sentinel and skips its default-
# workspace seeding block when the flag is set — admin's template is
# authoritative for INITIAL `.claude/` contents.
#
# Runtime CLI commands (e.g. `agnes refresh-marketplace`,
# `agnes self-upgrade`'s hook migration) do NOT consult the sentinel.
# They keep the workspace in sync with the user's current stack and the
# current Agnes hook layout regardless of how the workspace was seeded.
# Admin custom hooks survive runtime refresh because
# `cli/lib/hooks.py:_OUR_COMMAND_MARKERS` matches only Agnes commands.

_SENTINEL_PATH = Path(".claude") / "init-complete"


def _read_sentinel(workspace: Path) -> Optional[dict]:
    """Parse the sentinel as a flat ``key: value`` map. Returns None when
    the file is absent / unreadable / malformed.

    The sentinel format is intentionally minimal (one key-value per
    line, `key: value`) so this parser stays stdlib-only. If we ever
    need nested structure we'll switch to YAML — for now the contract
    fits on one screen.
    """
    sentinel = workspace / _SENTINEL_PATH
    if not sentinel.exists():
        return None
    try:
        text = sentinel.read_text(encoding="utf-8")
    except OSError:
        return None
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def is_override_workspace(workspace: Path) -> bool:
    """True iff ``workspace`` was inited from an admin-configured Initial
    Workspace Template (the sentinel carries ``override: true``).

    False on missing / unreadable sentinel, on sentinel without an
    override key, and on sentinel with ``override`` set to anything
    other than literal ``true`` (case-insensitive).

    Callers should use this only to gate **init-time** behavior — see
    the module docstring for the init-time/runtime split.
    """
    data = _read_sentinel(workspace)
    if not data:
        return False
    return data.get("override", "").strip().lower() == "true"


def read_override_metadata(workspace: Path) -> Optional[dict]:
    """Full sentinel contents (or None when no sentinel).

    Useful for surfacing ``template_source`` / ``template_sha`` /
    ``applied_at`` in diagnostics (``agnes status``, ``agnes diagnose``)
    so the operator can see which template version the workspace ran
    last. Returns the raw key-value map without type coercion — caller
    is responsible for interpreting ``override`` etc. (use
    :func:`is_override_workspace` for the boolean question).
    """
    return _read_sentinel(workspace)
