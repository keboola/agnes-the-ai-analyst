"""Select-mode Keboola project import — the user-driven half of the
multi-project login (``auth.keboola.multi_project_mode = "select"``).

A Keboola OAuth sign-in in that mode stashes the discovered projects (plus
the OAuth access token that can mint their PATs) vault-encrypted per user,
for a short TTL. These endpoints let the signed-in user see that discovery
and import a chosen subset; the import runs the same provisioning core the
``auto`` mode uses at login (``app.auth.keboola_provisioning``).

Surface (session/JWT user auth — each caller only ever sees and imports
their OWN discovery; POST-to-collection = "connect these discovered
projects", keeping the URL verb-free per the API design rules):

  GET  /api/auth/keboola/projects  — mode + discovered projects
                                     (imported flag per project)
  POST /api/auth/keboola/projects  — provision selected project ids

REST-only by design (classified ``_EXEMPT`` in the triple-surface ratchet):
this is a continuation of the browser OAuth login, bound to a short-lived
vaulted credential — the CLI has no OAuth login to continue, and an
MCP-exposed tool that mints + vaults upstream credentials would be exactly
the privilege-escalation seam CONTRIBUTING.md's standing exemption names.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.auth import keboola_provisioning as kprov
from app.auth.dependencies import get_current_user
from app.auth.provider_registry import require_provider
from app.auth.providers import keboola_verify as kv

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/auth/keboola",
    tags=["auth"],
    dependencies=[Depends(require_provider("keboola"))],
)


class ImportBody(BaseModel):
    project_ids: List[str]


@router.get("/projects")
async def list_discovered_projects(user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """The caller's pending Keboola project discovery, if any.

    ``discovery_available`` is False when the login didn't run discovery
    (mode ``disabled``/``auto``), when the stash expired (TTL — sign in with
    Keboola again to refresh), or when the vault key is unconfigured.
    """
    mode = kv.multi_project_mode()
    blob = await run_in_threadpool(kprov.load_pending_discovery, str(user.get("id")))

    def _annotate() -> List[Dict[str, Any]]:
        projects = []
        for p in (blob or {}).get("projects") or []:
            project_id = str(p.get("id"))
            projects.append(
                {
                    "id": project_id,
                    "name": p.get("name") or "",
                    "role": p.get("role") or "",
                    "imported": kprov.is_project_connected(project_id),
                }
            )
        return projects

    return {
        "mode": mode,
        "discovery_available": blob is not None,
        "projects": await run_in_threadpool(_annotate) if blob else [],
    }


@router.post("/projects")
async def import_discovered_projects(
    body: ImportBody,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Provision the selected subset of the caller's discovered projects.

    409 ``not_select_mode`` outside select mode (``auto`` imports at login;
    ``disabled`` never discovers). 409 ``discovery_expired`` when there is
    no live stash — sign in with Keboola again. 400 for ids that were never
    discovered. Per-project failures are reported in the response, not as an
    HTTP error; the slow tail (chat-tools introspection, semantic-layer
    refresh) continues as a background task after the response.
    """
    if kv.multi_project_mode() != "select":
        raise HTTPException(status_code=409, detail="not_select_mode")
    if not body.project_ids:
        raise HTTPException(status_code=400, detail="project_ids must be a non-empty list")
    try:
        summary = await run_in_threadpool(kprov.provision_selected, user, [str(pid) for pid in body.project_ids])
    except kprov.DiscoveryStateError as exc:
        status = 409 if exc.reason == "discovery_expired" else 400
        raise HTTPException(status_code=status, detail={"error": exc.reason, "message": exc.detail})
    if summary.connections_needing_chat_tools or summary.semantic_sync_needed:
        background_tasks.add_task(kprov.finish_login_provisioning, summary)
    logger.info(
        "keboola select-mode import for user %s: %d project(s), %d membership add(s)",
        user.get("id"),
        len(summary.outcomes),
        summary.memberships_added,
    )
    return summary.as_dict()
