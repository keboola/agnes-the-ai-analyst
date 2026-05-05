"""Domain logic for the flea-market community skill marketplace."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
PENDING_MARKER = ".pending"


@dataclass
class FleaMarketConfig:
    marketplace_slug: str
    plugin_name: str
    github_repo: str
    # PAT auth (simpler — for dev/testing)
    github_pat: str = field(default="")
    # GitHub App auth (for production — all three required when github_pat is empty)
    github_app_id: str = field(default="")
    github_app_private_key: str = field(default="")
    github_app_installation_id: str = field(default="")
    # GitHub Enterprise: set to "https://<host>/api/v3"; defaults to public GitHub API
    github_api_url: str = field(default="https://api.github.com")
    # _root is injected by tests; production code resolves from DATA_DIR
    _root: Optional[Path] = field(default=None, repr=False)

    def plugin_root(self) -> Path:
        if self._root is not None:
            return self._root
        import os
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        return data_dir / "marketplaces" / self.marketplace_slug

    def plugin_dir(self) -> Path:
        return self.plugin_root() / "plugins" / self.plugin_name

    def skills_dir(self) -> Path:
        return self.plugin_dir() / "skills"

    def plugin_json_path(self) -> Path:
        return self.plugin_dir() / ".claude-plugin" / "plugin.json"

    def marketplace_json_path(self) -> Path:
        return self.plugin_root() / ".claude-plugin" / "marketplace.json"


@dataclass
class SkillReview:
    is_duplicate: bool
    duplicate_of: Optional[str]
    duplicate_reason: Optional[str]
    requires_setup: bool
    setup_description: Optional[str]


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s


def _bump_patch(version: str) -> str:
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def skill_exists(config: FleaMarketConfig, skill_name: str) -> bool:
    return (config.skills_dir() / skill_name / "SKILL.md").exists()


def list_skills(config: FleaMarketConfig) -> List[Dict[str, str]]:
    skills_dir = config.skills_dir()
    if not skills_dir.exists():
        return []
    result = []
    for skill_dir in sorted(skills_dir.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        name = skill_dir.name
        description = ""
        for line in text.splitlines():
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
                break
        result.append({"name": name, "description": description})
    return result


def write_pending_marker(config: FleaMarketConfig, skill_name: str) -> None:
    (config.skills_dir() / skill_name / PENDING_MARKER).touch()


def clear_pending_marker(config: FleaMarketConfig, skill_name: str) -> None:
    (config.skills_dir() / skill_name / PENDING_MARKER).unlink(missing_ok=True)


def list_pending_skills(config: FleaMarketConfig) -> List[str]:
    """Return names of skills whose GitHub push has not yet completed."""
    skills_dir = config.skills_dir()
    if not skills_dir.exists():
        return []
    return [
        d.name for d in sorted(skills_dir.iterdir())
        if d.is_dir() and (d / PENDING_MARKER).exists() and (d / "SKILL.md").is_file()
    ]


_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "is_duplicate": {"type": "boolean"},
        "duplicate_of": {"type": ["string", "null"]},
        "duplicate_reason": {"type": ["string", "null"]},
        "requires_setup": {"type": "boolean"},
        "setup_description": {"type": ["string", "null"]},
    },
    "required": ["is_duplicate", "duplicate_of", "duplicate_reason", "requires_setup", "setup_description"],
}


def review_skill(
    extractor: Any,
    skill_name: str,
    description: str,
    body: str,
    existing_skills: List[Dict[str, str]],
) -> SkillReview:
    existing_list = "\n".join(
        f"- {s['name']}: {s['description']}" for s in existing_skills
    ) or "(none)"
    prompt = f"""You are reviewing a new community skill submission for Claude Code.

New skill name: {skill_name}
New skill description: {description}
New skill body (first 500 chars):
{body[:500]}

Existing skills in this marketplace:
{existing_list}

Evaluate and return JSON with these fields:
- is_duplicate (bool): true if this skill substantially overlaps an existing one
- duplicate_of (string|null): name of the existing skill it duplicates, if any
- duplicate_reason (string|null): brief reason if duplicate
- requires_setup (bool): true if the skill requires credentials, MCP server installation, or external tools
- setup_description (string|null): what setup is needed, if any
"""
    raw = extractor.extract_json(
        prompt=prompt,
        max_tokens=300,
        json_schema=_REVIEW_SCHEMA,
        schema_name="SkillReview",
    )
    return SkillReview(
        is_duplicate=bool(raw.get("is_duplicate")),
        duplicate_of=raw.get("duplicate_of"),
        duplicate_reason=raw.get("duplicate_reason"),
        requires_setup=bool(raw.get("requires_setup")),
        setup_description=raw.get("setup_description"),
    )


def write_skill_and_bump_version(
    config: FleaMarketConfig,
    skill_name: str,
    description: str,
    body: str,
) -> str:
    """Write SKILL.md to disk and bump version in plugin.json + marketplace.json.

    Returns skill_md content. Manifests are bumped on disk only; GitHub push
    sends only SKILL.md — manifests are regenerated by nightly sync from GitHub.
    """
    safe_description = re.sub(r"[\r\n]+", " ", description).strip()
    skill_md = f"---\nname: {skill_name}\ndescription: {safe_description}\nuser-invocable: true\n---\n\n{body}\n"

    skill_dir = config.skills_dir() / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    pj_path = config.plugin_json_path()
    pj = json.loads(pj_path.read_text(encoding="utf-8"))
    pj["version"] = _bump_patch(pj["version"])
    plugin_json_str = json.dumps(pj, indent=2)
    pj_path.write_text(plugin_json_str, encoding="utf-8")

    mj_path = config.marketplace_json_path()
    mj = json.loads(mj_path.read_text(encoding="utf-8"))
    matched = False
    for p in mj.get("plugins", []):
        if p.get("name") == config.plugin_name:
            p["version"] = pj["version"]
            matched = True
    if not matched:
        raise ValueError(
            f"plugin {config.plugin_name!r} not found in marketplace.json — "
            "cannot bump version"
        )
    marketplace_json_str = json.dumps(mj, indent=2)
    mj_path.write_text(marketplace_json_str, encoding="utf-8")

    return skill_md


def refresh_serving(marketplace_slug: str) -> None:
    """Refresh the in-memory plugin cache and invalidate ZIP etag cache."""
    try:
        from src.marketplace import refresh_plugin_cache
        refresh_plugin_cache(marketplace_slug)
    except Exception:
        logger.exception("flea_market: plugin cache refresh failed for %s", marketplace_slug)
