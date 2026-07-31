"""User-scope (all-repositories) writers — spec §6.4.

Two direct-edit surfaces, both governed by the same recovery philosophy:
on anything unexpected, warn to stderr and leave the user's file untouched
(never back up + rebuild a user-owned file — mirrors `cli/lib/automode.py`).

1. `~/.claude/CLAUDE.md` rails block, fenced by exact markers.
2. The `hooks` key in `~/.claude/settings.json` — no `claude` CLI exists
   for hook management, so the entry is merged directly using the
   workspace installer's `_OUR_COMMAND_MARKERS` contract.

Claude-Code-owned JSON (`enabledPlugins`, `mcpServers`) is NEVER written
here — the `claude` CLI is the only writer for those (spec §6.4).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from cli.lib.session_paths import user_settings_path


RAILS_BEGIN = "<!-- BEGIN agnes-global (managed by 'agnes global enable'; edits inside are overwritten) -->"
RAILS_END = "<!-- END agnes-global -->"

# Identical literal to the workspace SessionStart entry in
# `cli/lib/hooks.py` — the user-level hook is the same detached update.
GLOBAL_UPDATE_HOOK_CMD = 'bash -c "( nohup agnes update --quiet </dev/null >/dev/null 2>&1 & ) ; true"'


def user_claude_md_path() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def _split_on_block(text: str) -> tuple[str, str, str] | None:
    """(before, inside, after) for exactly one well-formed marker pair;
    None when markers are absent; raises ValueError when malformed
    (duplicated or unmatched markers)."""
    begins = text.count(RAILS_BEGIN)
    ends = text.count(RAILS_END)
    if begins == 0 and ends == 0:
        return None
    if begins != 1 or ends != 1:
        raise ValueError("duplicated markers")
    start = text.index(RAILS_BEGIN)
    end = text.index(RAILS_END)
    if end < start:
        raise ValueError("END before BEGIN")
    return (
        text[:start],
        text[start + len(RAILS_BEGIN) : end],
        text[end + len(RAILS_END) :],
    )


def _render_block(content: str) -> str:
    return f"{RAILS_BEGIN}\n{content.rstrip()}\n{RAILS_END}"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agnes-user-scope.", dir=str(path.parent))
    try:
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def upsert_rails_block(claude_md: Path, content: str) -> str:
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else None
    block = _render_block(content)
    if existing is None:
        _atomic_write(claude_md, block + "\n")
        return "created"
    try:
        parts = _split_on_block(existing)
    except ValueError:
        print(
            f"warn: {claude_md} has duplicated/unmatched agnes-global markers; "
            "left untouched. Repair by hand, then re-run `agnes global enable`.",
            file=sys.stderr,
        )
        return "skipped_malformed"
    if parts is None:
        sep = "" if existing.endswith("\n") else "\n"
        _atomic_write(claude_md, existing + sep + block + "\n")
        return "created"
    before, inside, after = parts
    if inside.strip("\n") == content.rstrip():
        return "unchanged"
    _atomic_write(claude_md, before + block + after)
    return "updated"


def remove_rails_block(claude_md: Path) -> str:
    if not claude_md.exists():
        return "absent"
    existing = claude_md.read_text(encoding="utf-8")
    try:
        parts = _split_on_block(existing)
    except ValueError:
        print(
            f"warn: {claude_md} has duplicated/unmatched agnes-global markers; left untouched.",
            file=sys.stderr,
        )
        return "skipped_malformed"
    if parts is None:
        return "absent"
    before, _inside, after = parts
    merged = before.rstrip("\n") + ("\n" if before.strip() else "") + after.lstrip("\n")
    if not merged.strip():
        claude_md.unlink()
    else:
        _atomic_write(claude_md, merged)
    return "removed"


def _load_user_settings() -> dict | None:
    """Parsed user settings dict; {} when the file is absent; None when the
    file exists but is unreadable/not-an-object (leave-untouched signal)."""
    path = user_settings_path()
    if not path.exists():
        return {}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cfg if isinstance(cfg, dict) else None


def _write_user_settings(cfg: dict) -> None:
    _atomic_write(user_settings_path(), json.dumps(cfg, indent=2) + "\n")


def _is_ours(entry: dict) -> bool:
    from cli.lib.hooks import _OUR_COMMAND_MARKERS

    cmds = [h.get("command", "") for h in entry.get("hooks", []) if isinstance(h, dict)]
    return bool(cmds) and all(any(m in c for m in _OUR_COMMAND_MARKERS) for c in cmds)


def install_user_session_hook() -> str:
    cfg = _load_user_settings()
    if cfg is None:
        print(f"warn: {user_settings_path()} is not valid JSON; left untouched.", file=sys.stderr)
        return "skipped_malformed"
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print(f"warn: {user_settings_path()} `hooks` is not an object; left untouched.", file=sys.stderr)
        return "skipped_malformed"
    entries = hooks.setdefault("SessionStart", [])
    if not isinstance(entries, list):
        print(
            f"warn: {user_settings_path()} `hooks.SessionStart` is not a list; left untouched.",
            file=sys.stderr,
        )
        return "skipped_malformed"
    ours = [e for e in entries if isinstance(e, dict) and _is_ours(e)]
    canonical = {"hooks": [{"type": "command", "command": GLOBAL_UPDATE_HOOK_CMD}]}
    if ours == [canonical]:
        return "unchanged"
    for e in ours:
        entries.remove(e)
    entries.append(canonical)
    _write_user_settings(cfg)
    return "installed"


def remove_user_session_hook() -> str:
    cfg = _load_user_settings()
    if cfg is None:
        print(f"warn: {user_settings_path()} is not valid JSON; left untouched.", file=sys.stderr)
        return "skipped_malformed"
    entries = cfg.get("hooks", {}).get("SessionStart") if isinstance(cfg.get("hooks"), dict) else None
    if not isinstance(entries, list):
        return "absent"
    ours = [e for e in entries if isinstance(e, dict) and _is_ours(e)]
    if not ours:
        return "absent"
    for e in ours:
        entries.remove(e)
    _write_user_settings(cfg)
    return "removed"


def user_session_hook_state() -> str:
    cfg = _load_user_settings()
    if cfg is None:
        return "malformed"
    entries = cfg.get("hooks", {}).get("SessionStart") if isinstance(cfg.get("hooks"), dict) else None
    if not isinstance(entries, list):
        return "missing"
    return "ok" if any(isinstance(e, dict) and _is_ours(e) for e in entries) else "missing"


def load_global_rails() -> str:
    """The compact rails content shipped with this CLI version (spec §6.1
    step 4). Markers are added by the splice, not stored in the template."""
    template = Path(__file__).parent.parent / "templates" / "global_rails.md"
    return template.read_text(encoding="utf-8")


def rails_block_state(claude_md: Path, content: str) -> str:
    if not claude_md.exists():
        return "missing"
    try:
        parts = _split_on_block(claude_md.read_text(encoding="utf-8"))
    except ValueError:
        return "malformed"
    if parts is None:
        return "missing"
    return "ok" if parts[1].strip("\n") == content.rstrip() else "drifted"
