"""Web UI routes — Jinja2 templates served by FastAPI.

Replicates all Flask webapp routes with DuckDB-backed data.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import duckdb

import jinja2

from app.auth.dependencies import get_current_user, get_optional_user, _get_db
from app.instance_config import (
    get_instance_name, get_instance_subtitle, get_datasets,
    get_theme, get_corporate_memory_config,
)
from src.repositories.sync_state import SyncStateRepository
from src.repositories.sync_settings import SyncSettingsRepository, DatasetPermissionRepository
from src.repositories.knowledge import KnowledgeRepository
from src.repositories.users import UserRepository
from src.repositories.profiles import ProfileRepository
from src.repositories.access_requests import AccessRequestRepository

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Make templates tolerant of missing variables (renders empty string instead of error)
class _SilentUndefined(jinja2.Undefined):
    """Silently handle any access on undefined variables — returns empty/falsy."""
    def __str__(self): return ""
    def __iter__(self): return iter([])
    def __bool__(self): return False
    def __len__(self): return 0
    def __getattr__(self, name): return self
    def __getitem__(self, name): return self
    def __call__(self, *args, **kwargs): return self
    def __int__(self): return 0

templates.env.undefined = _SilentUndefined

# Add custom JSON filter that handles _SilentUndefined and _FlexDict
import json as _json

class _SafeEncoder(_json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (_SilentUndefined, _FlexDict)):
            if isinstance(obj, _FlexDict) and dict.__len__(obj) > 0:
                return dict(obj)
            return None
        return super().default(obj)

templates.env.policies["json.dumps_function"] = lambda obj, **kw: _json.dumps(obj, cls=_SafeEncoder, **kw)


class _FlexDict(dict):
    """Dict that returns empty _FlexDict for missing keys and attributes.
    Prevents Jinja2 UndefinedError when templates access missing nested values."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            return _FlexDict()
    def __bool__(self): return bool(dict.__len__(self))
    def __str__(self): return ""
    def __int__(self): return 0
    def __float__(self): return 0.0
    def __iter__(self): return iter(dict.values(self)) if dict.__len__(self) else iter([])
    def __len__(self): return dict.__len__(self)
    def __call__(self, *args, **kwargs): return ""
    def __add__(self, other): return other
    def __radd__(self, other): return other
    def __sub__(self, other): return 0 - other if isinstance(other, (int, float)) else self
    def __rsub__(self, other): return other
    def __mul__(self, other): return 0
    def __rmul__(self, other): return 0
    def __truediv__(self, other): return 0
    def __rtruediv__(self, other): return 0
    def __mod__(self, other): return 0
    def __eq__(self, other): return False if dict.__len__(self) == 0 else dict.__eq__(self, other)
    def __ne__(self, other): return True if dict.__len__(self) == 0 else dict.__ne__(self, other)
    def __lt__(self, other): return False
    def __gt__(self, other): return False
    def __le__(self, other): return True
    def __ge__(self, other): return True
    def __contains__(self, item): return dict.__contains__(self, item) if dict.__len__(self) else False


