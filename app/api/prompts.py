"""Unified admin REST surface for managed prompts (#622 Slice 1).

Two managed prompts, addressed by a public ``kind`` vocabulary:

  - ``workspace`` → the analyst workspace ``CLAUDE.md`` (DB key ``claude_md``)
  - ``install``   → the install / setup prompt        (DB key ``welcome``)

Each prompt has an explicit ``source_mode`` toggle (``editor`` ⇄ ``git``) that
supersedes the old implicit ``seed_owns()`` read-only lock:

  - ``editor``: the admin's DB override wins at render time (the editor is
    writable even when an Initial Workspace Template repo is registered — the
    production lock-out this issue fixes).
  - ``git``: the prompt binds to a file in the IWT clone; the editor goes
    read-only and the renderer reads the repo file.

These endpoints are admin-only (web UI surface, no analyst CLI/MCP analogue);
they're classified EXEMPT in ``tests/test_documentation_api_triple_surface.py``.
The legacy ``/api/admin/{welcome,workspace-prompt}-template`` endpoints remain
alive (grandfathered) for the old standalone editors.
"""

from __future__ import annotations

import logging
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jinja2 import Environment, StrictUndefined, TemplateError
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import _get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["prompts"])

# Public `kind` → (repo factory name, canonical seed path, human label).
# The DB keys (claude_md / welcome) stay an internal detail of the repos;
# this is the single translation point per the build spec.
_KINDS = {
    "workspace": "workspace/CLAUDE.md",
    "install": "install-prompt/template.md.tmpl",
}


def _repo(kind: str):
    """Backend-aware repo for a managed prompt kind."""
    from src.repositories import claude_md_template_repo, welcome_template_repo

    if kind == "workspace":
        return claude_md_template_repo()
    if kind == "install":
        return welcome_template_repo()
    raise HTTPException(status_code=404, detail={"kind": "unknown_prompt_kind"})


def _validate_kind(kind: str) -> None:
    if kind not in _KINDS:
        raise HTTPException(
            status_code=404,
            detail={
                "kind": "unknown_prompt_kind",
                "hint": f"kind must be one of {sorted(_KINDS)}",
            },
        )


def _live_default(kind: str, conn, *, user: dict, server_url: str) -> str:
    if kind == "workspace":
        from src.claude_md import compute_default_claude_md

        return compute_default_claude_md(conn, user=user, server_url=server_url)
    from src.welcome_template import compute_default_agent_prompt

    return compute_default_agent_prompt(conn, user=user, server_url=server_url)


def _validate_template(kind: str, content: str) -> None:
    """Two-pass Jinja validation matching the legacy editors' contract.

    Reuses the per-kind stub contexts so a save through /api/admin/prompts is
    held to the same bar as the grandfathered /api/admin/*-template editors.
    """
    if kind == "workspace":
        from app.api.claude_md import (
            _VALIDATION_STUB_CONTEXT,
            _VALIDATION_STUB_CONTEXT_ANON,
        )

        anon_msg = (
            "Template fails for non-admin analyst users: {e}. Wrap "
            "user-dependent expressions in an {% if user.is_admin %} guard."
        )
    else:
        from app.api.welcome import (
            _VALIDATION_STUB_CONTEXT,
            _VALIDATION_STUB_CONTEXT_ANON,
        )

        anon_msg = (
            "Template fails for anonymous /setup visitors: {e}. Wrap "
            "user-dependent expressions in an {% if user %} guard — /setup "
            "is publicly accessible."
        )

    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = env.from_string(content)
        template.render(**_VALIDATION_STUB_CONTEXT)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    try:
        template.render(**_VALIDATION_STUB_CONTEXT_ANON)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=anon_msg.format(e=e))


class PromptGetResponse(BaseModel):
    kind: str
    source_mode: str
    content: Optional[str]
    git_path: Optional[str] = None
    base_sha: Optional[str] = None
    default: str
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    iwt_configured: bool = False
    # --- Slice 2 (#622): per-file blob-sha divergence ---
    diverged: bool = False
    # True iff the bound file's current blob sha != the stored base_sha.
    current_blob_sha: Optional[str] = None
    # Live blob sha of git_path in the IWT clone (None when absent/unbound).


class PromptPutRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


class SourceRequest(BaseModel):
    mode: str = Field(..., pattern="^(editor|git)$")


class BindGitRequest(BaseModel):
    git_path: str = Field(..., min_length=1, max_length=1024)


class PreviewRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


def _git_path_exists(kind: str, git_path: str) -> bool:
    """True iff ``git_path`` resolves to a file in the IWT clone.

    Paths are REPO-relative for both kinds — workspace files live under
    ``workspace/`` (bind e.g. ``workspace/CLAUDE.md``), the install prompt
    at repo root — because ``resolve_prompt`` resolves the stored path
    against the repo root. Validated via ``resolve_seed_file`` constrained
    to the ``iwt`` tier (the bundled fallback is not "operator content"
    you can bind to). An earlier revision validated workspace paths against
    ``list_template_files()`` (workspace-RELATIVE names), which let admins
    bind paths ``resolve_prompt`` could never find — and rejected the ones
    it could (#638 review).
    """
    from src.initial_workspace import resolve_seed_file

    resolved = resolve_seed_file(git_path)
    return resolved is not None and resolved[1] == "iwt"


