"""Per-instance Initial Workspace Template — clone, validate, zip.

Mirrors ``src/marketplace.py`` in shape (shallow clone, fetch+reset on
re-sync, token redaction, threading lock) but is a SINGLETON: there is at
most one initial-workspace template per Agnes instance. Configuration
lives in ``instance.yaml`` under ``initial_workspace:`` rather than the
DB (read by ``app/api/initial_workspace.py``, not by this module).

This module has no HTTP surface. Callers (server-side template-repo
management — clone, validate, build_zip, sync_template):
  - ``app/api/initial_workspace.py::sync_endpoint`` (admin "Sync now" click)
  - ``app/api/initial_workspace.py::status_endpoint`` (analyst CLI status probe)
  - ``app/api/initial_workspace.py::zip_endpoint`` (analyst CLI content fetch)

Pure workspace-init helpers (no typer / CLI dependencies) are appended
below ``delete_template_dir``. These are callable from both the CLI
(``cli/lib/initial_workspace.py`` wraps them) and the server-side chat
manager when hydrating per-user workdirs.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.utils import get_initial_workspace_dir
from src.marketplace import _authenticated_url, _redact

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SEC = 300

# Two admins clicking "Sync now" simultaneously would otherwise race on the
# same working directory (one clone in progress, the other tries to fetch
# from a half-cloned target). Process-local lock is enough because Agnes
# is the sole writer to ``${DATA_DIR}/initial-workspace/``.
_sync_lock = threading.Lock()

# Convention: only the contents of ``<repo-root>/workspace/`` get shipped
# to the analyst's local workspace. Anything else in the repo (README.md,
# CI config, docs, scripts for the admin team) lives at the repo root and
# is INVISIBLE to Agnes. This split keeps the repo usable both as a
# normal codebase (with its own README + CI) and as a workspace template.
_WORKSPACE_SUBDIR = "workspace"

# Paths Agnes reserves and refuses to extract from an admin's template
# repo. Stored RELATIVE TO ``<repo-root>/workspace/`` — so
# ``.claude/init-complete`` here means ``workspace/.claude/init-complete``
# in the admin's repo.
#
# ``.claude/init-complete`` is the sentinel the CLI writes at the end of
# ``agnes init`` (and reads on subsequent runs to decide
# resume-vs-refuse semantics). If admin's repo shipped this file, the
# extraction would clobber Agnes's completion-tracking write, breaking
# override-workspace detection.
#
# Surface the rejection at SYNC time (admin sees it in the Sync-now
# modal) rather than at extract time (analyst sees a confusing failure
# half-way through ``agnes init``). The repo on disk stays unchanged —
# admin must commit + push a fix.
_RESERVED_PATHS: tuple[str, ...] = (
    ".claude/init-complete",
)


class TemplateValidationError(ValueError):
    """Raised by ``validate_template_tree`` on a structurally unsafe or
    Agnes-reserved entry. Surfaces in the Sync-now modal so the admin
    sees the specific path that's wrong.
    """


def _run_git(args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SEC,
        check=True,
    )


def validate_template_tree(root: Path) -> None:
    """Walk ``root/workspace/`` and reject paths Agnes refuses to ship.

    Called from two places:
      1. ``sync_template`` after a successful clone — surfaces validation
         errors to the admin in the Sync-now modal response.
      2. ``build_zip`` — second-layer defense in case a path snuck in
         between sync and zip-build (shouldn't happen, but cheap to
         re-check).

    Strict pre-check: ``<root>/workspace/`` MUST exist. The repo root
    itself is admin's territory (README, CI configs, etc.) — only the
    ``workspace/`` subdir reaches the analyst. Repos that don't follow
    this convention are rejected so analysts never receive admin-only
    files by accident.

    Within the ``workspace/`` subtree, rejects:
      * Symlinks anywhere in the tree (analyst-side extraction following
        symlinks would let admin's repo escape the workspace dir).
      * ``..`` segments (defense in depth; ``git clone`` shouldn't produce
        these, but a manually-edited working dir could).
      * Absolute paths (same rationale).
      * Agnes-reserved paths in ``_RESERVED_PATHS`` (currently
        ``.claude/init-complete``, i.e. ``workspace/.claude/init-complete``
        in the repo).

    Raises ``TemplateValidationError`` with the offending path and the
    reason. The error message must NOT leak any token-containing string.
    """
    if not root.exists():
        return
    workspace_dir = root / _WORKSPACE_SUBDIR
    if not workspace_dir.is_dir():
        raise TemplateValidationError(
            f"Repository must contain a {_WORKSPACE_SUBDIR!r} directory at root; "
            "its contents are what gets shipped to analyst workspaces. "
            "Files outside `workspace/` (README, CI configs, etc.) stay in "
            "the repo and are NOT delivered to analysts."
        )
    for entry in workspace_dir.rglob("*"):
        if ".git" in entry.relative_to(workspace_dir).parts:
            # Defensive — admin should never ship a nested `.git/` inside
            # `workspace/`, but ignore if they do (git plumbing of any
            # nested submodule shouldn't reach the analyst).
            continue
        if entry.is_symlink():
            rel = entry.relative_to(workspace_dir).as_posix()
            raise TemplateValidationError(
                f"symlinks are not allowed in the template repo: "
                f"{_WORKSPACE_SUBDIR}/{rel}"
            )
        rel_posix = entry.relative_to(workspace_dir).as_posix()
        if ".." in rel_posix.split("/"):
            raise TemplateValidationError(
                f"path contains '..' segment: {_WORKSPACE_SUBDIR}/{rel_posix}"
            )
        if entry.is_absolute() and not str(entry).startswith(str(workspace_dir)):
            raise TemplateValidationError(
                f"path escapes template root: {_WORKSPACE_SUBDIR}/{rel_posix}"
            )
        if rel_posix in _RESERVED_PATHS:
            raise TemplateValidationError(
                f"path {_WORKSPACE_SUBDIR}/{rel_posix} is reserved by Agnes "
                "(written by `agnes init` after a successful run). "
                "Remove it from your template repo."
            )


def sync_template(
    url: str,
    branch: Optional[str] = None,
    token_env: Optional[str] = None,
) -> dict:
    """Shallow-clone (first run) or fetch+reset (subsequent runs) the
    template repo into ``${DATA_DIR}/initial-workspace/``.

    Returns ``{commit_sha, path, file_count}``. Raises ``RuntimeError`` on
    git failure (token-redacted) or ``TemplateValidationError`` when the
    cloned tree contains an Agnes-reserved or structurally unsafe path.

    Serialized via the module-level ``_sync_lock`` so two parallel admin
    clicks don't race the working directory.
    """
    if not url:
        raise ValueError("initial-workspace template: url is required")

    token = os.environ.get(token_env, "") if token_env else ""
    target = get_initial_workspace_dir()
    auth_url = _authenticated_url(url, token)
    is_git = (target / ".git").is_dir()
    action = "update" if is_git else "clone"

    with _sync_lock:
        try:
            if not is_git:
                if target.exists():
                    shutil.rmtree(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                clone_args = ["clone", "--depth", "1"]
                if branch:
                    clone_args += ["--branch", branch]
                clone_args += [auth_url, str(target)]
                _run_git(clone_args)
            else:
                _run_git(["remote", "set-url", "origin", auth_url], cwd=target)
                ref = branch or "HEAD"
                _run_git(["fetch", "--depth", "1", "origin", ref], cwd=target)
                _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=target)
            sha = _run_git(["rev-parse", "HEAD"], cwd=target).stdout.strip()
        except subprocess.CalledProcessError as e:
            stderr = _redact(e.stderr or "", token).strip()
            raise RuntimeError(f"git {action} failed: {stderr}") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"git {action} timed out after {GIT_TIMEOUT_SEC}s"
            ) from None

        # Run the strict validator AFTER the working tree is settled.
        # A failure here leaves the working dir on disk so the admin can
        # inspect with `git -C ${DATA_DIR}/initial-workspace/ log` what
        # got cloned — we don't auto-rollback.
        validate_template_tree(target)

        files = list_template_files()
        logger.info(
            "initial-workspace %s sha=%s files=%d",
            action, sha, len(files),
        )
        return {"commit_sha": sha, "path": str(target), "file_count": len(files)}


def list_template_files() -> List[str]:
    """Walk ``<initial-workspace>/workspace/``, exclude ``.git/``, return
    sorted POSIX-style relative paths. Deterministic order so
    ``build_zip`` and the status endpoint return stable content / ETag.

    Paths are returned RELATIVE TO the workspace subdir (NOT the repo
    root) — that's the layout the analyst's local workspace will mirror
    after extraction. A repo file at ``workspace/CLAUDE.md`` shows up
    here as ``"CLAUDE.md"``.

    Returns an empty list when the working tree does not exist (i.e.
    template is registered but never synced) OR when the ``workspace/``
    subdir is missing (admin's repo doesn't follow the convention —
    ``sync_template`` would have already failed, this is defense in
    depth for callers that bypass sync).
    """
    target = get_initial_workspace_dir()
    if not target.exists():
        return []
    workspace_dir = target / _WORKSPACE_SUBDIR
    if not workspace_dir.is_dir():
        return []
    out: List[str] = []
    for entry in workspace_dir.rglob("*"):
        if not entry.is_file():
            continue
        rel_parts = entry.relative_to(workspace_dir).parts
        if ".git" in rel_parts:
            continue
        out.append("/".join(rel_parts))
    out.sort()
    return out


def build_zip() -> bytes:
    """Build an in-memory zip of ``<initial-workspace>/workspace/``,
    excluding ``.git/`` and anything outside the ``workspace/`` subdir.
    Re-runs ``validate_template_tree`` first as defense in depth —
    sync_template already validates, but a manual edit on disk between
    sync and zip-fetch should still fail-closed.

    Entry names are relative to ``workspace/`` so the analyst's
    extraction lands files directly at workspace root (e.g.
    ``workspace/CLAUDE.md`` in the repo → ``CLAUDE.md`` at workspace
    root after extraction).

    Returns the zip bytes. Caller computes ``ETag`` from the bytes (or
    from ``last_commit_sha`` for a cheaper stable identifier).
    """
    target = get_initial_workspace_dir()
    validate_template_tree(target)

    workspace_dir = target / _WORKSPACE_SUBDIR
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in list_template_files():
            zf.write(workspace_dir / rel, arcname=rel)
    return buf.getvalue()


def delete_template_dir() -> bool:
    """Remove the working copy at ``${DATA_DIR}/initial-workspace/``.
    Returns True iff the directory existed and was removed.
    """
    target = get_initial_workspace_dir()
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


# ---------------------------------------------------------------------------
# Pure workspace-init helpers — no typer, no prompts, no CLI dependencies.
# Usable from the CLI wrapper AND from the server-side chat manager.
# ---------------------------------------------------------------------------


@dataclass
class TemplateStatus:
    configured: bool = False
    synced: bool = False
    template_source: Optional[str] = None
    template_sha: Optional[str] = None
    synced_at: Optional[str] = None
    files: list[str] = field(default_factory=list)


@dataclass
class ExtractResult:
    overwritten: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)


def extract_zip_to_workspace(zip_bytes: bytes, workspace: Path) -> ExtractResult:
    """Validate then extract every zip entry into ``workspace``.

    Rejects ``..`` traversal, absolute paths, and entries that resolve
    outside ``workspace`` after resolution. Raises ``ValueError`` with a
    short message if any entry is unsafe (caller decides how to surface).
    """
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    overwritten: list[str] = []
    created: list[str] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"unsafe zip entry: {name!r}")
            target = (workspace / name).resolve()
            try:
                target.relative_to(workspace)
            except ValueError as exc:
                raise ValueError(
                    f"unsafe zip entry escapes workspace: {name!r}"
                ) from exc

        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            target = workspace / name
            (overwritten if target.exists() else created).append(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                while chunk := src.read(65536):
                    dst.write(chunk)

    return ExtractResult(overwritten=sorted(overwritten), created=sorted(created))


def write_sentinel(
    workspace: Path,
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
    override: bool,
) -> None:
    """Write ``.claude/init-complete`` marking the workspace as initialized."""
    sentinel = workspace / ".claude" / "init-complete"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        f"completed_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"agnes_version: {agnes_version}\n"
        f"server_url: {server_url}\n"
        f"override: {'true' if override else 'false'}\n"
        f"template_source: {template_source or ''}\n"
        f"template_sha: {template_sha or ''}\n",
        encoding="utf-8",
    )


def is_override_workspace(workspace: Path) -> bool:
    """True iff ``workspace`` was initialised from an admin-configured
    Initial Workspace Template (the sentinel carries ``override: true``).

    False on missing / unreadable sentinel, on sentinel without an override
    key, and on sentinel with ``override`` set to anything other than
    literal ``true`` (case-insensitive).
    """
    sentinel = workspace / ".claude" / "init-complete"
    if not sentinel.exists():
        return False
    try:
        text = sentinel.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower() == "override: true":
            return True
    return False


def initialize_workspace_from_template(
    workspace: Path,
    template_zip_bytes: bytes,
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
) -> ExtractResult:
    """Extract template zip into ``workspace`` and write the override sentinel."""
    result = extract_zip_to_workspace(template_zip_bytes, workspace)
    write_sentinel(
        workspace,
        agnes_version=agnes_version,
        server_url=server_url,
        template_source=template_source,
        template_sha=template_sha,
        override=True,
    )
    return result


def initialize_default_workspace(
    workspace: Path,
    *,
    agnes_version: str,
    server_url: str,
    bundled_template_dir: Path,
) -> ExtractResult:
    """Copy every file from ``bundled_template_dir`` into ``workspace``
    and write the non-override sentinel.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    overwritten: list[str] = []
    created: list[str] = []
    bundled_template_dir = bundled_template_dir.resolve()

    for src in bundled_template_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(bundled_template_dir)
        dst = workspace / rel
        (overwritten if dst.exists() else created).append(str(rel))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    write_sentinel(
        workspace,
        agnes_version=agnes_version,
        server_url=server_url,
        template_source=None,
        template_sha=None,
        override=False,
    )
    return ExtractResult(overwritten=sorted(overwritten), created=sorted(created))