def _flex(d):
    """Recursively convert dicts to _FlexDict for template compatibility."""
    if isinstance(d, dict) and not isinstance(d, _FlexDict):
        return _FlexDict({k: _flex(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_flex(i) for i in d]
    return d


def _build_context(request: Request, user: Optional[dict] = None, **extra) -> dict:
    """Build template context with config, user, and theme."""
    class ConfigProxy:
        INSTANCE_NAME = get_instance_name()
        INSTANCE_SUBTITLE = get_instance_subtitle()
        INSTANCE_COPYRIGHT = ""
        LOGO_SVG = ""
        TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")
        SSH_ALIAS = "data-analyst"
        SERVER_HOST = os.environ.get("SERVER_HOST", "")
        PROJECT_DIR = "data-analyst"

        @staticmethod
        def theme_overrides():
            theme = get_theme()
            # Return dict of CSS variable overrides (only non-empty values)
            if isinstance(theme, dict):
                return {k: v for k, v in theme.items() if v}
            return {}

    ctx = {
        "request": request,
        "config": ConfigProxy,
        "user": _flex(user) if user else _FlexDict(),
        "now": datetime.now,
        "static_url": lambda path: f"/static/{path}",
        # Flask compatibility shims for templates
        "get_flashed_messages": lambda **kwargs: [],
        "url_for": lambda endpoint, **kw: f"/{endpoint}",
        "session": _FlexDict({"user": user}) if user else _FlexDict(),
    }
    # Flex all extra context values for template compatibility
    for k, v in extra.items():
        ctx[k] = _flex(v) if isinstance(v, (dict, list)) else v
    return ctx


# ---- Navigation ----

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    providers = []
    try:
        from app.auth.providers.google import is_available as google_available
        if google_available():
            providers.append({"name": "google", "display_name": "Google", "icon": "google"})
    except Exception:
        pass
    providers.append({"name": "password", "display_name": "Email & Password", "icon": "key"})
    try:
        from app.auth.providers.email import is_available as email_available
        if email_available():
            providers.append({"name": "email", "display_name": "Email Link", "icon": "mail"})
    except Exception:
        pass
    ctx = _build_context(request, providers=providers)
    return templates.TemplateResponse(request, "login.html", ctx)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    sync_repo = SyncStateRepository(conn)
    settings_repo = SyncSettingsRepository(conn)
    profile_repo = ProfileRepository(conn)

    all_states = sync_repo.get_all_states()
    enabled_datasets = settings_repo.get_enabled_datasets(user["id"])
    datasets = get_datasets()

    # Stats
    total_tables = len(all_states)
    total_rows = sum(s.get("rows", 0) or 0 for s in all_states)

    # Build user_info object expected by dashboard template
    class UserInfo:
        def __init__(self):
            self.exists = True
            self.is_admin = user.get("role") == "admin"
            self.is_analyst = user.get("role") in ("analyst", "admin", "km_admin")
            self.is_privileged = user.get("role") == "admin"
            self.username = user.get("email", "").split("@")[0]
            self.home_dir = ""
            self.groups = []

    ctx = _build_context(
        request, user=user,
        user_info=UserInfo(),
        username=user.get("email", "").split("@")[0],
        total_tables=total_tables,
        total_rows=total_rows,
        sync_states=all_states,
        enabled_datasets=enabled_datasets,
        datasets=datasets,
        account_status="active",
        account_details=None,
        telegram_status={"linked": False},
        setup_instructions="Use 'da login' to connect your CLI tool.",
        data_stats={
            "tables": total_tables,
            "total_tables": total_tables,
            "columns": 0,
            "rows_display": f"{total_rows:,}" if total_rows else "0",
            "size_display": "0 MB",
            "unstructured_display": "0 MB",
            "total_rows": total_rows,
            "last_updated": None,
            "remote_tables": 0,
            "local_tables": total_tables,
        },
        categories=[],
        metrics_data=[],
        desktop_status={"linked": False},
        activity_summary={"total_sessions": 0, "total_queries": 0},
        knowledge_stats={"total": 0, "approved": 0},
        user_knowledge_stats={"authored": 0, "votes_given": 0},
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    sync_repo = SyncStateRepository(conn)
    settings_repo = SyncSettingsRepository(conn)
    profile_repo = ProfileRepository(conn)

    all_states = sync_repo.get_all_states()
    all_profiles = profile_repo.get_all()
    enabled_datasets = settings_repo.get_enabled_datasets(user["id"])
    datasets = get_datasets()

    # Build catalog data from table_registry in DuckDB
    try:
        from src.repositories.table_registry import TableRegistryRepository
        table_repo = TableRegistryRepository(conn)
        perm_repo = DatasetPermissionRepository(conn)
        access_repo = AccessRequestRepository(conn)
        registered = table_repo.list_all()

        # Pre-fetch user's pending access requests
        user_id = user.get("id", "")
        user_requests = access_repo.list_by_user(user_id)
        pending_request_table_ids = {
            r["table_id"] for r in user_requests if r.get("status") == "pending"
        }

        tables = []
        for tc in registered:
            table_id = tc.get("id", "")
            is_public = tc.get("is_public", True)
            has_access = is_public or perm_repo.has_access(user_id, table_id)

            table_data = {
                "id": table_id,
                "name": tc.get("name", ""),
                "description": tc.get("description", ""),
                "dataset": tc.get("bucket"),
                "sync_strategy": tc.get("sync_strategy", "full_refresh"),
                "query_mode": tc.get("query_mode", "local"),
                "profile": all_profiles.get(table_id),
                "is_public": is_public,
                "has_access": has_access,
                "pending_request": table_id in pending_request_table_ids,
            }
            # Add sync state
            for state in all_states:
                if state["table_id"] == table_id:
                    table_data["last_sync"] = state.get("last_sync")
                    table_data["rows"] = state.get("rows")
                    break
            tables.append(table_data)
    except Exception as e:
        tables = []
        pending_request_table_ids = set()
        logger.warning(f"Could not load catalog: {e}")

    # Build data_stats for catalog template
    total_rows = sum(s.get("rows", 0) or 0 for s in all_states)
    data_stats = {
        "total_tables": len(all_states),
        "total_rows": total_rows,
        "total_columns": 0,
        "total_size": sum(s.get("file_size_bytes", 0) or 0 for s in all_states),
        "last_updated": max((s.get("last_sync") for s in all_states if s.get("last_sync")), default=None),
    }

    # Build categories from tables
    categories = {}
    for t in tables:
        ds = t.get("dataset") or "default"
        if ds not in categories:
            categories[ds] = {"name": ds, "tables": []}
        categories[ds]["tables"].append(t)

    # Add count to each category (template expects .count)
    catalog_data = []
    for cat in categories.values():
        cat["count"] = len(cat["tables"])
        catalog_data.append(cat)

    ctx = _build_context(
        request, user=user,
        tables=tables,
        datasets=datasets,
        enabled_datasets=enabled_datasets,
        data_stats=data_stats,
        categories=catalog_data,
        catalog_data=catalog_data,
        metrics_data=[],
        sync_states=all_states,
        folder_mapping={},
    )
    return templates.TemplateResponse(request, "catalog.html", ctx)


@router.get("/corporate-memory", response_class=HTMLResponse)
async def corporate_memory(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    items = repo.list_items(statuses=["approved", "mandatory"], limit=100)

    # Enrich with votes
    for item in items:
        votes = repo.get_votes(item["id"])
        item["upvotes"] = votes["upvotes"]
        item["downvotes"] = votes["downvotes"]

    cm_config = get_corporate_memory_config()
    governance_mode = cm_config.get("distribution_mode")

    # Build stats
    all_items = repo.list_items(limit=10000)
    categories = sorted(set(i.get("category", "") for i in all_items if i.get("category")))

    ctx = _build_context(
        request, user=user,
        knowledge_items=items,
        governance_mode=governance_mode,
        governance={"mode": governance_mode, "groups": cm_config.get("groups", {})},
        categories=categories,
        stats={"total": len(all_items), "approved": len([i for i in all_items if i.get("status") == "approved"])},
        user_votes={},
        is_km_admin=user.get("role") in ("km_admin", "admin"),
        user_stats={"authored": 0, "votes_given": 0},
        # Template expects knowledge as object with .items and .total_pages
        knowledge={"items": items, "total_pages": 1, "page": 1, "per_page": 100, "total": len(items)},
        total_pages=1,
        current_page=1,
        page=1,
        per_page=100,
    )
    return templates.TemplateResponse(request, "corporate_memory.html", ctx)


@router.get("/corporate-memory/admin", response_class=HTMLResponse)
async def corporate_memory_admin(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    pending = repo.list_items(statuses=["pending"], limit=100)
    all_items = repo.list_items(limit=10000)
    status_counts = {}
    for item in all_items:
        s = item.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1
    ctx = _build_context(
        request, user=user,
        pending_items=pending,
        stats={"total": len(all_items), "by_status": status_counts, "pending": len(pending)},
        governance=get_corporate_memory_config(),
        groups=get_corporate_memory_config().get("groups", {}),
        audit_entries=[],
    )
    return templates.TemplateResponse(request, "corporate_memory_admin.html", ctx)


@router.get("/activity-center", response_class=HTMLResponse)
async def activity_center(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    stats = {
        "total_items": len(repo.list_items(limit=10000)),
    }
    ctx = _build_context(
        request, user=user,
        stats=stats,
        activity={"recent_sessions": [], "recent_reports": [], "insights": []},
        knowledge_stats={"total": 0, "approved": 0, "mandatory": 0},
    )
    return templates.TemplateResponse(request, "activity_center.html", ctx)


@router.get("/admin/tables", response_class=HTMLResponse)
async def admin_tables(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.repositories.table_registry import TableRegistryRepository
    repo = TableRegistryRepository(conn)
    tables = repo.list_all()
    ctx = _build_context(request, user=user, registered_tables=tables)
    return templates.TemplateResponse(request, "admin_tables.html", ctx)


@router.get("/admin/permissions", response_class=HTMLResponse)
async def admin_permissions_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin page for managing permissions and access requests."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_permissions.html", ctx)
