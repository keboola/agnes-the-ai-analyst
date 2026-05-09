"""Community-driven Store endpoints.

Any authenticated user can upload skills, agents, or plugins as ZIP archives.
Uploaded entities live under ``${DATA_DIR}/store/<entity_id>/`` and are surfaced
into each user's served Claude Code marketplace via ``user_store_installs``.

Per the per-user marketplace composition (see
``src/marketplace_filter.py:resolve_user_marketplace``):

    served_set = (admin_granted ∖ opt_outs) ∪ store_installs

Entities owned by user X have their plugin/skill/agent name suffixed with
``-by-<owner-username>`` at upload time so two owners uploading the same
display name don't collide in Claude Code's flat namespace.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse

import duckdb
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.auth.access import is_user_admin, require_admin
from app.auth.dependencies import _get_db, get_current_user
from app.instance_config import get_guardrails_enabled
from app.utils import get_store_dir
from src.db import get_system_db
from src.repositories.audit import AuditRepository
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.store_submissions import StoreSubmissionsRepository
from src.repositories.user_store_installs import UserStoreInstallsRepository
from src.repositories.users import UserRepository
from src.store_categories import STORE_CATEGORIES, is_valid_category
from src.store_guardrails import InlineResult, run_inline_checks, run_llm_review
from src.store_guardrails.runner import (
    default_api_key_loader,
    default_model_loader,
)
from src.store_naming import (
    compute_entity_version,
    sanitize_username,
    suffixed_name,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/store", tags=["store"])


MAX_ZIP_SIZE = 50 * 1024 * 1024   # 50 MB — matches app/api/upload.py
MAX_PHOTO_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_DOC_SIZE = 10 * 1024 * 1024   # 10 MB per uploaded doc
ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_CHUNK_SIZE = 64 * 1024
_VALID_TYPES = {"skill", "agent", "plugin"}
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_ALLOWED_VIDEO_SCHEMES = {"http", "https"}

# Cap on uncompressed total size of an uploaded ZIP. The compressed-side cap
# is MAX_ZIP_SIZE; an attacker could craft a 50 MB ZIP that decompresses to
# >>10 GB and DOS the host disk via _safe_zip_extract. We sum infolist()
# file_size before extracting and refuse anything above this bound.
MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB


def _suffixed_already_taken(
    conn: duckdb.DuckDBPyConnection,
    suffixed: str,
    *,
    exclude_entity_id: Optional[str] = None,
    exclude_archived: bool = False,
) -> bool:
    """Whether any existing entity ships the same display+invocation name.

    The Store namespace is **flat** in Claude Code — two plugins/skills/agents
    that share a ``name`` collide in the served marketplace catalog (the
    ``manifest_name`` is unique-key for ``/plugin`` lookup) and on-disk inside
    the ``agnes-store-bundle`` (skills/<suffixed>/SKILL.md is the dir name).

    ``sanitize_username`` is many-to-one (``alice.smith`` and ``alice_smith``
    both → ``alice-smith``), so the per-owner UNIQUE on
    ``(owner_user_id, name)`` does NOT prevent the cross-owner collision.
    We enforce global uniqueness on ``name || '-by-' || owner_username``
    here, at upload time, with a clear 409.

    ``exclude_archived=True`` skips rows whose
    ``visibility_status='archived'`` — required by the upload conflict
    check so the same owner can re-upload under the original name after
    archive. The archive path renames the row to free the slug, so this
    flag is belt-and-braces.
    """
    sql = (
        "SELECT id FROM store_entities "
        "WHERE name || '-by-' || owner_username = ?"
    )
    params: List[Any] = [suffixed]
    if exclude_entity_id:
        sql += " AND id != ?"
        params.append(exclude_entity_id)
    if exclude_archived:
        sql += " AND visibility_status != 'archived'"
    return bool(conn.execute(sql, params).fetchone())


def _validate_video_url(value: Optional[str]) -> Optional[str]:
    """Return the URL if it is a safe http(s) URL, raise 400 otherwise.

    Empty / None passes through as None — video_url is optional. Defends
    against ``javascript:``, ``data:``, ``vbscript:`` (etc.) URIs that
    would execute in the viewer's session if rendered inside an ``href``.
    Jinja2 autoescape only HTML-escapes characters; it does not block URI
    schemes inside attribute values.
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    parsed = urlparse(s)
    if parsed.scheme.lower() not in _ALLOWED_VIDEO_SCHEMES or not parsed.netloc:
        raise HTTPException(status_code=400, detail="invalid_video_url")
    return s


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class StoreEntityResponse(BaseModel):
    id: str
    type: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    version: str
    owner_user_id: str
    owner_username: str
    owner_display_name: Optional[str] = None
    install_count: int = 0
    file_size: int = 0
    photo_url: Optional[str] = None
    video_url: Optional[str] = None
    doc_paths: List[str] = []
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    invocation_name: str  # what the user types in Claude Code
    # v32+ quarantine: surface visibility so /store browse can render
    # the corner badge on the submitter's own non-approved cards.
    visibility_status: Optional[str] = None


class StoreEntityListResponse(BaseModel):
    items: List[StoreEntityResponse]
    total: int
    skip: int
    limit: int


class InstallResponse(BaseModel):
    entity_id: str
    installed: bool


class PreviewResponse(BaseModel):
    type: str
    name: Optional[str] = None
    description: Optional[str] = None


class OkResponse(BaseModel):
    ok: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    entity_id: str,
    params: Optional[dict] = None,
) -> None:
    try:
        AuditRepository(conn).log(
            user_id=actor_id,
            action=action,
            resource=f"store_entity:{entity_id}",
            params=params,
        )
    except Exception:
        pass


def _schedule_llm_review(
    background_tasks: BackgroundTasks,
    submission_id: str,
    plugin_dir: Path,
) -> None:
    """Defer the LLM security review to FastAPI's BackgroundTasks queue.

    Runs after the response has been sent so the uploader sees the 202
    immediately while the (slow) Anthropic call happens in the background.
    Pulls a *fresh* DuckDB cursor inside the task — sharing the request's
    cursor across the background path would close it before the task
    fires (FastAPI yields cursors from ``_get_db`` and the cleanup runs
    on the response yield).
    """
    background_tasks.add_task(
        run_llm_review,
        submission_id,
        plugin_dir=plugin_dir,
        conn_factory=get_system_db,
        api_key_loader=default_api_key_loader,
        model_loader=default_model_loader,
    )


def _categories_for_user(conn: duckdb.DuckDBPyConnection, user_id: str) -> List[str]:
    """Return the Store-wide category taxonomy.

    Used to be user-scoped (sourced from user_groups), but groups are an RBAC
    construct — they describe *who* can do what, not what an entity is about.
    Categories are now a fixed taxonomy in ``src.store_categories`` so the
    Store's organizational axis is independent of permissions.
    """
    return list(STORE_CATEGORIES)


def _entity_dir(entity_id: str) -> Path:
    return get_store_dir() / entity_id


def _plugin_dir(entity_id: str) -> Path:
    return _entity_dir(entity_id) / "plugin"


def _assets_dir(entity_id: str) -> Path:
    return _entity_dir(entity_id) / "assets"


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _resolve_owner_display(
    conn: duckdb.DuckDBPyConnection, user_id: str
) -> Optional[str]:
    row = conn.execute(
        "SELECT name, email FROM users WHERE id = ?", [user_id]
    ).fetchone()
    if not row:
        return None
    name, email = row
    if name and str(name).strip():
        return str(name).strip()
    return str(email) if email else None


def _entity_to_response(
    conn: duckdb.DuckDBPyConnection, entity: dict
) -> StoreEntityResponse:
    photo_url = (
        f"/api/store/entities/{entity['id']}/photo" if entity.get("photo_path") else None
    )
    return StoreEntityResponse(
        id=entity["id"],
        type=entity["type"],
        name=entity["name"],
        description=entity.get("description"),
        category=entity.get("category"),
        version=entity["version"],
        owner_user_id=entity["owner_user_id"],
        owner_username=entity["owner_username"],
        owner_display_name=_resolve_owner_display(conn, entity["owner_user_id"]),
        install_count=int(entity.get("install_count") or 0),
        file_size=int(entity.get("file_size") or 0),
        photo_url=photo_url,
        video_url=entity.get("video_url"),
        doc_paths=entity.get("doc_paths") or [],
        created_at=_to_iso(entity.get("created_at")),
        updated_at=_to_iso(entity.get("updated_at")),
        invocation_name=suffixed_name(entity["name"], entity["owner_username"]),
        visibility_status=entity.get("visibility_status") or "approved",
    )


# ---------------------------------------------------------------------------
# Streaming helpers (reuse pattern from app/api/upload.py)
# ---------------------------------------------------------------------------


async def _stream_to_temp(
    file: UploadFile, max_size: int, suffix: str = ".tmp"
) -> tuple[tempfile._TemporaryFileWrapper, int]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    total = 0
    try:
        while True:
            chunk = await file.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"file_too_large (max {max_size // 1024 // 1024}MB)",
                )
            tmp.write(chunk)
        tmp.flush()
    except HTTPException:
        raise
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    tmp.seek(0)
    return tmp, total


# ---------------------------------------------------------------------------
# ZIP validation + bake
# ---------------------------------------------------------------------------


