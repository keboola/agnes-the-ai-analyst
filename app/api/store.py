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
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, get_current_user
from app.utils import get_store_dir
from src.repositories.audit import AuditRepository
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.user_store_installs import UserStoreInstallsRepository
from src.store_categories import STORE_CATEGORIES, is_valid_category
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
    """
    sql = (
        "SELECT id FROM store_entities "
        "WHERE name || '-by-' || owner_username = ?"
    )
    params: List[Any] = [suffixed]
    if exclude_entity_id:
        sql += " AND id != ?"
        params.append(exclude_entity_id)
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
    """
    rows = conn.execute(
        """SELECT
               se.owner_user_id,
               COALESCE(NULLIF(TRIM(u.name), ''), u.email, se.owner_username) AS display_name,
               COUNT(*) AS entity_count
           FROM store_entities se
           LEFT JOIN users u ON u.id = se.owner_user_id
           GROUP BY se.owner_user_id, display_name
           ORDER BY display_name"""
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
    items, total = repo.list(
        skip=skip,
        limit=limit,
        type=type,
        category=category,
        search=search,
        owner_user_id=owner,
    )
    return StoreEntityListResponse(
        items=[_entity_to_response(conn, e) for e in items],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/entities/{entity_id}", response_model=StoreEntityResponse)
async def get_entity(
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
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

    # Stream + extract ZIP into a scratch dir.
    tmp, size = await _stream_to_temp(file, MAX_ZIP_SIZE, suffix=".zip")
    try:
        tmp.close()
        scratch = Path(tempfile.mkdtemp(prefix="agnes_store_"))
        try:
            with zipfile.ZipFile(tmp.name, "r") as zf:
                _safe_zip_extract(zf, scratch)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=422, detail="zip_invalid")
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    try:
        meta = _validate_and_extract_metadata(type, scratch)
        final_name = (name or meta.get("name") or "").strip()
        if not final_name:
            raise HTTPException(status_code=400, detail="missing_name")
        if not _NAME_RE.match(final_name):
            raise HTTPException(status_code=400, detail="invalid_name_format")
        final_description = description or meta.get("description")

        repo = StoreEntitiesRepository(conn)
        if repo.get_by_owner_and_name(user["id"], final_name):
            raise HTTPException(status_code=409, detail="conflict_owner_name")

        entity_id = uuid.uuid4().hex
        suffixed = suffixed_name(final_name, username)
        # Global cross-owner check — sanitize_username is many-to-one, so
        # two emails (alice.smith / alice_smith) can resolve to the same
        # username and produce the same `<name>-by-<username>` suffix even
        # when the per-owner UNIQUE passes. The suffixed value drives both
        # the bundle on-disk dir and the served plugin.json `name`, so a
        # collision silently last-write-wins. Refuse upfront.
        if _suffixed_already_taken(conn, suffixed):
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
        )
        _audit(
            conn,
            user["id"],
            "store.entity.create",
            entity_id,
            {"type": type, "name": final_name, "version": version, "size": file_size},
        )
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
    file: Optional[UploadFile] = File(None),
    description: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    video_url: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = StoreEntitiesRepository(conn)
    entity = repo.get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    if entity["owner_user_id"] != user["id"] and not is_user_admin(user["id"], conn):
        raise HTTPException(status_code=403, detail="not_owner")

    if category and not is_valid_category(category):
        raise HTTPException(status_code=400, detail="invalid_category")

    video_url = _validate_video_url(video_url)

    new_version: Optional[str] = None
    new_size: Optional[int] = None
    if file is not None:
        tmp, size = await _stream_to_temp(file, MAX_ZIP_SIZE, suffix=".zip")
        try:
            tmp.close()
            scratch = Path(tempfile.mkdtemp(prefix="agnes_store_"))
            try:
                with zipfile.ZipFile(tmp.name, "r") as zf:
                    _safe_zip_extract(zf, scratch)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=422, detail="zip_invalid")
        finally:
            Path(tmp.name).unlink(missing_ok=True)

        try:
            _validate_and_extract_metadata(entity["type"], scratch)
            suffixed = suffixed_name(entity["name"], entity["owner_username"])
            new_size = _bake_plugin_tree(
                type_=entity["type"],
                extracted_root=scratch,
                plugin_dir=_plugin_dir(entity_id),
                final_name=entity["name"],
                suffixed=suffixed,
                description=description if description is not None else entity.get("description"),
            )
            new_version = compute_entity_version(_plugin_dir(entity_id))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

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

    repo.update(
        entity_id,
        description=description,
        category=category,
        version=new_version,
        photo_path=photo_rel,
        video_url=video_url,
        file_size=new_size,
    )
    _audit(
        conn,
        user["id"],
        "store.entity.update",
        entity_id,
        {"version": new_version, "rebuilt": file is not None},
    )
    _invalidate_etag()
    return _entity_to_response(conn, repo.get(entity_id))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Delete — DELETE /api/store/entities/{id}
# ---------------------------------------------------------------------------


@router.delete("/entities/{entity_id}", response_model=OkResponse)
async def delete_entity(
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    if entity["owner_user_id"] != user["id"] and not is_user_admin(user["id"], conn):
        raise HTTPException(status_code=403, detail="not_owner")

    UserStoreInstallsRepository(conn).delete_all_for_entity(entity_id)
    StoreEntitiesRepository(conn).delete(entity_id)
    shutil.rmtree(_entity_dir(entity_id), ignore_errors=True)
    _audit(
        conn,
        user["id"],
        "store.entity.delete",
        entity_id,
        {"name": entity.get("name")},
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