@router.get("/api/admin/prompts/{kind}", response_model=PromptGetResponse)
async def get_prompt(
    kind: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    from src.initial_workspace import blob_sha, is_configured, resolve_prompt

    meta = _repo(kind).get_meta()
    server_url = str(request.base_url).rstrip("/")
    default = _live_default(kind, conn, user=user, server_url=server_url)

    if meta["source_mode"] == "git":
        git_content, _mode = resolve_prompt(kind, conn)
        content = git_content
    else:
        content = meta["content"]

    # Slice 2 (#622): per-file blob-sha divergence. Meaningful whenever a
    # binding exists (git mode OR a Slice-3 imported-then-edited editor file
    # that keeps its git_path back-reference) — the guard fires on git_path,
    # not source_mode, so editor-mode import-backref divergence flows through
    # here automatically once Slice 3 adds the import action.
    diverged = False
    current_blob = None
    git_path = meta["git_path"]
    if git_path and is_configured():
        current_blob = blob_sha(git_path)
        base = meta["base_sha"]
        # Loud default: a stored base that doesn't match the live blob ->
        # diverged. current_blob None (file removed from the repo) -> diverged.
        if base is not None and current_blob != base:
            diverged = True
        elif base is None and current_blob is not None:
            # Bound but never stamped (legacy / edge) -> diverged so the
            # operator re-reconciles rather than trust a stale bind.
            diverged = True

    return PromptGetResponse(
        kind=kind,
        source_mode=meta["source_mode"],
        content=content,
        git_path=meta["git_path"],
        base_sha=meta["base_sha"],
        default=default,
        updated_at=meta["updated_at"].isoformat() if meta["updated_at"] else None,
        updated_by=meta["updated_by"],
        iwt_configured=is_configured(),
        diverged=diverged,
        current_blob_sha=current_blob,
    )


@router.put("/api/admin/prompts/{kind}")
async def put_prompt(
    kind: str,
    payload: PromptPutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    repo = _repo(kind)
    if repo.get_meta()["source_mode"] == "git":
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "prompt_in_git_mode",
                "hint": (
                    "Switch to Editor override before saving — this prompt is "
                    "bound to the Initial Workspace Template repo."
                ),
            },
        )
    _validate_template(kind, payload.content)
    repo.set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/prompts/{kind}", status_code=204)
async def reset_prompt(
    kind: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    repo = _repo(kind)
    if repo.get_meta()["source_mode"] == "git":
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "prompt_in_git_mode",
                "hint": "No Editor override to reset while in Git source mode.",
            },
        )
    repo.reset(updated_by=user["email"])
    return Response(status_code=204)


@router.post("/api/admin/prompts/{kind}/source")
async def set_source(
    kind: str,
    payload: SourceRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    from src.initial_workspace import is_configured

    if payload.mode == "git" and not is_configured():
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "iwt_not_configured",
                "hint": (
                    "Register an Initial Workspace Template repo in "
                    "/admin/server-config before binding a prompt to Git."
                ),
            },
        )
    _repo(kind).set_source_mode(payload.mode, updated_by=user["email"])
    return {"status": "ok", "source_mode": payload.mode}


@router.post("/api/admin/prompts/{kind}/bind-git")
async def bind_git(
    kind: str,
    payload: BindGitRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    from src.initial_workspace import is_configured

    if not is_configured():
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "iwt_not_configured",
                "hint": (
                    "Register an Initial Workspace Template repo in "
                    "/admin/server-config before binding a prompt to Git."
                ),
            },
        )
    if not _git_path_exists(kind, payload.git_path):
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "git_path_not_found",
                "git_path": payload.git_path,
                "hint": (
                    "Path is not present in the synced Initial Workspace "
                    "Template clone. Paths are repo-relative — workspace "
                    "files live under workspace/ (e.g. workspace/CLAUDE.md). "
                    "Check the path + 'Sync now'."
                ),
            },
        )
    # Stamp the per-file git BLOB sha as the binding's base (Slice 2):
    # precise divergence — flips only when THIS file's content changes, not
    # when any unrelated commit lands. (Slice 1 stamped the HEAD commit sha;
    # the divergence comparator treats a stored commit sha that doesn't match
    # the live blob as diverged, the safe loud default for legacy bindings.)
    from src.initial_workspace import blob_sha

    base = blob_sha(payload.git_path)
    _repo(kind).bind_git(payload.git_path, base_sha=base, updated_by=user["email"])
    return {"status": "ok", "source_mode": "git", "git_path": payload.git_path}


@router.post("/api/admin/prompts/{kind}/preview")
async def preview_prompt(
    kind: str,
    payload: PreviewRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    server_url = str(request.base_url).rstrip("/")
    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = env.from_string(payload.content)
        if kind == "workspace":
            from src.claude_md import build_claude_md_context

            ctx = build_claude_md_context(conn, user=user, server_url=server_url)
        else:
            from src.welcome_template import build_context

            ctx = build_context(user=user, server_url=server_url)
        rendered = template.render(**ctx)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    return {"content": rendered}
