"""Admin endpoints — table discovery, registry management, instance configuration.

v9: every gate on this router uses ``require_role(Role.ADMIN)``, which routes
through ``require_internal_role("core.admin")`` — the same two-path resolver
(session cache + ``user_role_grants`` fallback) covers OAuth and PAT callers
alike. Module authors implementing their own routers should follow the same
pattern: register a capability with ``register_internal_role(...)`` at import
time and gate the route with ``Depends(require_internal_role("<your-key>"))``.
See ``docs/RBAC.md`` for the full module-author workflow.
"""

import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import duckdb

from app.auth.dependencies import require_role, Role, _get_db
from src.repositories.table_registry import TableRegistryRepository
from src.repositories.audit import AuditRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- Server-config (instance.yaml) editor -----------------------------------
#
# The /admin/server-config UI POSTs a partial dict here keyed by section
# (instance, data_source, email, telegram, jira, theme, server, auth) with
# the field values to merge into instance.yaml. Each save:
#   1. Loads the current instance.yaml (writable overlay first, then static).
#   2. Deep-merges the patch on top.
#   3. Writes to DATA_DIR/state/instance.yaml (the writable overlay).
#   4. Writes one audit_log entry tagged `instance_config.update` containing
#      a sanitized diff (secret-looking keys are masked).
# Hot-reload is OUT OF SCOPE for #91 — the response carries
# `restart_required=True` so the UI can show the banner.

# Sections an admin can mutate. Keep the list explicit so a typo'd section
# in the request body is rejected loudly instead of being silently merged
# into the YAML root and confusing future loads.
_EDITABLE_SECTIONS: tuple[str, ...] = (
    "instance",
    "data_source",
    "email",
    "telegram",
    "jira",
    "theme",
    "server",
    "auth",
)

# "Danger-zone" sections — flipping these can lock operators out (auth.*) or
# break OAuth callbacks (server.hostname/host). The UI shows a confirmation
# dialog before submitting them. The API accepts them; this list exists so
# the audit entry can flag the change as high-risk and the UI can surface
# the right warning copy.
_DANGER_SECTIONS: tuple[str, ...] = ("auth", "server")

# Keys whose values must be redacted from the audit diff. We match
# substring (case-insensitive) so `client_secret`, `api_token`,
# `webapp_secret_key`, `bot_token`, `password`, `smtp_password`, etc. all
# get masked even when nested.
_SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "api_key",
)


def _is_secret_key(key: str) -> bool:
    """True if a config key holds a credential and should be masked in audit logs."""
    k = key.lower()
    return any(pat in k for pat in _SECRET_KEY_PATTERNS)


def _mask(value: Any) -> str:
    """Replacement value used in the audit diff for secret fields.

    We deliberately do NOT preserve length or any hint about the secret —
    the diff is read by other admins, and there's no operator value to
    leaking "the new SMTP password is 16 chars". `***` is enough to show
    that the field changed without exposing it.
    """
    if value in (None, ""):
        return "<empty>"
    return "***"


