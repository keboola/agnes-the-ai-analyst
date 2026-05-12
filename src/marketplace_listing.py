"""Shared helpers to walk a plugin clone and enumerate its skills, agents,
and slash commands.

These functions were originally private to ``app.api.marketplace`` (as
``_list_inner_skills``, ``_list_inner_agents``, ``_list_commands``). They are
extracted here so ``src.marketplace`` (the sync path) can call them when
building usage-attribution rows without importing from the FastAPI app layer.

The ``app.api.marketplace`` module re-imports and re-exports them so the
existing call sites in the API layer keep working unchanged.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def list_inner_skills(plugin_root: Path) -> List[str]:
    """Return a list of skill names from ``<plugin_root>/skills/*/SKILL.md``
    frontmatter.  Missing / unreadable directories return an empty list.
    """
    out: List[str] = []
    skills_dir = plugin_root / "skills"
    if not skills_dir.is_dir():
        return out
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        name = (fm.get("name") or skill_dir.name or "").strip()
        if name:
            out.append(name)
    return out


def list_inner_agents(plugin_root: Path) -> List[str]:
    """Return a list of agent names from ``<plugin_root>/agents/*.md``
    frontmatter.  Missing / unreadable directories return an empty list.
    """
    out: List[str] = []
    agents_dir = plugin_root / "agents"
    if not agents_dir.is_dir():
        return out
    for agent_path in sorted(agents_dir.iterdir()):
        if not agent_path.is_file() or agent_path.suffix != ".md":
            continue
        try:
            text = agent_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        name = (fm.get("name") or agent_path.stem or "").strip()
        if name:
            out.append(name)
    return out


def list_commands(plugin_root: Path) -> List[str]:
    """Return a list of command names (with leading ``/``) from
    ``<plugin_root>/commands/*.md`` frontmatter.

    Missing / unreadable directories return an empty list.
    """
    d = plugin_root / "commands"
    if not d.is_dir():
        return []
    out: List[str] = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix != ".md":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        raw = (fm.get("name") or p.stem or "").strip()
        if not raw:
            continue
        name = raw if raw.startswith("/") else f"/{raw}"
        out.append(name)
    return out