def _safe_zip_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract ``zf`` into ``dest`` while rejecting unsafe members.

    Three guards:

    1. Path traversal (zip-slip) — refuse absolute paths or ``..`` segments.
    2. Decompression bomb — reject if the sum of declared uncompressed sizes
       exceeds ``MAX_ZIP_UNCOMPRESSED``. The compressed-side cap
       (``MAX_ZIP_SIZE``) does not bound the decompressed footprint; a 50 MB
       ZIP at ratio 1:1000 expands to 50 GB on disk.
    3. (Note) Python's stdlib ``ZipFile.extractall`` does NOT honor symlink
       mode bits — symlink entries are written as regular files containing
       the link target text, not as actual symlinks. So no extra symlink
       guard is needed for the stdlib path.
    """
    dest_resolved = dest.resolve()
    total_uncompressed = 0
    for member in zf.infolist():
        # Path traversal.
        member_path = Path(member.filename)
        if member_path.is_absolute() or any(part == ".." for part in member_path.parts):
            raise HTTPException(status_code=422, detail="zip_unsafe_path")
        target = (dest / member_path).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            raise HTTPException(status_code=422, detail="zip_unsafe_path")

        # Decompression bomb. We sum declared sizes — these are advisory
        # (an attacker can lie) but mismatched values trip Python's own
        # CRC/size check during read. The pre-extract sum catches the
        # honest-malicious case (large declared sizes) and is the last
        # cheap fence before extractall touches the disk.
        total_uncompressed += int(member.file_size or 0)
        if total_uncompressed > MAX_ZIP_UNCOMPRESSED:
            raise HTTPException(
                status_code=413,
                detail=f"zip_too_large_uncompressed (max {MAX_ZIP_UNCOMPRESSED // 1024 // 1024}MB)",
            )

    zf.extractall(dest)


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    body = m.group(1)
    out: dict = {}
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _set_frontmatter_name(text: str, new_name: str) -> str:
    """Rewrite the ``name`` field in YAML-ish frontmatter, inserting one if
    the document has frontmatter without a ``name`` key. If there is no
    frontmatter at all, prepend a minimal block.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return f"---\nname: {new_name}\n---\n\n{text}"

    body = m.group(1)
    new_body_lines: List[str] = []
    found = False
    for line in body.splitlines():
        if not found and re.match(r"^\s*name\s*:", line):
            new_body_lines.append(f"name: {new_name}")
            found = True
        else:
            new_body_lines.append(line)
    if not found:
        new_body_lines.insert(0, f"name: {new_name}")
    new_body = "\n".join(new_body_lines)
    return f"---\n{new_body}\n---" + text[m.end():]


def _find_skill_md(root: Path) -> Optional[Path]:
    for p in sorted(root.rglob("SKILL.md")):
        if p.is_file():
            return p
    return None