def _redact(value: Any, key_hint: str = "") -> Any:
    """Recursively mask secret-looking fields in a config subtree.

    `key_hint` is the parent key — used so a string value like
    ``"${KEBOOLA_TOKEN}"`` under ``token_env`` is masked even though the
    value itself isn't a credential, because the key signals it points at
    one.
    """
    if isinstance(value, dict):
        return {k: (_mask(v) if _is_secret_key(k) else _redact(v, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key_hint) for item in value]
    if key_hint and _is_secret_key(key_hint):
        return _mask(value)
    return value


def _diff_dicts(before: dict, after: dict, path: str = "") -> List[Dict[str, Any]]:
    """Flat list of changed fields between two dicts.

    Output: [{"path": "email.smtp_host", "before": "...", "after": "..."}].
    Secret-looking keys are pre-masked via _redact at call time, so the
    diff entries are always safe to persist in audit_log.

    Recurses into a dict on either side (treating the missing side as
    `{}`) so adding a brand-new section reports per-field paths
    (`email.smtp_host`) rather than a single opaque `email` blob — that
    keeps the audit row useful when an admin populates a section for the
    first time.
    """
    changes: List[Dict[str, Any]] = []
    keys = set(before.keys()) | set(after.keys())
    for key in sorted(keys):
        new_path = f"{path}.{key}" if path else key
        b_val = before.get(key)
        a_val = after.get(key)
        b_is_dict = isinstance(b_val, dict)
        a_is_dict = isinstance(a_val, dict)
        if b_is_dict or a_is_dict:
            # Recurse with missing side coerced to {} so per-field paths
            # are emitted for dict-vs-None and dict-vs-dict.
            changes.extend(_diff_dicts(
                b_val if b_is_dict else {},
                a_val if a_is_dict else {},
                new_path,
            ))
        elif b_val != a_val:
            changes.append({"path": new_path, "before": b_val, "after": a_val})
    return changes


def _deep_merge(base: dict, patch: dict) -> dict:
    """Merge `patch` into `base` recursively, returning a new dict.

    Patch values overwrite base values. Dict-into-dict recurses; everything
    else (lists, scalars, None) is replaced wholesale — admin sets
    ``email: {smtp_port: 465}`` and we don't try to re-merge nested ports.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_current_instance_yaml() -> dict:
    """Load instance.yaml the same way /api/admin/configure does.

    Writable overlay (DATA_DIR/state/instance.yaml) wins; static config
    (CONFIG_DIR/instance.yaml) is the fallback. Returns {} if neither
    exists or both fail to parse — the editor still needs to render an
    empty form so the operator can populate it from scratch.
    """
    import yaml
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    overlay = data_dir / "state" / "instance.yaml"
    if overlay.exists():
        try:
            return yaml.safe_load(overlay.read_text()) or {}
        except Exception:
            logger.exception("Failed to parse overlay instance.yaml at %s", overlay)
    ro = Path(os.environ.get("CONFIG_DIR", "./config")) / "instance.yaml"
    if ro.exists():
        try:
            return yaml.safe_load(ro.read_text()) or {}
        except Exception:
            logger.exception("Failed to parse static instance.yaml at %s", ro)
    return {}


def _public_view(config: dict) -> dict:
    """Return a config dict safe to render in the admin UI form.

    Deep-copies and redacts secret-looking fields so an admin can see
    *which* fields are populated without the cleartext leaking into the
    rendered HTML / browser DevTools.
    """
    import copy
    return _redact(copy.deepcopy(config))


class ServerConfigUpdateRequest(BaseModel):
    """Patch payload for POST /api/admin/server-config.

    Only the sections listed in `_EDITABLE_SECTIONS` are accepted; anything
    else is rejected with 400. `confirm_danger` must be true if the patch
    touches any danger-zone section (auth.*, server.*).
    """
    sections: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-section patch dict (e.g. {'instance': {'name': 'X'}})",
    )
    confirm_danger: bool = Field(
        default=False,
        description="Must be true to apply changes touching auth.* or server.*",
    )


@router.get("/server-config")
async def get_server_config(
    user: dict = Depends(require_role(Role.ADMIN)),
):
    """Return the current instance.yaml with secrets redacted.

    Used by the /admin/server-config UI to prefill its form. The redacted
    payload mirrors the actual file shape, so the UI doesn't need to know
    the schema — it iterates over the editable sections and renders the
    fields it finds. Empty sections still show in the response so the form
    knows to render their headers.
    """
    config = _load_current_instance_yaml()
    redacted = _public_view(config)
    # Surface every editable section so the UI renders them even when the
    # file omits them — operator can populate from scratch without manual
    # JSON edits.
    sections = {section: redacted.get(section, {}) for section in _EDITABLE_SECTIONS}
    return {
        "sections": sections,
        "editable_sections": list(_EDITABLE_SECTIONS),
        "danger_sections": list(_DANGER_SECTIONS),
        "secret_key_patterns": list(_SECRET_KEY_PATTERNS),
    }


@router.post("/server-config")
async def update_server_config(
    request: ServerConfigUpdateRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Patch instance.yaml from the /admin/server-config editor.

    Accepts a partial patch keyed by section. Validates sections, refuses
    danger-zone edits without explicit confirmation, deep-merges into the
    current overlay, writes the file, and emits one audit entry per save
    with a sanitized diff. Returns ``restart_required=true`` so the UI can
    show the restart banner — hot-reload is a separate issue (see #91 Out
    of scope).
    """
    import yaml

    if not request.sections:
        raise HTTPException(status_code=422, detail="sections cannot be empty")

    # Reject unknown sections loudly. Without this, a typo like "thmee"
    # would silently land in the YAML root and the operator wouldn't see
    # their colour change apply.
    unknown = sorted(set(request.sections.keys()) - set(_EDITABLE_SECTIONS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown section(s): {', '.join(unknown)}. "
                   f"Editable: {', '.join(_EDITABLE_SECTIONS)}",
        )

    # Danger-zone gate. The UI shows a confirmation dialog before posting
    # with confirm_danger=true; an API caller (CLI/script) has to pass it
    # explicitly so they can't fat-finger a hostname change.
    danger_touched = sorted(set(request.sections.keys()) & set(_DANGER_SECTIONS))
    if danger_touched and not request.confirm_danger:
        raise HTTPException(
            status_code=400,
            detail=f"section(s) {', '.join(danger_touched)} require confirm_danger=true",
        )

    before = _load_current_instance_yaml()

    # Deep merge — section-by-section so we never accidentally delete a
    # sibling section the patch didn't touch.
    after = dict(before)
    for section, patch in request.sections.items():
        if not isinstance(patch, dict):
            raise HTTPException(
                status_code=422,
                detail=f"section '{section}' must be an object, got {type(patch).__name__}",
            )
        if isinstance(after.get(section), dict):
            after[section] = _deep_merge(after[section], patch)
        else:
            after[section] = patch

    # Write to DATA_DIR/state/instance.yaml — same path /api/admin/configure
    # uses, since CONFIG_DIR is read-only in containerized deploys.
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    config_path = data_dir / "state" / "instance.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(after, default_flow_style=False, sort_keys=False))
    logger.info("server-config: wrote %d section(s) to %s",
                len(request.sections), config_path)

    # Invalidate cached instance config so subsequent reads pick up the
    # change. Hot-reload of running modules (auth providers, SMTP client)
    # is out of scope — the restart banner tells the operator to bounce.
    import app.instance_config as ic
    ic._instance_config = None

    # Audit entry — diff is computed AFTER redaction so secret values
    # never land in audit_log.params. We log even if no fields actually
    # changed so the operator's intent (touched the page, hit save) is
    # auditable.
    diff = _diff_dicts(_redact(before), _redact(after))
    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="instance_config.update",
        resource="instance.yaml",
        params={
            "sections": sorted(request.sections.keys()),
            "danger_sections": danger_touched,
            "diff": diff,
            "diff_count": len(diff),
        },
    )

    return {
        "status": "ok",
        "restart_required": True,
        "sections_updated": sorted(request.sections.keys()),
        "diff_count": len(diff),
    }


# --- End server-config editor -----------------------------------------------


class RegisterTableRequest(BaseModel):
    name: str
    folder: Optional[str] = None
    sync_strategy: str = "full_refresh"
    primary_key: Optional[str] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    query_mode: str = "local"
    sync_schedule: Optional[str] = None
    profile_after_sync: bool = True


class UpdateTableRequest(BaseModel):
    name: Optional[str] = None
    sync_strategy: Optional[str] = None
    primary_key: Optional[str] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    query_mode: Optional[str] = None
    sync_schedule: Optional[str] = None
    profile_after_sync: Optional[bool] = None


class ConfigureRequest(BaseModel):
    data_source: str  # "keboola" | "bigquery" | "local"
    keboola_token: Optional[str] = None
    keboola_url: Optional[str] = None
    bigquery_project: Optional[str] = None
    bigquery_location: Optional[str] = None
    instance_name: Optional[str] = None
    allowed_domain: Optional[str] = None


@router.get("/discover-tables")
async def discover_tables(
    user: dict = Depends(require_role(Role.ADMIN)),
):
    """Discover all available tables from the configured data source."""
    try:
        from app.instance_config import get_data_source_type
        source_type = get_data_source_type()

        if source_type == "keboola":
            from connectors.keboola.client import KeboolaClient
            from app.instance_config import get_value
            url = get_value("data_source", "keboola", "stack_url", default="")
            token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
            token = os.environ.get(token_env, "") if token_env else ""
            if not token:
                token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")
            client = KeboolaClient(token=token, url=url)
            tables = client.discover_all_tables()
            return {"tables": tables, "count": len(tables), "source": "keboola"}
        else:
            return {"tables": [], "count": 0, "source": source_type, "error": "Discovery not implemented for this source"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {e}")


@router.get("/registry")
async def list_registry(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get full table registry."""
    repo = TableRegistryRepository(conn)
    tables = repo.list_all()
    return {"tables": tables, "count": len(tables)}


@router.post("/register-table", status_code=201)
async def register_table(
    request: RegisterTableRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a new table in the system."""
    if not request.name or not request.name.strip():
        raise HTTPException(status_code=422, detail="Table name cannot be empty")
    repo = TableRegistryRepository(conn)
    table_id = request.name.strip().lower().replace(" ", "_")

    if repo.get(table_id):
        raise HTTPException(status_code=409, detail=f"Table '{table_id}' already registered")

    repo.register(
        id=table_id,
        name=request.name,
        folder=request.folder,
        sync_strategy=request.sync_strategy,
        primary_key=request.primary_key,
        description=request.description,
        registered_by=user.get("email"),
        source_type=request.source_type,
        bucket=request.bucket,
        source_table=request.source_table,
        query_mode=request.query_mode,
        sync_schedule=request.sync_schedule,
        profile_after_sync=request.profile_after_sync,
    )

    return {"id": table_id, "name": request.name, "status": "registered"}


@router.put("/registry/{table_id}")
async def update_table(
    table_id: str,
    request: UpdateTableRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update a registered table's configuration."""
    repo = TableRegistryRepository(conn)
    if not repo.get(table_id):
        raise HTTPException(status_code=404, detail="Table not found")

    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if updates:
        existing = repo.get(table_id)
        merged = {k: v for k, v in existing.items() if k != "registered_at"}
        merged.update(updates)
        merged.pop("id", None)  # avoid duplicate id kwarg
        repo.register(id=table_id, **merged)
    return {"id": table_id, "updated": list(updates.keys())}


@router.delete("/registry/{table_id}", status_code=204)
async def unregister_table(
    table_id: str,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unregister a table from the system."""
    repo = TableRegistryRepository(conn)
    if not repo.get(table_id):
        raise HTTPException(status_code=404, detail="Table not found")
    repo.unregister(table_id)


@router.post("/configure")
async def configure_instance(
    request: ConfigureRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
):
    """Configure data source and instance settings via API.

    Writes config to instance.yaml and persists secrets to .env_overlay.
    AI agents and the /setup wizard use this instead of manual file editing.
    """
    import yaml

    if request.data_source not in ("keboola", "bigquery", "local"):
        raise HTTPException(status_code=400, detail="data_source must be 'keboola', 'bigquery', or 'local'")

    # Validate credentials if provided
    if request.data_source == "keboola":
        if not request.keboola_token or not request.keboola_url:
            raise HTTPException(status_code=400, detail="keboola_token and keboola_url are required for Keboola data source")
        try:
            from connectors.keboola.client import KeboolaClient
            client = KeboolaClient(token=request.keboola_token, url=request.keboola_url)
            client.test_connection()
        except Exception as e:
            logger.error("Keboola connection validation failed: %s", e)
            raise HTTPException(status_code=400, detail="Keboola connection failed. Check your token and URL.")

    elif request.data_source == "bigquery":
        if not request.bigquery_project:
            raise HTTPException(status_code=400, detail="bigquery_project is required for BigQuery data source")

    # Write instance.yaml to DATA_DIR/state/ (writable Docker volume),
    # NOT to CONFIG_DIR which is mounted read-only in Docker.
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    config_path = data_dir / "state" / "instance.yaml"

    # Load existing API-generated config, or fall back to read-only CONFIG_DIR config
    existing = {}
    if config_path.exists():
        try:
            existing = yaml.safe_load(config_path.read_text()) or {}
        except Exception:
            existing = {}
    else:
        # Try loading from read-only config as base
        ro_path = Path(os.environ.get("CONFIG_DIR", "./config")) / "instance.yaml"
        if ro_path.exists():
            try:
                existing = yaml.safe_load(ro_path.read_text()) or {}
            except Exception:
                existing = {}

    # Merge instance settings
    if request.instance_name:
        existing.setdefault("instance", {})["name"] = request.instance_name

    if request.allowed_domain:
        existing.setdefault("auth", {})["allowed_domain"] = request.allowed_domain

    # Merge data source config (secrets as env var references)
    existing["data_source"] = {"type": request.data_source}
    if request.data_source == "keboola":
        existing["data_source"]["keboola"] = {
            "stack_url": request.keboola_url,
            "token_env": "KEBOOLA_STORAGE_TOKEN",
        }
    elif request.data_source == "bigquery":
        existing["data_source"]["bigquery"] = {
            "project": request.bigquery_project,
            "location": request.bigquery_location or "us",
        }

    # Write to writable data volume
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(existing, default_flow_style=False, sort_keys=False))
    logger.info("Wrote instance config to %s", config_path)

    # Persist secrets to .env_overlay (in data volume, never in git)
    secrets_to_persist = {}
    if request.keboola_token:
        secrets_to_persist["KEBOOLA_STORAGE_TOKEN"] = request.keboola_token
    if request.keboola_url:
        secrets_to_persist["KEBOOLA_STACK_URL"] = request.keboola_url

    if secrets_to_persist:
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        overlay_path = data_dir / "state" / ".env_overlay"
        overlay_path.parent.mkdir(parents=True, exist_ok=True)

        # Merge with existing overlay
        existing_overlay = {}
        if overlay_path.exists():
            for line in overlay_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing_overlay[k.strip()] = v.strip()
        existing_overlay.update(secrets_to_persist)

        overlay_path.write_text(
            "\n".join(f"{k}={v}" for k, v in existing_overlay.items()) + "\n"
        )
        try:
            overlay_path.chmod(0o600)
        except OSError:
            pass
        logger.info("Persisted %d secrets to .env_overlay", len(secrets_to_persist))

        # Inject into current process environment
        for k, v in secrets_to_persist.items():
            os.environ[k] = v

    # Invalidate cached instance config so next read picks up changes
    import app.instance_config as ic
    ic._instance_config = None

    return {
        "status": "ok",
        "data_source": request.data_source,
        "connection": "verified" if request.data_source != "local" else "local",
    }


def _discover_and_register_tables(conn: duckdb.DuckDBPyConnection, user_email: str) -> dict:
    """Discover tables from configured source and register them. Shared logic for API and sync."""
    from app.instance_config import get_data_source_type, get_value

    source_type = get_data_source_type()
    if source_type != "keboola":
        return {"registered": 0, "skipped": 0, "errors": 0, "tables": [], "source": source_type}

    from connectors.keboola.client import KeboolaClient
    # Read from data_source.keboola (matches what /api/admin/configure writes)
    url = get_value("data_source", "keboola", "stack_url", default="")
    token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
    token = os.environ.get(token_env, "") if token_env else ""
    if not token:
        token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")

    client = KeboolaClient(token=token, url=url)
    discovered = client.discover_all_tables()

    repo = TableRegistryRepository(conn)
    registered = 0
    skipped = 0
    errors = 0
    table_names = []

    for table in discovered:
        table_id = table.get("id", "").strip().lower().replace(".", "_").replace(" ", "_")
        if not table_id:
            errors += 1
            continue

        if repo.get(table_id):
            skipped += 1
            continue

        try:
            # Parse bucket from table ID (format: in.c-bucket.table_name)
            parts = table.get("id", "").split(".")
            bucket = parts[1] if len(parts) > 1 else ""
            source_table = parts[2] if len(parts) > 2 else table.get("name", "")

            repo.register(
                id=table_id,
                name=table.get("name", table_id),
                source_type="keboola",
                bucket=bucket,
                source_table=source_table,
                query_mode="local",
                registered_by=user_email,
                description=f"Auto-discovered from Keboola: {table.get('id', '')}",
            )
            registered += 1
            table_names.append(table_id)
        except Exception as e:
            logger.warning("Failed to register %s: %s", table_id, e)
            errors += 1

    return {
        "registered": registered,
        "skipped": skipped,
        "errors": errors,
        "tables": table_names,
        "source": "keboola",
    }


@router.post("/discover-and-register")
async def discover_and_register(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Discover tables from configured source and auto-register them.

    Combines discover-tables + register-table into one call.
    Skips already-registered tables. Used by /setup wizard and AI agents.
    """
    try:
        result = _discover_and_register_tables(conn, user.get("email", "admin"))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery and registration failed: {e}")
