"""Server-normalized skills + commands catalog for the web chat slash menu.

``GET /api/chat/skills`` (``app/api/chat.py``) needs to tell the composer
what is actually invokable in a given user's chat sandbox. Two independent
sources feed it:

  - **bundled** — skills shipped in the chat sandbox's bundled workspace
    template. ``app/chat/workdir.py``'s ``WorkdirManager`` copies this same
    ``<bundled_template_dir>/.claude/skills`` tree into every session
    (``prepare_ephemeral_session_dir``) / the whole template
    (``initialize_default_workspace``, used by ``ensure_user_workdir``) —
    this module reads the identical directory so the catalog matches what
    actually lands on disk.
  - **marketplace** — the caller's RBAC-filtered marketplace/store plugin
    set, resolved via ``src.marketplace_filter.resolve_user_marketplace``.
    That resolver is also what backs the served marketplace ZIP/git
    endpoints ``agnes refresh-marketplace`` pulls from — the same command
    ``app/chat/runner.py``'s ``_bootstrap_marketplace`` runs inside the
    sandbox — so this list matches what is actually installed there.

Shadowing: when a skill name appears in both sources, the **marketplace**
entry wins — it is the more user-specific grant. Merging happens in
``merged_skills``, which also isolates each source so a failure in one
(unreadable directory, resolver exception) never blocks the other; the
failing source is logged as a warning and treated as empty.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import duckdb

from src.marketplace_filter import resolve_user_marketplace
from src.store_guardrails._frontmatter import parse_frontmatter

logger = logging.getLogger(__name__)

# Same literal ``app/chat/workdir.py``'s ``WorkdirManager`` is constructed
# with in ``app/main.py`` / ``app/api/users.py`` (``bundled_template_dir``).
# Resolved relative to the repo root (not CWD) so this module works the same
# regardless of where the process was launched from.
BUNDLED_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "app" / "initial_workspace_default"


def _read_skill_md(skill_md: Path) -> tuple[str, Optional[str]]:
    """Return ``(name, description)`` for one ``SKILL.md``, frontmatter-first.

    Frontmatter is the source of truth. ``name`` falls back to the SKILL.md's
    parent directory name (the "skill directory") when frontmatter has no
    ``name``; ``description`` falls back to ``None`` (no second line in the
    UI) rather than an empty string.
    """
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)
    name = (fm.get("name") or "").strip() or skill_md.parent.name
    description = (fm.get("description") or "").strip() or None
    return name, description


def list_bundled_skills(bundled_template_dir: Path) -> list[dict]:
    """Skills shipped in the chat sandbox's bundled workspace template.

    ``bundled_template_dir`` is the template root (e.g.
    ``app/initial_workspace_default``); this looks under its
    ``.claude/skills/<name>/SKILL.md`` — the same layout
    ``WorkdirManager.prepare_ephemeral_session_dir`` copies from. A missing
    ``.claude/skills`` directory is normal (nothing bundled yet), not an
    error — returns an empty list.
    """
    out: list[dict] = []
    skills_dir = bundled_template_dir / ".claude" / "skills"
    if not skills_dir.is_dir():
        return out
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            name, description = _read_skill_md(skill_md)
        except OSError:
            logger.warning("chat skills: unreadable bundled SKILL.md %s", skill_md, exc_info=True)
            continue
        out.append({"name": name, "description": description, "source": "bundled"})
    return out


def _plugin_dirs(plugin: dict) -> list[Path]:
    """Every on-disk root to search for ``SKILL.md`` for one resolved plugin.

    Most entries have a single ``plugin_dir``; the synthetic Store "flea"
    bundle entry (``resolve_user_marketplace``'s ``source="store-bundle"``)
    has no single root — instead ``bundle_dirs`` lists one directory per
    bundled skill/agent upload.
    """
    if plugin.get("bundle_dirs"):
        return list(plugin["bundle_dirs"])
    plugin_dir = plugin.get("plugin_dir")
    return [plugin_dir] if plugin_dir is not None else []


def list_marketplace_skills(conn: duckdb.DuckDBPyConnection, user: dict) -> list[dict]:
    """The caller's RBAC-filtered marketplace/store plugin skills.

    Uses ``resolve_user_marketplace`` (admin-granted-and-subscribed
    marketplace plugins, unioned with the caller's Store installs) — the
    same composition ``agnes refresh-marketplace`` fetches server-side and
    ``app/chat/runner.py``'s ``_bootstrap_marketplace`` installs into the
    live sandbox.

    A plugin's ``SKILL.md`` may live directly at its root (single-skill
    plugins, e.g. the built-in marketplace) or under ``skills/<name>/
    SKILL.md`` (multi-skill plugins, the curated-marketplace and Store
    convention) — this looks anywhere under the plugin root, mirroring
    ``src/store_guardrails/manifest_check.py``'s same defensive scan.
    """
    out: list[dict] = []
    for plugin in resolve_user_marketplace(conn, user):
        for plugin_dir in _plugin_dirs(plugin):
            if plugin_dir is None or not plugin_dir.is_dir():
                continue
            for skill_md in sorted(plugin_dir.rglob("SKILL.md")):
                try:
                    name, description = _read_skill_md(skill_md)
                except OSError:
                    logger.warning("chat skills: unreadable marketplace SKILL.md %s", skill_md, exc_info=True)
                    continue
                out.append({"name": name, "description": description, "source": "marketplace"})
    return out


def merged_skills(bundled_template_dir: Path, conn: duckdb.DuckDBPyConnection, user: dict) -> list[dict]:
    """Merge bundled + marketplace skills into one deterministic list.

    Marketplace entries win name clashes against bundled ones (more
    user-specific). Either source failing to list is logged as a warning
    and treated as empty rather than failing the whole request — the other
    source's skills still reach the caller. The result is sorted by name so
    the composer's filterable menu has a stable order.
    """
    try:
        bundled = list_bundled_skills(bundled_template_dir)
    except Exception:
        logger.warning("chat skills: bundled source failed to list", exc_info=True)
        bundled = []

    try:
        marketplace = list_marketplace_skills(conn, user)
    except Exception:
        logger.warning("chat skills: marketplace source failed to list", exc_info=True)
        marketplace = []

    by_name: dict[str, dict] = {s["name"]: s for s in bundled}
    for s in marketplace:
        by_name[s["name"]] = s  # marketplace wins name clashes

    return sorted(by_name.values(), key=lambda s: s["name"])


def list_recognized_commands() -> list[dict]:
    """Slash commands the chat backend/agent actually recognizes.

    As things stand, this is an empty list — checked, not assumed:

    - ``app/chat/runner.py`` performs no slash-command parsing of its own.
      Every ``user_msg`` frame's ``text`` is forwarded verbatim to
      ``ClaudeSDKClient.connect()``/``query()`` as a plain user turn; Agnes
      never special-cases a leading ``/``.
    - The bundled chat workspace template (``app/initial_workspace_default``,
      the same tree ``list_bundled_skills`` reads) ships no
      ``.claude/commands/*.md`` — so there are no custom project commands
      either (contrast with the LOCAL laptop workspace, which does get
      ``.claude/commands/*.md`` from ``cli/templates/commands/`` via
      ``agnes init`` — a different, non-chat code path).
    - The underlying claude-agent-sdk's ``ClaudeSDKClient.get_server_info()``
      can surface the sandboxed CLI's own built-in commands from its init
      handshake, but Agnes doesn't read or forward that handshake data
      anywhere today, so there is no verified list to publish without
      guessing at Claude Code version-specific command names.

    Extend this once a command is actually wired end-to-end (a bundled
    ``.claude/commands/*.md`` file, or the runner intercepting a specific
    command before forwarding to the SDK) — don't invent entries ahead of
    the implementation.
    """
    return []