def _find_agent_md(root: Path) -> Optional[Path]:
    """An agent is any *.md (with name + description frontmatter) that is
    NOT a SKILL.md and is NOT located inside a ``.claude-plugin/`` directory.
    The two exclusions are how we keep skill / plugin ZIPs from
    accidentally validating as agents — SKILL.md has the same frontmatter
    fields as an agent definition, and plugin.json siblings live under
    ``.claude-plugin/``.
    """
    for p in sorted(root.rglob("*.md")):
        if not p.is_file():
            continue
        if p.name.lower() == "skill.md":
            continue
        # Reject anything below a .claude-plugin/ ancestor.
        if any(part == ".claude-plugin" for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        if "name" in fm and "description" in fm:
            return p
    return None


def _find_plugin_json(root: Path) -> Optional[Path]:
    for p in sorted(root.rglob("plugin.json")):
        if p.is_file() and p.parent.name == ".claude-plugin":
            return p
    return None


def _validate_and_extract_metadata(
    type_: str, extracted_root: Path
) -> dict:
    """Return ``{"name": str | None, "description": str | None}`` parsed from
    the ZIP for pre-fill. Raises 422 if the ZIP layout doesn't match ``type``.

    Cross-type guards reject obvious mismatches so a skill ZIP can't be
    smuggled in as an agent (same frontmatter shape) and a plugin ZIP can't
    masquerade as a skill or agent (plugin trees often contain inner skills
    + agents that share their signatures):

      * skill  → must have SKILL.md and must NOT have a top-level
                  .claude-plugin/plugin.json (would be a plugin)
      * agent  → must have a non-SKILL .md with name+description frontmatter
                  outside any .claude-plugin/ folder, must NOT have SKILL.md
                  (would be a skill), and must NOT have .claude-plugin/plugin.json
                  (would be a plugin)
      * plugin → must have .claude-plugin/plugin.json
    """
    has_skill_md = _find_skill_md(extracted_root) is not None
    has_plugin_json = _find_plugin_json(extracted_root) is not None

    if type_ == "skill":
        if has_plugin_json:
            raise HTTPException(status_code=422, detail="zip_looks_like_plugin")
        if not has_skill_md:
            raise HTTPException(status_code=422, detail="zip_missing_skill_md")
        skill_md = _find_skill_md(extracted_root)
        text = skill_md.read_text(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        fm = _parse_frontmatter(text)
        return {"name": fm.get("name"), "description": fm.get("description")}

    if type_ == "agent":
        if has_skill_md:
            raise HTTPException(status_code=422, detail="zip_looks_like_skill")
        if has_plugin_json:
            raise HTTPException(status_code=422, detail="zip_looks_like_plugin")
        agent_md = _find_agent_md(extracted_root)
        if agent_md is None:
            raise HTTPException(
                status_code=422,
                detail="zip_missing_agent_md_with_frontmatter",
            )
        fm = _parse_frontmatter(agent_md.read_text(encoding="utf-8", errors="replace"))
        return {"name": fm.get("name"), "description": fm.get("description")}

    if type_ == "plugin":
        if not has_plugin_json:
            raise HTTPException(
                status_code=422,
                detail="zip_missing_claude_plugin_json",
            )
        pj = _find_plugin_json(extracted_root)
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except (OSError, ValueError):
            raise HTTPException(status_code=422, detail="plugin_json_invalid")
        if not isinstance(data, dict):
            raise HTTPException(status_code=422, detail="plugin_json_invalid")
        return {"name": data.get("name"), "description": data.get("description")}

    raise HTTPException(status_code=422, detail=f"unknown_type:{type_}")


def _bake_plugin_tree(
    *,
    type_: str,
    extracted_root: Path,
    plugin_dir: Path,
    final_name: str,
    suffixed: str,
    description: Optional[str],
) -> int:
    """Materialize the canonical Claude Code plugin tree at ``plugin_dir``.

    Returns the total bytes written. Caller computes the version hash *after*
    this finishes (so the hash covers exactly what's served on disk).
    """
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)

    if type_ == "skill":
        skill_md = _find_skill_md(extracted_root)
        if skill_md is None:
            raise HTTPException(status_code=422, detail="zip_missing_skill_md")
        skill_root = skill_md.parent
        target = plugin_dir / "skills" / suffixed
        target.mkdir(parents=True, exist_ok=True)
        for f in skill_root.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(skill_root)
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if f == skill_md:
                # Rewrite frontmatter `name` to the suffixed value.
                text = f.read_text(encoding="utf-8", errors="replace")
                dest.write_text(_set_frontmatter_name(text, suffixed), encoding="utf-8")
            else:
                shutil.copy2(f, dest)
        _write_synth_plugin_json(plugin_dir, suffixed, description)

    elif type_ == "agent":
        agent_md = _find_agent_md(extracted_root)
        if agent_md is None:
            raise HTTPException(status_code=422, detail="zip_missing_agent_md_with_frontmatter")
        target_dir = plugin_dir / "agents"
        target_dir.mkdir(parents=True, exist_ok=True)
        text = agent_md.read_text(encoding="utf-8", errors="replace")
        rewritten = _set_frontmatter_name(text, suffixed)
        (target_dir / f"{suffixed}.md").write_text(rewritten, encoding="utf-8")
        _write_synth_plugin_json(plugin_dir, suffixed, description)

    elif type_ == "plugin":
        # Mirror the upload as-is, then rewrite plugin.json `name` to suffixed.
        for f in extracted_root.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(extracted_root)
            dest = plugin_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        pj_path = plugin_dir / ".claude-plugin" / "plugin.json"
        if not pj_path.is_file():
            raise HTTPException(status_code=422, detail="zip_missing_claude_plugin_json")
        try:
            data = json.loads(pj_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(status_code=422, detail="plugin_json_invalid")
        if not isinstance(data, dict):
            raise HTTPException(status_code=422, detail="plugin_json_invalid")
        data["name"] = suffixed
        pj_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    else:
        raise HTTPException(status_code=422, detail=f"unknown_type:{type_}")

    total = 0
    for f in plugin_dir.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def _write_synth_plugin_json(
    plugin_dir: Path, suffixed: str, description: Optional[str]
) -> None:
    target = plugin_dir / ".claude-plugin"
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": suffixed,
        "description": description or "",
    }
    (target / "plugin.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _rename_baked_tree(
    *,
    type_: str,
    plugin_dir: Path,
    old_suffix: str,
    new_suffix: str,
    description: Optional[str],
) -> None:
    """Rename a baked entity's on-disk slug so a re-uploader can take
    the original name.

    Per-type layout (see :func:`_bake_plugin_tree`):
      * ``skill``  — rename ``skills/<old>/`` → ``skills/<new>/`` and
        rewrite ``SKILL.md`` frontmatter ``name``.
      * ``agent``  — rename ``agents/<old>.md`` → ``agents/<new>.md``
        and rewrite the file's frontmatter ``name``.
      * ``plugin`` — tree paths don't carry the suffix; only the
        synth ``.claude-plugin/plugin.json`` ``name`` field changes.

    Always rewrites the synth ``plugin.json`` ``name`` to the new
    suffix. Idempotent — old==new is a no-op.
    """
    if old_suffix == new_suffix or not plugin_dir.is_dir():
        return

    if type_ == "skill":
        old_dir = plugin_dir / "skills" / old_suffix
        new_dir = plugin_dir / "skills" / new_suffix
        if old_dir.is_dir() and not new_dir.exists():
            old_dir.rename(new_dir)
        skill_md = new_dir / "SKILL.md"
        if skill_md.is_file():
            text = skill_md.read_text(encoding="utf-8", errors="replace")
            skill_md.write_text(
                _set_frontmatter_name(text, new_suffix), encoding="utf-8"
            )
    elif type_ == "agent":
        old_md = plugin_dir / "agents" / f"{old_suffix}.md"
        new_md = plugin_dir / "agents" / f"{new_suffix}.md"
        if old_md.is_file() and not new_md.exists():
            old_md.rename(new_md)
        if new_md.is_file():
            text = new_md.read_text(encoding="utf-8", errors="replace")
            new_md.write_text(
                _set_frontmatter_name(text, new_suffix), encoding="utf-8"
            )
    elif type_ == "plugin":
        pj_path = plugin_dir / ".claude-plugin" / "plugin.json"
        if pj_path.is_file():
            try:
                data = json.loads(pj_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data["name"] = new_suffix
                    pj_path.write_text(
                        json.dumps(data, indent=2), encoding="utf-8"
                    )
            except (OSError, ValueError):
                pass

    # Always rewrite the synth plugin.json (skill + agent path) so the
    # name aligns with the renamed slug.
    if type_ in ("skill", "agent"):
        _write_synth_plugin_json(plugin_dir, new_suffix, description)


# ---------------------------------------------------------------------------
# Listing + detail endpoints
# ---------------------------------------------------------------------------


@router.get("/categories", response_model=List[str])
async def my_categories(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Caller's group names — populates the upload form's category select."""
    return _categories_for_user(conn, user["id"])


class OwnerOption(BaseModel):
    user_id: str
    display_name: str
    entity_count: int


@router.get("/owners", response_model=List[OwnerOption])
async def list_owners(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Owners who have at least one entity in the Store — populates the
    listing-page owner filter. Sorted by display name (name fallbacks to
    email then to username) so the dropdown is alphabetical and stable.

    Visibility filter (v32+/v35): non-admin sees owners-of-approved
    only (a submitter with N quarantined uploads must not surface in
    the public dropdown until at least one is approved). Admin sees
    every owner regardless of state.
    """
    if is_user_admin(user["id"], conn):
        where_sql = ""
        params: list = []
    else:
        # 'approved' is the public set. Owners of only-archived /
        # only-pending / only-blocked entries don't appear in the
        # public dropdown — they have nothing to filter to.
        where_sql = "WHERE se.visibility_status = 'approved'"
        params = []
    rows = conn.execute(
        f"""SELECT
               se.owner_user_id,
               COALESCE(NULLIF(TRIM(u.name), ''), u.email, se.owner_username) AS display_name,
               COUNT(*) AS entity_count
           FROM store_entities se
           LEFT JOIN users u ON u.id = se.owner_user_id
           {where_sql}
           GROUP BY se.owner_user_id, display_name
           ORDER BY display_name""",
        params,
    ).fetchall()
    return [
        OwnerOption(
            user_id=r[0],
            display_name=str(r[1]),
            entity_count=int(r[2]),
        )
        for r in rows
    ]


@router.get("/entities", response_model=StoreEntityListResponse)
async def list_entities(
    skip: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
    type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    owner: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if type and type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="invalid_type")
    repo = StoreEntitiesRepository(conn)
    # Visibility filter: hide pending/blocked from the public flea browse.
    # An owner viewing their own uploads (`owner=<self_id>`) sees their
    # whole catalogue regardless of guardrail status — same goes for
    # admins via the existing /admin path. Anyone else only sees approved.
    visibility_filter: Optional[List[str]]
    include_owner_id: Optional[str] = None
    is_admin = is_user_admin(user["id"], conn)
    is_self_owner = bool(owner and owner == user["id"])
    if is_admin or is_self_owner:
        visibility_filter = None
    else:
        visibility_filter = ["approved"]
        # Owner sees their own non-approved entries in the listing too
        # so they spot what they uploaded that's still under review or
        # quarantined. The card template renders a status badge for
        # those rows; without this an upload silently disappears from
        # the grid the moment it's quarantined.
        include_owner_id = user["id"]
    items, total = repo.list(
        skip=skip,
        limit=limit,
        type=type,
        category=category,
        search=search,
        owner_user_id=owner,
        visibility_status=visibility_filter,
        include_owner_id=include_owner_id,
    )
    return StoreEntityListResponse(
        items=[_entity_to_response(conn, e) for e in items],
        total=total,
        skip=skip,
        limit=limit,
    )


def _enforce_visibility(entity: dict, user: dict, conn) -> None:
    """Refuse asset reads on quarantined entities for non-owner non-admin.

    Returns 404 (not 403) so the existence of the entity is not leaked
    via timing / status-code differences. Owner + admin always pass.
    """
    if entity.get("visibility_status") == "approved":
        return
    if entity.get("owner_user_id") == user.get("id"):
        return
    if is_user_admin(user["id"], conn):
        return
    raise HTTPException(status_code=404, detail="entity_not_found")


@router.get("/entities/{entity_id}", response_model=StoreEntityResponse)
async def get_entity(
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    _enforce_visibility(entity, user, conn)
    return _entity_to_response(conn, entity)


@router.get("/entities/{entity_id}/files")
async def list_entity_files(
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    _enforce_visibility(entity, user, conn)
    plugin_dir = _plugin_dir(entity_id)
    if not plugin_dir.is_dir():
        return {"files": []}
    files = []
    for f in sorted(plugin_dir.rglob("*")):
        if f.is_file():
            files.append(
                {
                    "path": f.relative_to(plugin_dir).as_posix(),
                    "size": f.stat().st_size,
                }
            )
    return {"files": files}


@router.get("/entities/{entity_id}/photo")
async def get_entity_photo(
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity or not entity.get("photo_path"):
        raise HTTPException(status_code=404, detail="photo_not_found")
    _enforce_visibility(entity, user, conn)
    abs_path = _entity_dir(entity_id) / entity["photo_path"]
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="photo_not_found")
    return FileResponse(abs_path)


@router.get("/entities/{entity_id}/docs/{filename}")
async def get_entity_doc(
    entity_id: str,
    filename: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream an attached doc — directory-traversal-guarded."""
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    _enforce_visibility(entity, user, conn)
    docs_dir = (_assets_dir(entity_id) / "docs").resolve()
    abs_path = (docs_dir / filename).resolve()
    try:
        abs_path.relative_to(docs_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_path")
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="doc_not_found")
    return FileResponse(abs_path, filename=filename)


# ---------------------------------------------------------------------------
# Preview — POST /api/store/entities/preview
# ---------------------------------------------------------------------------


@router.post("/entities/preview", response_model=PreviewResponse)
async def preview_entity(
    file: UploadFile = File(...),
    type: str = Form(...),
    user: dict = Depends(get_current_user),
):
    """Wizard step 1 — validate the uploaded ZIP and parse frontmatter for
    pre-fill on step 2. Does **not** persist anything: tmp dir is wiped before
    the response returns. The browser must hold the same File and re-submit
    it on step 2 (POST /entities) for the actual create.
    """
    if type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="invalid_type")

    tmp, _ = await _stream_to_temp(file, MAX_ZIP_SIZE, suffix=".zip")
    try:
        tmp.close()
        scratch = Path(tempfile.mkdtemp(prefix="agnes_store_preview_"))
        try:
            try:
                with zipfile.ZipFile(tmp.name, "r") as zf:
                    _safe_zip_extract(zf, scratch)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=422, detail="zip_invalid")
            meta = _validate_and_extract_metadata(type, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    return PreviewResponse(
        type=type,
        name=meta.get("name"),
        description=meta.get("description"),
    )


# ---------------------------------------------------------------------------
# Create (upload) — POST /api/store/entities
# ---------------------------------------------------------------------------


@router.post("/entities", response_model=StoreEntityResponse, status_code=201)
async def create_entity(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    type: str = Form(...),
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    video_url: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    docs: List[UploadFile] = File(default=[]),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="invalid_type")

    try:
        username = sanitize_username(user["email"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=400, detail="invalid_email")

    # Category: must be one of caller's groups.
    if category and not is_valid_category(category):
        raise HTTPException(status_code=400, detail="invalid_category")

    video_url = _validate_video_url(video_url)

    # Per-submitter spam quota (v30). When persisted-bundle is on, a bot
    # looping on malformed ZIPs would otherwise fill disk + the admin
    # queue with noise. Cap rejected uploads per submitter per 24h;
    # operator can disable by setting the knob to 0.
    #
    # #9 widening: counts blocked_inline + blocked_llm + review_error so
    # a submitter triggering only LLM-blocked verdicts is bounded too.
    #
    # Race note (#5, deferred): two parallel uploads from the same
    # submitter can both pass the SELECT before either INSERT — the cap
    # may be exceeded by the number of in-flight requests. A
    # threading.Lock would block the asyncio event loop; a proper fix
    # needs an asyncio.Lock held across the entire pipeline (extract +
    # check + insert) which is a larger restructure tracked separately.
    # API-level rate limiting (slowapi) bounds the worst case until then.
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from app.instance_config import get_guardrails_blocked_quota_per_day
    quota = get_guardrails_blocked_quota_per_day()
    if quota > 0:
        since = _dt.now(_tz.utc) - _td(hours=24)
        recent_blocked = StoreSubmissionsRepository(conn) \
            .count_blocked_for_submitter_since(user["id"], since)
        if recent_blocked >= quota:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "quota_exceeded",
                    "blocked_in_last_24h": recent_blocked,
                    "limit": quota,
                    "hint": "Fix the previous upload errors before retrying. "
                            "Quota resets 24h after each blocked attempt.",
                },
            )

    # Stream + extract ZIP into a scratch dir. Both the temp-file (`tmp`)
    # AND the scratch dir need cleanup on every exit path, including
    # validation HTTPExceptions raised inside _safe_zip_extract
    # (zip_unsafe_path, zip_too_large_uncompressed) and the BadZipFile→422
    # conversion. Pre-fix the scratch was created in one try/finally and
    # cleaned up in a SEPARATE one — when extraction raised, control
    # exited the first scope and the second never ran, leaking the dir.
    # Single try/finally fixes both.
    tmp, size = await _stream_to_temp(file, MAX_ZIP_SIZE, suffix=".zip")
    tmp.close()
    scratch = Path(tempfile.mkdtemp(prefix="agnes_store_"))
    try:
        try:
            with zipfile.ZipFile(tmp.name, "r") as zf:
                _safe_zip_extract(zf, scratch)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=422, detail="zip_invalid")
        finally:
            Path(tmp.name).unlink(missing_ok=True)

        meta = _validate_and_extract_metadata(type, scratch)
        final_name = (name or meta.get("name") or "").strip()
        if not final_name:
            raise HTTPException(status_code=400, detail="missing_name")
        if not _NAME_RE.match(final_name):
            raise HTTPException(status_code=400, detail="invalid_name_format")
        final_description = description or meta.get("description")

        repo = StoreEntitiesRepository(conn)
        # Skip archived rows: archive renames the row to free the slot,
        # so a same-name re-upload after archive succeeds. Active rows
        # (approved / pending / hidden) still 409 on collision.
        if repo.get_by_owner_and_name(user["id"], final_name, exclude_archived=True):
            raise HTTPException(status_code=409, detail="conflict_owner_name")

        entity_id = uuid.uuid4().hex
        suffixed = suffixed_name(final_name, username)
        # Global cross-owner check — sanitize_username is many-to-one, so
        # two emails (alice.smith / alice_smith) can resolve to the same
        # username and produce the same `<name>-by-<username>` suffix even
        # when the per-owner UNIQUE passes. The suffixed value drives both
        # the bundle on-disk dir and the served plugin.json `name`, so a
        # collision silently last-write-wins. Refuse upfront — but skip
        # archived rows since archive renames their slug.
        if _suffixed_already_taken(conn, suffixed, exclude_archived=True):
            raise HTTPException(status_code=409, detail="conflict_global_suffix")
        plugin_dir = _plugin_dir(entity_id)
        file_size = _bake_plugin_tree(
            type_=type,
            extracted_root=scratch,
            plugin_dir=plugin_dir,
            final_name=final_name,
            suffixed=suffixed,
            description=final_description,
        )
        version = compute_entity_version(plugin_dir)

        # v37: also seed versions/v1/plugin/ so the restore endpoint
        # can copy v1 bytes forward later. Same content as the live
        # plugin/ dir; cheap copy.
        v1_plugin = _entity_dir(entity_id) / "versions" / "v1" / "plugin"
        v1_plugin.parent.mkdir(parents=True, exist_ok=True)
        if v1_plugin.exists():
            shutil.rmtree(v1_plugin, ignore_errors=True)
        shutil.copytree(plugin_dir, v1_plugin)

        # ---- Guardrail pipeline ------------------------------------------
        #
        # Inline checks (manifest, static security, quality+templating)
        # run synchronously against the BAKED plugin tree.
        #
        # On fail (v30): we KEEP the bundle on disk, create the entity
        # row at ``visibility_status='hidden'`` (invisible in flea
        # browse + served marketplace), persist the submission with
        # entity_id + sha + size. Admin can Rescan / Override /
        # Download from /admin/store/submissions. The 30-day TTL job
        # purges bundle bytes later but keeps the audit row.
        #
        # On pass: create the entity in 'pending' visibility, schedule
        # the (slow) LLM review on BackgroundTasks. Entity only
        # becomes visible after the review approves it (or admin
        # overrides). See docs/STORE_GUARDRAILS.md.
        from src.store_guardrails.bundle_meta import compute_bundle_meta
        bundle_meta = compute_bundle_meta(plugin_dir)
        inline = run_inline_checks(
            plugin_dir, type_=type, description=final_description,
        )
        subs_repo = StoreSubmissionsRepository(conn)
        if not inline.passed:
            # Persist the bundle on disk so admins can Rescan/Download.
            # No rmtree here — TTL purge owns deletion now.
            photo_rel = await _save_photo(photo, entity_id) if photo else None
            doc_rels = await _save_docs(docs, entity_id)
            repo.create(
                id=entity_id,
                owner_user_id=user["id"],
                owner_username=username,
                type=type,
                name=final_name,
                description=final_description,
                category=category,
                version=version,
                photo_path=photo_rel,
                video_url=video_url,
                doc_paths=doc_rels,
                file_size=file_size,
                visibility_status="hidden",
            )
            sub_id = subs_repo.create(
                submitter_id=user["id"],
                submitter_email=user.get("email"),
                type=type,
                name=final_name,
                version=version,
                status="blocked_inline",
                entity_id=entity_id,
                inline_checks=inline.to_response_dict(),
                file_size=bundle_meta.file_size,
                bundle_sha256=bundle_meta.sha256,
            )
            _audit(
                conn, user["id"], "store.submission.blocked_inline",
                sub_id, {
                    "type": type, "name": final_name,
                    "entity_id": entity_id,
                    "manifest": inline.manifest.get("status"),
                    "static_security": inline.static_security.get("status"),
                    "static_findings": len(inline.static_security.get("findings") or []),
                    "sha256": bundle_meta.sha256,
                    "file_size": bundle_meta.file_size,
                },
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "submission_blocked",
                    "submission_id": sub_id,
                    "entity_id": entity_id,
                    "checks": inline.to_response_dict(),
                },
            )

        guardrails_on = get_guardrails_enabled()
        # When the pipeline is disabled (local dev / no ANTHROPIC_API_KEY)
        # we approve immediately. Inline checks ran above and recorded a
        # passing verdict; skip the async LLM step and the 'pending' hold.
        initial_visibility = "approved" if not guardrails_on else "pending"
        photo_rel = await _save_photo(photo, entity_id) if photo else None
        doc_rels = await _save_docs(docs, entity_id)

        repo.create(
            id=entity_id,
            owner_user_id=user["id"],
            owner_username=username,
            type=type,
            name=final_name,
            description=final_description,
            category=category,
            version=version,
            photo_path=photo_rel,
            video_url=video_url,
            doc_paths=doc_rels,
            file_size=file_size,
            visibility_status=initial_visibility,
        )
        _audit(
            conn,
            user["id"],
            "store.entity.create",
            entity_id,
            {"type": type, "name": final_name, "version": version, "size": file_size},
        )

        sub_id = subs_repo.create(
            submitter_id=user["id"],
            submitter_email=user.get("email"),
            type=type,
            name=final_name,
            version=version,
            status="approved" if not guardrails_on else "pending_llm",
            entity_id=entity_id,
            inline_checks=inline.to_response_dict(),
            file_size=bundle_meta.file_size,
            bundle_sha256=bundle_meta.sha256,
        )
        _audit(
            conn, user["id"],
            "store.submission.accepted" if guardrails_on else "store.submission.approved",
            sub_id, {
                "entity_id": entity_id,
                "guardrails_enabled": guardrails_on,
            },
        )
        if guardrails_on:
            _schedule_llm_review(background_tasks, sub_id, plugin_dir)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    _invalidate_etag()
    entity = StoreEntitiesRepository(conn).get(entity_id)
    return _entity_to_response(conn, entity)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Update — PUT /api/store/entities/{id}
# ---------------------------------------------------------------------------


@router.put("/entities/{entity_id}", response_model=StoreEntityResponse)
async def update_entity(
    entity_id: str,
    background_tasks: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    name: Optional[str] = Form(None),
    type: Optional[str] = Form(None),  # noqa: A002
    description: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    video_url: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Edit a flea-market entity. Owner or admin.

    v37 edit feature semantics:

    * **Type is locked** — passing ``type`` that differs from the
      stored row returns 400 ``type_locked``. Replacing one form
      factor with another is a fresh upload, not an edit.
    * **Display-name change** is allowed — triggers a slug rename in
      the live bundle (mirrors the rename-on-archive flow). Existing
      installers see the plugin renamed on next sync.
    * **Bundle change** creates a new version: bake into
      ``versions/v<N+1>/plugin/``, run guardrails, on approval copy
      to the live ``plugin/`` dir + bump ``version_no`` + append
      ``version_history``. The prior version dir stays so rollback
      can copy it forward.
    * **Block-while-pending**: an in-flight LLM review (the entity's
      ``visibility_status='pending'`` AND the latest submission has
      ``status IN ('pending_inline','pending_llm')``) blocks any
      further edit with 409 ``prior_version_pending``. Owner waits
      for the verdict; the detail page auto-refreshes.
    * **Metadata-only edit** (no ``file`` posted) skips the bundle
      pipeline and the version bump.
    """
    repo = StoreEntitiesRepository(conn)
    entity = repo.get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    if entity["owner_user_id"] != user["id"] and not is_user_admin(user["id"], conn):
        raise HTTPException(status_code=403, detail="not_owner")

    # Type is immutable — reject change attempts up front.
    if type is not None and type != entity["type"]:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "type_locked",
                "message": "Cannot change a flea entity's type. "
                           "Upload a new entity instead.",
            },
        )

    # Block-while-pending: an in-flight review must complete (or be
    # reaped) before another version can be uploaded. Metadata-only
    # edits are also blocked for UX consistency — one rule for the
    # owner instead of "metadata yes / bundle no".
    if entity.get("visibility_status") == "pending":
        latest_sub = StoreSubmissionsRepository(conn).latest_for_entity(entity_id)
        if latest_sub and latest_sub.get("status") in (
            "pending_inline", "pending_llm",
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "prior_version_pending",
                    "message": "A previous edit is still under review. "
                               "Wait for the verdict to finish before "
                               "submitting another change.",
                    "submission_id": latest_sub.get("id"),
                },
            )

    if category and not is_valid_category(category):
        raise HTTPException(status_code=400, detail="invalid_category")

    video_url = _validate_video_url(video_url)

    # Display-name change handled at the end (after bundle bake) so the
    # rename can target the version-bumped or current bundle dir.
    rename_to: Optional[str] = None
    if name is not None and name.strip() and name.strip() != entity["name"]:
        new_name = name.strip()
        if not _NAME_RE.match(new_name):
            raise HTTPException(status_code=400, detail="invalid_name_format")
        # Same-owner conflict (skip archived which already freed slot).
        if repo.get_by_owner_and_name(
            entity["owner_user_id"], new_name, exclude_archived=True,
        ):
            raise HTTPException(status_code=409, detail="conflict_owner_name")
        # Cross-owner suffix — must be globally unique post-rename.
        new_suffixed = suffixed_name(new_name, entity["owner_username"])
        if _suffixed_already_taken(
            conn, new_suffixed, exclude_entity_id=entity_id,
            exclude_archived=True,
        ):
            raise HTTPException(status_code=409, detail="conflict_global_suffix")
        rename_to = new_name

    new_version: Optional[str] = None
    new_size: Optional[int] = None
    inline_after_update: Optional[InlineResult] = None
    new_version_dir: Optional[Path] = None  # set when bundle uploaded
    new_version_no: Optional[int] = None
    if file is not None:
        # PUT atomicity + version history: bake the new bundle into the
        # versioned dir ``versions/v<N+1>/plugin/`` and run checks
        # there. On approval the live ``plugin/`` dir is replaced with
        # a copy of the new version's contents — prior versions stay on
        # disk so rollback can copy them forward.
        tmp, size = await _stream_to_temp(file, MAX_ZIP_SIZE, suffix=".zip")
        tmp.close()
        scratch = Path(tempfile.mkdtemp(prefix="agnes_store_"))
        existing_plugin = _plugin_dir(entity_id)
        new_version_no = int(entity.get("version_no") or 1) + 1
        version_root = _entity_dir(entity_id) / "versions" / f"v{new_version_no}"
        staging_plugin = version_root / "plugin"
        new_version_dir = version_root  # exposed to outer scope
        backup_plugin: Optional[Path] = None  # set if the swap starts
        try:
            try:
                with zipfile.ZipFile(tmp.name, "r") as zf:
                    _safe_zip_extract(zf, scratch)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=422, detail="zip_invalid")
            finally:
                Path(tmp.name).unlink(missing_ok=True)

            _validate_and_extract_metadata(entity["type"], scratch)
            suffixed = suffixed_name(entity["name"], entity["owner_username"])
            # Bake into the staging dir — _bake_plugin_tree creates the
            # target if missing and does its own rmtree on existing
            # children, so the staging path being fresh is fine.
            new_size = _bake_plugin_tree(
                type_=entity["type"],
                extracted_root=scratch,
                plugin_dir=staging_plugin,
                final_name=entity["name"],
                suffixed=suffixed,
                description=description if description is not None else entity.get("description"),
            )
            new_version = compute_entity_version(staging_plugin)

            inline_after_update = run_inline_checks(
                staging_plugin,
                type_=entity["type"],
                description=description if description is not None
                            else entity.get("description"),
            )
            if not inline_after_update.passed:
                # Compute the rejected bundle's sha+size BEFORE the
                # staging dir is removed so the submission row carries
                # forensics about what was tried.
                from src.store_guardrails.bundle_meta import compute_bundle_meta
                rejected_meta = compute_bundle_meta(staging_plugin)
                # Live tree was never touched — no rollback work to do.
                # The `finally` below cleans the staging dir.
                subs_repo = StoreSubmissionsRepository(conn)
                sub_id = subs_repo.create(
                    submitter_id=user["id"],
                    submitter_email=user.get("email"),
                    type=entity["type"],
                    name=entity["name"],
                    version=new_version,
                    status="blocked_inline",
                    entity_id=entity_id,
                    inline_checks=inline_after_update.to_response_dict(),
                    file_size=rejected_meta.file_size,
                    bundle_sha256=rejected_meta.sha256,
                )
                _audit(
                    conn, user["id"], "store.submission.blocked_inline",
                    sub_id, {"entity_id": entity_id, "on": "update",
                             "sha256": rejected_meta.sha256,
                             "file_size": rejected_meta.file_size},
                )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "submission_blocked",
                        "submission_id": sub_id,
                        "entity_id": entity_id,
                        "checks": inline_after_update.to_response_dict(),
                    },
                )

            # Checks passed — copy the new version's contents into the
            # live ``plugin/`` dir. The version dir at
            # ``versions/v<N+1>/plugin/`` keeps the canonical bundle
            # bytes for rollback; ``plugin/`` is the consumer-facing
            # snapshot of the current version. Three steps so a failed
            # copy doesn't leave the live dir in a half-state: rename
            # live → backup, copy version → live, rmtree backup.
            if existing_plugin.exists():
                backup_plugin = existing_plugin.with_name(
                    f"plugin.backup-{os.urandom(4).hex()}",
                )
                os.rename(existing_plugin, backup_plugin)
            try:
                shutil.copytree(staging_plugin, existing_plugin)
            except OSError:
                if backup_plugin is not None and backup_plugin.exists():
                    try:
                        if existing_plugin.exists():
                            shutil.rmtree(existing_plugin, ignore_errors=True)
                        os.rename(backup_plugin, existing_plugin)
                    except OSError:
                        pass
                raise
            if backup_plugin is not None:
                shutil.rmtree(backup_plugin, ignore_errors=True)
                backup_plugin = None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            # The version dir IS the source of truth — don't remove it
            # on failure paths. But if the bundle was rejected before
            # the swap (failed inline check raised 422), the version
            # dir is incomplete: the row hasn't been bumped yet, the
            # bytes are blocked-only forensics. Decision: keep the
            # version dir for the admin to download via the
            # submission's bundle download endpoint, but mark the
            # row's version_history WITHOUT this incomplete entry
            # (we never called append_version on the failed path).
            # The version dir at versions/v<N+1>/plugin/ stays orphan
            # on disk linked from the submission row's hash — admin
            # can still inspect.
            if backup_plugin is not None and backup_plugin.exists():
                logger.error(
                    "PUT version-swap left orphan backup at %s — "
                    "operator should reconcile manually",
                    backup_plugin,
                )

    photo_rel: Optional[str] = None
    if photo is not None:
        # Replace existing photo if present.
        existing_photo = entity.get("photo_path")
        if existing_photo:
            try:
                (_entity_dir(entity_id) / existing_photo).unlink(missing_ok=True)
            except OSError:
                pass
        photo_rel = await _save_photo(photo, entity_id)

    # Apply name change AFTER the bundle bake so the rename targets
    # the just-baked live tree. Reuses the rename-on-archive helper:
    # rename live skill/agent dir + rewrite frontmatter `name`.
    if rename_to is not None:
        owner_username = entity["owner_username"]
        old_suffix = suffixed_name(entity["name"], owner_username)
        new_suffix = suffixed_name(rename_to, owner_username)
        try:
            _rename_baked_tree(
                type_=entity["type"],
                plugin_dir=_plugin_dir(entity_id),
                old_suffix=old_suffix,
                new_suffix=new_suffix,
                description=description if description is not None else entity.get("description"),
            )
        except Exception:
            logger.exception(
                "rename_baked_tree failed during edit for entity %s",
                entity_id,
            )
            raise HTTPException(status_code=500, detail="rename_failed")
        # Apply the same rename to the version dir we just baked into
        # so its on-disk slug matches the new display name.
        if new_version_dir is not None:
            try:
                _rename_baked_tree(
                    type_=entity["type"],
                    plugin_dir=new_version_dir / "plugin",
                    old_suffix=old_suffix,
                    new_suffix=new_suffix,
                    description=description if description is not None else entity.get("description"),
                )
            except Exception:
                logger.exception(
                    "rename_baked_tree failed for version dir %s",
                    new_version_dir,
                )

    repo.update(
        entity_id,
        name=rename_to,
        description=description,
        category=category,
        version=new_version,
        photo_path=photo_rel,
        video_url=video_url,
        file_size=new_size,
    )

    # Re-run the LLM review whenever the bundle bytes changed. New
    # version = new submission row; visibility flips back to 'pending'
    # until the review approves the new tree. Description-only / photo-
    # only edits don't re-trigger. Append the version_history entry
    # AFTER the submission row exists so the entry can carry the
    # submission_id for forensic linkage.
    if file is not None and new_version_no is not None:
        guardrails_on = get_guardrails_enabled()
        if guardrails_on:
            repo.set_visibility(entity_id, "pending")
        subs_repo = StoreSubmissionsRepository(conn)
        from src.store_guardrails.bundle_meta import compute_bundle_meta
        accepted_meta = compute_bundle_meta(_plugin_dir(entity_id))
        sub_id = subs_repo.create(
            submitter_id=user["id"],
            submitter_email=user.get("email"),
            type=entity["type"],
            name=rename_to or entity["name"],
            version=new_version,
            status="approved" if not guardrails_on else "pending_llm",
            entity_id=entity_id,
            inline_checks=inline_after_update.to_response_dict()
                          if inline_after_update else None,
            file_size=accepted_meta.file_size,
            bundle_sha256=accepted_meta.sha256,
        )
        # Append the new version entry to version_history. Bumps
        # version_no on the entity row in the same UPDATE.
        repo.append_version(
            entity_id,
            version_hash=new_version,
            sha256=accepted_meta.sha256,
            size=accepted_meta.file_size,
            submission_id=sub_id,
            created_by=user["id"],
        )
        _audit(
            conn, user["id"],
            "store.submission.accepted" if guardrails_on else "store.submission.approved",
            sub_id, {"entity_id": entity_id, "on": "update",
                     "version_no": new_version_no,
                     "guardrails_enabled": guardrails_on},
        )
        if guardrails_on:
            _schedule_llm_review(background_tasks, sub_id, _plugin_dir(entity_id))

    _audit(
        conn,
        user["id"],
        "store.entity.update",
        entity_id,
        {"version": new_version, "version_no": new_version_no,
         "rebuilt": file is not None,
         "renamed_to": rename_to},
    )
    _invalidate_etag()
    return _entity_to_response(conn, repo.get(entity_id))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Restore — POST /api/store/entities/{id}/versions/{version_no}/restore
