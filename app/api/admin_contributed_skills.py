"""REST API for contributed-skill management.

GET    /api/admin/contributed-skills         — list contributed plugins
POST   /api/admin/contributed-skills         — publish a skill (admin only)
DELETE /api/admin/contributed-skills/{name}  — remove a skill (admin only)
"""

from __future__ import annotations

import json
import shutil
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.auth.access import require_admin
from app.utils import get_marketplaces_dir
from src.marketplace import _lock, _refresh_plugin_cache
from src.repositories import resource_grants_repo
from src.skill_contribution import (
    CONTRIBUTED_MARKETPLACE_SLUG,
    SkillContributionError,
    contribute_skill,
)

router = APIRouter(tags=["admin"])


class _ContributeRequest(BaseModel):
    skill_md: str
    grant_group: str = "Admin"


@router.post("/api/admin/contributed-skills")
def post_contributed_skill(
    body: _ContributeRequest,
    user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Publish a SKILL.md into the Agnes Contributed marketplace."""
    from app.marketplace_server.packager import invalidate_etag_cache

    try:
        result = contribute_skill(
            body.skill_md,
            registered_by=user.get("email") or user.get("id"),
            grant_group=(body.grant_group or "Admin").strip(),
        )
    except SkillContributionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    invalidate_etag_cache()
    return result


@router.get("/api/admin/contributed-skills")
def list_contributed_skills(
    user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """List all plugins in the Agnes Contributed marketplace."""
    repo_root = get_marketplaces_dir() / CONTRIBUTED_MARKETPLACE_SLUG
    plugins_dir = repo_root / "plugins"

    grants = resource_grants_repo().list_all(resource_type="marketplace_plugin")
    grant_map: Dict[str, Optional[str]] = {}
    prefix = f"{CONTRIBUTED_MARKETPLACE_SLUG}/"
    for g in grants:
        rid = g.get("resource_id") or ""
        if rid.startswith(prefix):
            pname = rid[len(prefix) :]
            grant_map[pname] = g.get("group_name")

    plugins: List[Dict[str, Any]] = []
    if plugins_dir.is_dir():
        for entry in sorted(plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            plugin_json_path = entry / ".claude-plugin" / "plugin.json"
            meta: Dict[str, Any] = {"name": entry.name}
            if plugin_json_path.is_file():
                try:
                    meta.update(json.loads(plugin_json_path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
            meta["grant_group"] = grant_map.get(entry.name)
            plugins.append(meta)

    return {"plugins": plugins}


@router.delete("/api/admin/contributed-skills/{name}", status_code=204)
def delete_contributed_skill(
    name: str,
    user: dict = Depends(require_admin),
) -> Response:
    """Remove a contributed skill plugin by name."""
    from app.marketplace_server.packager import invalidate_etag_cache

    repo_root = get_marketplaces_dir() / CONTRIBUTED_MARKETPLACE_SLUG
    plugins_dir = repo_root / "plugins"
    plugin_dir = plugins_dir / name

    if not plugin_dir.exists():
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")

    with _lock:
        shutil.rmtree(plugin_dir)

        manifest_path = repo_root / ".claude-plugin" / "marketplace.json"
        if manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    old_plugins = data.get("plugins")
                    if isinstance(old_plugins, list):
                        data["plugins"] = [
                            p for p in old_plugins if not (isinstance(p, dict) and p.get("name") == name)
                        ]
                    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            except (OSError, ValueError):
                pass

        _refresh_plugin_cache(CONTRIBUTED_MARKETPLACE_SLUG)
        resource_grants_repo().delete_by_resource("marketplace_plugin", f"{CONTRIBUTED_MARKETPLACE_SLUG}/{name}")
        invalidate_etag_cache()

    return Response(status_code=204)
