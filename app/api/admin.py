"""Admin endpoints — table discovery, registry management, instance configuration.

Every gate on this router uses ``require_admin`` from ``app.auth.access``,
which checks Admin user_group membership for both OAuth session and PAT
callers via the same ``_user_group_ids`` lookup.
"""

import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional, List
import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.table_registry import TableRegistryRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

# SSRF protection: reject private/internal URLs for keboola_url
import ipaddress as _ipaddress
import socket as _socket
from urllib.parse import urlparse as _urlparse


def _validate_url_not_private(url: str, field_name: str = "url") -> None:
    """Raise 400 if the URL host points to a private/reserved network.

    Uses DNS resolution + ipaddress checks instead of hostname regex,
    which correctly handles all IPv4/IPv6 addresses including abbreviated
    forms (fe80::1, ::1, etc.) and DNS rebinding (resolves at check time).
    """
    try:
        parsed = _urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: not a valid URL")
    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: missing hostname")

    # Reject well-known dangerous hostnames before DNS resolution
    if host.lower() in ("localhost", "localhost.localdomain"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: must not point to a private or reserved network",
        )

    # Resolve hostname to IP addresses and check each one
    try:
        addrinfos = _socket.getaddrinfo(host, None, proto=_socket.IPPROTO_TCP)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: could not resolve hostname",
        )

    for family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            ip = _ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {field_name}: must not point to a private or reserved network",
            )


def _normalize_primary_key(v):
    """Coerce a string primary_key to ``[v]`` for backward compatibility.

    The 0.14.0 contract is ``Optional[List[str]]`` so composite primary keys
    (e.g. session-grain tables keyed on ``(session_id, event_date)``) round-
    trip cleanly. Pre-0.14.0 callers sent a single string; Pydantic v2
    refuses to coerce, so without this validator a CLI script posting
    ``"primary_key": "session_id"`` would now hit a 422. Wrap a bare string
    in a one-element list so old and new callers both work.
    """
    if v is None:
        return v
    if isinstance(v, str):
        return [v]
    return v


class RegisterTableRequest(BaseModel):
    name: str
    folder: Optional[str] = None
    sync_strategy: str = "full_refresh"
    # Composite primary keys are real (session-grain MSA tables key on
    # `(session_id, event_date)`, browse rows on more). The frontend sends +
    # reads this as a list; backend stores it JSON-serialized in VARCHAR.
    # A bare string is accepted for backward compat — see _normalize_primary_key.
    primary_key: Optional[List[str]] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    query_mode: str = "local"
    sync_schedule: Optional[str] = None
    profile_after_sync: bool = True

    @field_validator("primary_key", mode="before")
    @classmethod
    def _coerce_primary_key(cls, v):
        return _normalize_primary_key(v)


class UpdateTableRequest(BaseModel):
    name: Optional[str] = None
    sync_strategy: Optional[str] = None
    primary_key: Optional[List[str]] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    query_mode: Optional[str] = None
    sync_schedule: Optional[str] = None
    profile_after_sync: Optional[bool] = None

    @field_validator("primary_key", mode="before")
    @classmethod
    def _coerce_primary_key(cls, v):
        return _normalize_primary_key(v)


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
    user: dict = Depends(require_admin),
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
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get full table registry."""
    repo = TableRegistryRepository(conn)
    tables = repo.list_all()
    return {"tables": tables, "count": len(tables)}


@router.post("/register-table", status_code=201)
async def register_table(
    request: RegisterTableRequest,
    user: dict = Depends(require_admin),
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
    user: dict = Depends(require_admin),
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
    user: dict = Depends(require_admin),
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
    user: dict = Depends(require_admin),
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
        _validate_url_not_private(request.keboola_url, field_name="keboola_url")
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
    user: dict = Depends(require_admin),
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