# ---------------------------------------------------------------------------


@router.post(
    "/entities/{entity_id}/versions/{version_no}/restore",
    response_model=StoreEntityResponse,
)
async def restore_version(
    entity_id: str,
    version_no: int,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Roll back to a prior version. Owner or admin.

    Creates a NEW version (`v<max+1>`) by copying the bundle bytes
    from `versions/v<N>/plugin/`, then runs the standard guardrails
    pipeline so today's rules apply (rules tighten over time —
    pre-approved bundles re-validate at restore time).

    The original `version_no` row in ``version_history`` keeps its
    own verdict; the new copy gets a fresh one. Forward-only history
    — no deletes from version_history.

    Refuses while a prior version is under review (same
    ``prior_version_pending`` 409 as PUT).
    """
    repo = StoreEntitiesRepository(conn)
    entity = repo.get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    if entity["owner_user_id"] != user["id"] and not is_user_admin(user["id"], conn):
        raise HTTPException(status_code=403, detail="not_owner")

    # Block while pending — same gate as PUT.
    if entity.get("visibility_status") == "pending":
        latest_sub = StoreSubmissionsRepository(conn).latest_for_entity(entity_id)
        if latest_sub and latest_sub.get("status") in (
            "pending_inline", "pending_llm",
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "prior_version_pending",
                    "message": "A previous edit is still under review. "
                               "Wait for the verdict before restoring.",
                    "submission_id": latest_sub.get("id"),
                },
            )

    # Locate the source version dir.
    source_dir = (
        _entity_dir(entity_id) / "versions" / f"v{version_no}" / "plugin"
    )
    if not source_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail={
                "code": "version_not_found",
                "version_no": version_no,
            },
        )
    if int(version_no) == int(entity.get("version_no") or 1):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "already_current",
                "version_no": version_no,
            },
        )

    # Copy source → new version dir, run guardrails, swap live.
    new_version_no = int(entity.get("version_no") or 1) + 1
    target_root = _entity_dir(entity_id) / "versions" / f"v{new_version_no}"
    target_plugin = target_root / "plugin"
    if target_plugin.exists():
        shutil.rmtree(target_plugin, ignore_errors=True)
    target_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, target_plugin)

    new_version = compute_entity_version(target_plugin)
    inline = run_inline_checks(
        target_plugin,
        type_=entity["type"],
        description=entity.get("description"),
    )
    if not inline.passed:
        from src.store_guardrails.bundle_meta import compute_bundle_meta
        rejected_meta = compute_bundle_meta(target_plugin)
        subs_repo = StoreSubmissionsRepository(conn)
        sub_id = subs_repo.create(
            submitter_id=user["id"],
            submitter_email=user.get("email"),
            type=entity["type"],
            name=entity["name"],
            version=new_version,
            status="blocked_inline",
            entity_id=entity_id,
            inline_checks=inline.to_response_dict(),
            file_size=rejected_meta.file_size,
            bundle_sha256=rejected_meta.sha256,
        )
        _audit(
            conn, user["id"], "store.submission.blocked_inline",
            sub_id,
            {"entity_id": entity_id, "on": "restore",
             "restored_from_version_no": version_no,
             "sha256": rejected_meta.sha256,
             "file_size": rejected_meta.file_size},
        )
        # Live tree untouched. Version dir kept for forensic download.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "submission_blocked",
                "submission_id": sub_id,
                "entity_id": entity_id,
                "checks": inline.to_response_dict(),
            },
        )

    # Inline checks passed. Swap live.
    existing_plugin = _plugin_dir(entity_id)
    backup_plugin: Optional[Path] = None
    if existing_plugin.exists():
        backup_plugin = existing_plugin.with_name(
            f"plugin.backup-{os.urandom(4).hex()}",
        )
        os.rename(existing_plugin, backup_plugin)
    try:
        shutil.copytree(target_plugin, existing_plugin)
    except OSError:
        if backup_plugin is not None and backup_plugin.exists():
            try:
                if existing_plugin.exists():
                    shutil.rmtree(existing_plugin, ignore_errors=True)
                os.rename(backup_plugin, existing_plugin)
            except OSError:
                pass
        raise
    if backup_plugin is not None:
        shutil.rmtree(backup_plugin, ignore_errors=True)

    # Update DB row + history. LLM review queued if guardrails on.
    guardrails_on = get_guardrails_enabled()
    if guardrails_on:
        repo.set_visibility(entity_id, "pending")
    from src.store_guardrails.bundle_meta import compute_bundle_meta
    accepted_meta = compute_bundle_meta(_plugin_dir(entity_id))
    subs_repo = StoreSubmissionsRepository(conn)
    sub_id = subs_repo.create(
        submitter_id=user["id"],
        submitter_email=user.get("email"),
        type=entity["type"],
        name=entity["name"],
        version=new_version,
        status="approved" if not guardrails_on else "pending_llm",
        entity_id=entity_id,
        inline_checks=inline.to_response_dict(),
        file_size=accepted_meta.file_size,
        bundle_sha256=accepted_meta.sha256,
    )
    repo.update(entity_id, version=new_version, file_size=accepted_meta.file_size)
    repo.append_version(
        entity_id,
        version_hash=new_version,
        sha256=accepted_meta.sha256,
        size=accepted_meta.file_size,
        submission_id=sub_id,
        created_by=user["id"],
    )
    _audit(
        conn, user["id"], "store.entity.restore", entity_id,
        {"restored_from_version_no": version_no,
         "new_version_no": new_version_no,
         "submission_id": sub_id},
    )
    if guardrails_on:
        _schedule_llm_review(background_tasks, sub_id, _plugin_dir(entity_id))

    _invalidate_etag()
    return _entity_to_response(conn, repo.get(entity_id))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Delete — DELETE /api/store/entities/{id}
# ---------------------------------------------------------------------------


@router.delete("/entities/{entity_id}", response_model=OkResponse)
async def delete_entity(
    entity_id: str,
    hard: bool = False,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Soft-archive (default) or hard-delete (admin-only).

    * **Soft (default)** — flips `visibility_status='archived'`. Bundle
      stays on disk; existing user_store_installs continue serving the
      bundle through marketplace.zip / .git so already-installed users
      don't lose the plugin. Browse listings hide it; install endpoint
      refuses new installs. Owner + admin can soft-archive an
      approved entity.
    * **Hard (`?hard=true`)** — admin-only. Drops the row, removes the
      bundle from disk, deletes user_store_installs (existing users
      lose the plugin). Use for legal / privacy removals where the
      bytes have to go.

    Quarantined (pending / blocked / hidden) entities: only admins can
    archive or hard-delete; owner is refused so they can't erase the
    evidence of a flagged upload before triage.
    """
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    is_admin_caller = is_user_admin(user["id"], conn)
    if entity["owner_user_id"] != user["id"] and not is_admin_caller:
        raise HTTPException(status_code=403, detail="not_owner")

    # Hard delete is admin-only. Owners (or admins without ?hard=true)
    # take the soft archive path below.
    if hard and not is_admin_caller:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "hard_delete_admin_only",
                "hint": "Hard delete is admin-only — it drops the bundle "
                        "from disk and removes existing installs. Use the "
                        "default Archive button to soft-delete (keeps "
                        "existing installs working).",
            },
        )

    # Quarantined (non-approved + non-archived): owner can't touch.
    # Admin can either Archive (soft) or Hard Delete from here.
    if (
        entity.get("visibility_status") not in ("approved", "archived")
        and not is_admin_caller
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "quarantined_owner_cannot_delete",
                "hint": "This submission is under quarantine while admins "
                        "review it. Edit and re-upload to fix the issues, "
                        "or wait for an admin to resolve the quarantine.",
            },
        )

    if hard:
        # Mark linked submissions before dropping the entity row so
        # mark_deleted_for_entity can find them by entity_id.
        StoreSubmissionsRepository(conn).mark_deleted_for_entity(entity_id)
        UserStoreInstallsRepository(conn).delete_all_for_entity(entity_id)
        StoreEntitiesRepository(conn).delete(entity_id)
        shutil.rmtree(_entity_dir(entity_id), ignore_errors=True)
        _audit(
            conn,
            user["id"],
            "store.entity.hard_delete",
            entity_id,
            {"name": entity.get("name"),
             "owner_user_id": entity.get("owner_user_id")},
        )
        _invalidate_etag()
        return OkResponse()

    # Soft archive — preserves disk + installs + audit chain.
    # v36+: archive renames the entity row's `name` (appends
    # `__archived__<epoch>`) so the (owner, name) UNIQUE slot AND
    # the global `<name>-by-<owner_username>` slug slot free up for
    # re-upload. The on-disk skill/agent/plugin subdir is renamed
    # in lockstep + frontmatter rewritten so consumers see the
    # plugin under the new slug on their next sync.
    rename_info = StoreEntitiesRepository(conn).archive(
        entity_id, by_user_id=user["id"],
    )
    original_name = rename_info["original_name"]
    new_name = rename_info["new_name"]
    if original_name and new_name and original_name != new_name:
        owner_username = entity.get("owner_username") or ""
        old_suffix = suffixed_name(original_name, owner_username)
        new_suffix = suffixed_name(new_name, owner_username)
        try:
            _rename_baked_tree(
                type_=entity["type"],
                plugin_dir=_plugin_dir(entity_id),
                old_suffix=old_suffix,
                new_suffix=new_suffix,
                description=entity.get("description"),
            )
        except Exception:
            # On-disk rename failure leaves the row pointing at a
            # stale slug. Revert the DB row so the system stays
            # consistent (operator can retry archive).
            logger.exception(
                "archive on-disk rename failed for entity %s — "
                "reverting DB",
                entity_id,
            )
            conn.execute(
                """UPDATE store_entities
                      SET visibility_status = 'approved',
                          name = ?,
                          archived_at = NULL,
                          archived_by = NULL,
                          updated_at = ?
                    WHERE id = ?""",
                [original_name, datetime.now(timezone.utc), entity_id],
            )
            raise HTTPException(
                status_code=500, detail="archive_rename_failed",
            )
    _audit(
        conn,
        user["id"],
        "store.entity.archive",
        entity_id,
        {"name": new_name,
         "original_name": original_name,
         "owner_user_id": entity.get("owner_user_id"),
         "by_admin": is_admin_caller and entity["owner_user_id"] != user["id"]},
    )
    _invalidate_etag()
    return OkResponse()


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


