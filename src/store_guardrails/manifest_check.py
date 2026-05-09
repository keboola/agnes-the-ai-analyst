"""Manifest & metadata validation for uploaded skill/agent/plugin bundles.

Runs inline (no LLM, no I/O beyond the extracted scratch dir). Verifies the
shape on disk matches the declared ``type`` and that required metadata is
present. Failures here block publication with an actionable detail string
the upload UI can render directly.

We deliberately keep this orthogonal to ``app/api/store.py``'s existing
``_validate_and_extract_metadata`` — that helper extracts the manifest
contents; this one validates them against per-type rules so the guardrail
record explains *why* a submission was rejected.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List


# Semver, permissive. Pre-release / build-meta segments accepted (we don't
# enforce strict semver — operators surface the version as a label, not a
# precedence key). Reject empty + obvious garbage.
_SEMVER_RE = re.compile(
    r"^v?\d+(\.\d+){0,2}([\-+][0-9A-Za-z.\-]+)?$"
)


def check(plugin_dir: Path, type_: str) -> Dict[str, Any]:
    """Validate the extracted bundle against per-type structural rules.

    ``plugin_dir`` is the *baked* plugin tree (post ``_bake_plugin_tree``),
    not the raw extracted scratch — guardrails always see the final shape
    that would land on disk.

    Returns ``{"status": "pass"|"fail", "issues": [...]}``.
    """
    issues: List[str] = []

    if not plugin_dir.is_dir():
        return {"status": "fail", "issues": ["plugin_dir_missing"]}

    if type_ not in {"skill", "agent", "plugin"}:
        return {"status": "fail", "issues": [f"unsupported_type:{type_}"]}

    # Per-type required-shape rules. ``plugin_dir`` is the baked tree
    # (post ``_bake_plugin_tree``):
    #   skill   → plugin_dir/skills/<suffixed>/SKILL.md
    #   agent   → plugin_dir/agents/<suffixed>.md
    #   plugin  → plugin_dir/.claude-plugin/plugin.json
    # We look anywhere under plugin_dir for the required file so this stays
    # robust across small layout drifts and across legacy bundles uploaded
    # before the bake conventions tightened.
    if type_ == "skill":
        skill_md_candidates = list(plugin_dir.rglob("SKILL.md")) + \
                              list(plugin_dir.rglob("skill.md"))
        if not skill_md_candidates:
            issues.append("missing_skill_md")

    elif type_ == "plugin":
        manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
        if not manifest_path.is_file():
            issues.append("missing_plugin_manifest")
        else:
            issues.extend(_validate_plugin_manifest(manifest_path))

    elif type_ == "agent":
        # Agents bake to plugin_dir/agents/*.md, but accept any .md anywhere
        # under the tree so old single-file agent uploads still validate.
        agent_candidates = list(plugin_dir.rglob("*.md"))
        if not agent_candidates:
            issues.append("missing_agent_md")

    return {"status": "pass" if not issues else "fail", "issues": issues}


def _validate_plugin_manifest(path: Path) -> List[str]:
    issues: List[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["plugin_manifest_invalid_json"]

    if not isinstance(data, dict):
        return ["plugin_manifest_not_object"]

    name = data.get("name")
    if not name or not isinstance(name, str):
        issues.append("plugin_manifest_missing_name")
    elif not re.fullmatch(r"[a-zA-Z0-9_\-]{1,64}", name):
        issues.append("plugin_manifest_invalid_name")

    version = data.get("version")
    if version is not None and isinstance(version, str):
        if not _SEMVER_RE.fullmatch(version.strip()):
            issues.append("plugin_manifest_invalid_version")
    return issues
