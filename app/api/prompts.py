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

    Workspace files live under ``workspace/`` and surface in
    ``list_template_files()`` (paths relative to ``workspace/``). The install
    prompt lives at repo root, so it's validated via ``resolve_seed_file``
    constrained to the ``iwt`` tier (the bundled fallback is not "operator
    content" you can bind to).
    """
    from src.initial_workspace import list_template_files, resolve_seed_file

    if kind == "workspace":
        return git_path in set(list_template_files())
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
    from src.initial_workspace import is_configured, resolve_prompt

    meta = _repo(kind).get_meta()
    server_url = str(request.base_url).rstrip("/")
    default = _live_default(kind, conn, user=user, server_url=server_url)

    if meta["source_mode"] == "git":
        git_content, _mode = resolve_prompt(kind, conn)
        content = git_content
    else:
        content = meta["content"]

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
                    "Template clone. Check the path + 'Sync now'."
                ),
            },
        )
    # Stamp the IWT's last commit sha as the binding's base (Slice-2
    # divergence detection metadata; written, not read in Slice 1).
    from app.api.initial_workspace import _read_section

    base_sha = _read_section().get("last_commit_sha")
    _repo(kind).bind_git(payload.git_path, base_sha=base_sha, updated_by=user["email"])
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