@router.post("/entities/{entity_id}/install", response_model=InstallResponse)
async def install_entity(
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = StoreEntitiesRepository(conn)
    entity = repo.get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    # Block installs against entities still in guardrail review or that
    # have been hidden — they are not visible in the public flea browse,
    # but a user with the entity_id in hand could otherwise install
    # directly. Owner installing their own pending entity gets the same
    # 409 — they preview before publishing via /api/store/entities/preview
    # or wait for approval.
    if entity.get("visibility_status") != "approved" and not is_user_admin(user["id"], conn):
        raise HTTPException(status_code=409, detail="entity_not_approved")
    installs = UserStoreInstallsRepository(conn)
    inserted = installs.install(user["id"], entity_id)
    if inserted:
        repo.bump_install_count(entity_id, +1)
        _audit(conn, user["id"], "store.entity.install", entity_id)
        _invalidate_etag()
    return InstallResponse(entity_id=entity_id, installed=True)


@router.delete("/entities/{entity_id}/install", response_model=InstallResponse)
async def uninstall_entity(
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    installs = UserStoreInstallsRepository(conn)
    deleted = installs.uninstall(user["id"], entity_id)
    if deleted:
        StoreEntitiesRepository(conn).bump_install_count(entity_id, -1)
        _audit(conn, user["id"], "store.entity.uninstall", entity_id)
        _invalidate_etag()
    return InstallResponse(entity_id=entity_id, installed=False)


# ---------------------------------------------------------------------------
# Bundle: GET /api/store/bundle.zip + POST /api/store/import-bundle
# ---------------------------------------------------------------------------
#
# Whole-Store backup/restore primitive. Operationally consumed by the
# `agnes admin store {pull,push}` CLI commands which back up the Store to a
# git repo (or restore from one). Bundle format:
#
#     agnes-store-bundle.zip
#     ├── manifest.json                 ← {"format":1,"generated_at":..., "entries":[...]}
#     └── entities/<entity_id>/
#         ├── plugin/...                ← canonical Claude Code plugin tree
#         └── assets/...                ← photo + docs
#
# Each manifest entry carries `owner_email` (resolved at export time from the
# users table) — when `import-bundle` lands on a different Agnes instance,
# the importer matches by email rather than by `owner_user_id` (the latter
# is per-instance and won't match). If the email is unknown on the target,
# we create a stub user (active=False, password_hash=NULL) so the historical
# owner is preserved; an admin can later activate or reassign.
#
# Bundle ordering is deterministic (entries sorted by entity_id, files within
# each entity sorted by relpath, fixed mtime) so that diffs of two
# successive snapshots stay clean when committed to git.

BUNDLE_FORMAT_VERSION = 1
BUNDLE_DETERMINISTIC_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class BundleEntry(BaseModel):
    entity_id: str
    type: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    version: str
    owner_user_id: str
    owner_email: Optional[str] = None
    owner_username: str
    install_count: int = 0
    file_size: int = 0
    photo_path: Optional[str] = None
    video_url: Optional[str] = None
    doc_paths: List[str] = []
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BundleManifest(BaseModel):
    format: int = BUNDLE_FORMAT_VERSION
    generated_at: str
    entry_count: int
    entries: List[BundleEntry]


class ImportBundleResponse(BaseModel):
    imported: int
    replaced: int
    skipped: int
    stub_users_created: int
    errors: List[dict] = []


def _resolve_owner_emails(
    conn: duckdb.DuckDBPyConnection, owner_ids: List[str]
) -> dict:
    """Bulk-fetch user_id → email map for the given owners.

    Empty list short-circuits to {} so the caller doesn't need a guard.
    Missing rows are simply absent from the returned dict — the caller
    falls back to the row's stored ``owner_username`` for diagnostics.
    """
    if not owner_ids:
        return {}
    placeholders = ",".join(["?"] * len(owner_ids))
    rows = conn.execute(
        f"SELECT id, email FROM users WHERE id IN ({placeholders})",
        list(owner_ids),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _walk_entity_files(entity_id: str) -> List[tuple[str, Path]]:
    """Return [(arcname, abs_path)] for every file under
    ``${DATA_DIR}/store/<entity_id>/`` that should land in the bundle.

    Both ``plugin/`` and ``assets/`` subtrees are included. Output is
    sorted by arcname so the resulting ZIP is byte-deterministic.
    """
    out: list[tuple[str, Path]] = []
    root = _entity_dir(entity_id)
    if not root.is_dir():
        return out
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = f.relative_to(root).as_posix()
        # Only ship plugin/ and assets/ subtrees — anything else under the
        # entity dir is internal scratch and shouldn't enter the bundle.
        first = rel.split("/", 1)[0] if "/" in rel else rel
        if first not in ("plugin", "assets"):
            continue
        arc = f"entities/{entity_id}/{rel}"
        out.append((arc, f))
    return sorted(out, key=lambda t: t[0])


def _build_bundle_zip(
    conn: duckdb.DuckDBPyConnection,
    entries: List[dict],
) -> bytes:
    """Build the deterministic ZIP from a list of store_entities rows.

    Entries arrive already filtered (per the caller's query). We resolve
    owner_email in one bulk roundtrip to keep the export path off the
    O(N) per-row query path.
    """
    owner_emails = _resolve_owner_emails(
        conn, list({e["owner_user_id"] for e in entries})
    )
    bundle_entries: List[dict] = []
    for e in sorted(entries, key=lambda r: r["id"]):
        bundle_entries.append(
            {
                "entity_id": e["id"],
                "type": e["type"],
                "name": e["name"],
                "description": e.get("description"),
                "category": e.get("category"),
                "version": e["version"],
                "owner_user_id": e["owner_user_id"],
                "owner_email": owner_emails.get(e["owner_user_id"]),
                "owner_username": e["owner_username"],
                "install_count": int(e.get("install_count") or 0),
                "file_size": int(e.get("file_size") or 0),
                "photo_path": e.get("photo_path"),
                "video_url": e.get("video_url"),
                "doc_paths": e.get("doc_paths") or [],
                "created_at": _to_iso(e.get("created_at")),
                "updated_at": _to_iso(e.get("updated_at")),
            }
        )

    manifest = {
        "format": BUNDLE_FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry_count": len(bundle_entries),
        "entries": bundle_entries,
    }

    members: list[tuple[str, bytes]] = [
        ("manifest.json", json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8"))
    ]
    for entry in bundle_entries:
        for arc, abs_path in _walk_entity_files(entry["entity_id"]):
            members.append((arc, abs_path.read_bytes()))
    members.sort(key=lambda m: m[0])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, data in members:
            info = zipfile.ZipInfo(filename=arc, date_time=BUNDLE_DETERMINISTIC_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


@router.get("/bundle.zip")
async def export_bundle(
    type: Optional[str] = Query(None, description="skill | agent | plugin"),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    owner: Optional[str] = Query(None, description="Filter by owner user_id"),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream a ZIP of all (filtered) Store entities.

    Auth: any authenticated user — the Store is community-open, the same
    set is already visible via ``GET /api/store/entities``. The bundle is
    deterministic so two consecutive pulls without state changes produce
    byte-identical ZIPs (modulo the manifest's ``generated_at`` timestamp).
    Filters mirror the listing endpoint so a backup workflow can scope by
    type/owner if needed.
    """
    if type and type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="invalid_type")
    # `owner=me` magic value resolves to the caller's user id — used by
    # `agnes store mine` so analysts can pull a bundle of just their own
    # uploads without needing to look up their own user_id first.
    if owner == "me":
        owner = user["id"]
    repo = StoreEntitiesRepository(conn)
    # Page through everything. The 100/req limit on `list` is a UI
    # pagination affordance, not a backup constraint — for a bulk export
    # we want all matches.
    items: list[dict] = []
    skip = 0
    page = 200
    while True:
        page_items, _total = repo.list(
            skip=skip, limit=page, type=type, category=category,
            search=search, owner_user_id=owner,
        )
        if not page_items:
            break
        items.extend(page_items)
        if len(page_items) < page:
            break
        skip += page

    payload = _build_bundle_zip(conn, items)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="agnes-store-bundle.zip"',
            "X-Bundle-Entry-Count": str(len(items)),
        },
    )


def _import_one_entry(
    conn: duckdb.DuckDBPyConnection,
    entry: dict,
    extract_root: Path,
    *,
    mode: str,
    actor_user_id: str,
) -> tuple[str, int]:
    """Apply a single manifest entry. Returns ``(outcome, stub_users_created)``
    where outcome is one of ``imported``, ``replaced``, ``skipped``.

    Owner resolution: we match the bundle's ``owner_email`` against
    ``users.email``. Missing → create a stub (active=False, no password)
    so the historical owner stays attached; an admin can activate or
    reassign in /admin/users. The stub gets ``id = "imported-" +
    sha256(email)[:12]`` to make it idempotent across repeated imports.
    """
    entity_id = entry["entity_id"]
    repo = StoreEntitiesRepository(conn)
    existing = repo.get(entity_id)

    if existing:
        if mode == "skip":
            return ("skipped", 0)
        if mode == "merge":
            # Keep newer version (content-hash). If equal, skip.
            if (existing.get("version") or "") == (entry.get("version") or ""):
                return ("skipped", 0)
        # mode='replace' OR mode='merge' with newer version → fall through.

    # Resolve owner.
    user_repo = UserRepository(conn)
    owner_email = (entry.get("owner_email") or "").strip().lower()
    stub_created = 0
    owner_user_id: Optional[str] = None
    if owner_email:
        existing_user = user_repo.get_by_email(owner_email)
        if existing_user:
            owner_user_id = existing_user["id"]
        else:
            import hashlib as _hl
            stub_id = "imported-" + _hl.sha256(owner_email.encode("utf-8")).hexdigest()[:12]
            if not user_repo.get_by_id(stub_id):
                user_repo.create(
                    id=stub_id, email=owner_email, name=owner_email,
                    password_hash=None,
                )
                user_repo.update(stub_id, active=False)
                stub_created = 1
            owner_user_id = stub_id
    if owner_user_id is None:
        # Fallback: use the importer (admin) so the row has a valid owner.
        owner_user_id = actor_user_id

    # Materialize files.
    src_dir = extract_root / "entities" / entity_id
    if not src_dir.is_dir():
        raise HTTPException(
            status_code=422,
            detail=f"manifest entry {entity_id!r} has no entities/<id>/ directory in the bundle",
        )
    target_dir = _entity_dir(entity_id)
    if existing and target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for f in src_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(src_dir)
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)

    # Upsert DB row.
    if existing:
        repo.update(
            entity_id,
            description=entry.get("description"),
            category=entry.get("category"),
            version=entry["version"],
            photo_path=entry.get("photo_path"),
            video_url=entry.get("video_url"),
            doc_paths=entry.get("doc_paths") or [],
            file_size=int(entry.get("file_size") or 0),
        )
        return ("replaced", stub_created)

    repo.create(
        id=entity_id,
        owner_user_id=owner_user_id,
        owner_username=entry.get("owner_username") or owner_email.split("@")[0],
        type=entry["type"],
        name=entry["name"],
        description=entry.get("description"),
        category=entry.get("category"),
        version=entry["version"],
        photo_path=entry.get("photo_path"),
        video_url=entry.get("video_url"),
        doc_paths=entry.get("doc_paths") or [],
        file_size=int(entry.get("file_size") or 0),
    )
    return ("imported", stub_created)


@router.post("/import-bundle", response_model=ImportBundleResponse)
async def import_bundle(
    file: UploadFile = File(...),
    mode: str = Form("merge"),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Restore a Store bundle ZIP — admin only.

    Modes:
      * ``merge`` (default) — upsert by ``entity_id``; existing entities
        are replaced when the bundle's ``version`` differs, otherwise
        skipped. Safe default for nightly cron round-trips.
      * ``replace`` — every entity in the bundle overwrites the existing
        row + on-disk tree. Bundle-not-in-target rows are NOT deleted.
      * ``skip`` — only entities NOT already present are imported.

    Owner resolution by ``owner_email``; missing emails get a stub
    disabled user so the row references an existing ``users.id`` (no
    foreign key, but app code joins).
    """
    if mode not in {"merge", "replace", "skip"}:
        raise HTTPException(status_code=400, detail="invalid_mode")

    tmp, _ = await _stream_to_temp(file, MAX_ZIP_SIZE * 4, suffix=".zip")
    tmp.close()
    extract_root = Path(tempfile.mkdtemp(prefix="agnes_store_import_"))
    try:
        try:
            with zipfile.ZipFile(tmp.name, "r") as zf:
                _safe_zip_extract(zf, extract_root)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=422, detail="zip_invalid")
        finally:
            Path(tmp.name).unlink(missing_ok=True)

        manifest_path = extract_root / "manifest.json"
        if not manifest_path.is_file():
            raise HTTPException(status_code=422, detail="manifest_missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(status_code=422, detail="manifest_invalid")
        if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT_VERSION:
            raise HTTPException(
                status_code=422,
                detail=f"manifest_unsupported_format (expected {BUNDLE_FORMAT_VERSION})",
            )
        entries = manifest.get("entries") or []
        if not isinstance(entries, list):
            raise HTTPException(status_code=422, detail="manifest_entries_invalid")

        imported = replaced = skipped = stubs = 0
        errors: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("entity_id"):
                errors.append({"entry": entry, "error": "entry_missing_id"})
                continue
            try:
                outcome, sc = _import_one_entry(
                    conn, entry, extract_root, mode=mode, actor_user_id=user["id"],
                )
            except HTTPException:
                raise
            except Exception as exc:
                errors.append({"entity_id": entry.get("entity_id"), "error": str(exc)})
                continue
            stubs += sc
            if outcome == "imported":
                imported += 1
            elif outcome == "replaced":
                replaced += 1
            elif outcome == "skipped":
                skipped += 1

        _audit(
            conn, user["id"], "store.bundle.import", "bundle",
            {
                "mode": mode,
                "imported": imported,
                "replaced": replaced,
                "skipped": skipped,
                "stub_users_created": stubs,
                "errors": len(errors),
            },
        )
        _invalidate_etag()
        return ImportBundleResponse(
            imported=imported, replaced=replaced, skipped=skipped,
            stub_users_created=stubs, errors=errors,
        )
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Photo upload helper
# ---------------------------------------------------------------------------


async def _save_docs(docs: List[UploadFile], entity_id: str) -> List[str]:
    """Save user-uploaded auxiliary docs into ``assets/docs/``.

    Filenames are sanitized (basename only — strips any directory
    component sent by the browser). On collision the file is suffixed
    with a counter so two ``readme.md`` uploads don't overwrite each
    other.
    """
    if not docs:
        return []
    docs_dir = _assets_dir(entity_id) / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    saved: List[str] = []
    for upload in docs:
        if upload is None or not upload.filename:
            continue
        safe_name = Path(upload.filename).name
        if not safe_name or safe_name.startswith("."):
            continue
        target = _unique_doc_path(docs_dir, safe_name)
        tmp, _ = await _stream_to_temp(
            upload, MAX_DOC_SIZE, suffix=Path(safe_name).suffix or ".tmp",
        )
        try:
            tmp.close()
            shutil.move(tmp.name, str(target))
        except Exception:
            Path(tmp.name).unlink(missing_ok=True)
            raise
        saved.append(f"assets/docs/{target.name}")
    return saved


def _unique_doc_path(docs_dir: Path, filename: str) -> Path:
    target = docs_dir / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    i = 2
    while True:
        candidate = docs_dir / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


async def _save_photo(photo: UploadFile, entity_id: str) -> str:
    """Save the uploaded photo into the entity's assets dir. Returns the path
    relative to the entity dir (what gets stored on the row).
    """
    raw_name = photo.filename or "photo"
    ext = Path(raw_name).suffix.lower()
    if ext not in ALLOWED_PHOTO_EXT:
        raise HTTPException(status_code=415, detail="photo_unsupported_format")
    tmp, size = await _stream_to_temp(photo, MAX_PHOTO_SIZE, suffix=ext)
    try:
        tmp.close()
        assets = _assets_dir(entity_id)
        assets.mkdir(parents=True, exist_ok=True)
        target_name = f"photo{ext}"
        target = assets / target_name
        # Replace any pre-existing photo of a different ext.
        for existing in assets.glob("photo.*"):
            try:
                existing.unlink()
            except OSError:
                pass
        shutil.move(tmp.name, str(target))
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    return f"assets/{target_name}"


# ---------------------------------------------------------------------------
# ETag invalidation — pulled into a module-level helper so we can mock it in
# tests if needed.
# ---------------------------------------------------------------------------


def _invalidate_etag() -> None:
    try:
        from app.marketplace_server import packager
        packager.invalidate_etag_cache()
    except Exception:
        logger.exception("failed to invalidate marketplace etag cache")
