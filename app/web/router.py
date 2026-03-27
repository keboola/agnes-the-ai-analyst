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

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
        "user": user,
        "now": datetime.now,
        "static_url": lambda path: f"/static/{path}",
        # Flask compatibility shims for templates
        "get_flashed_messages": lambda **kwargs: [],
        "url_for": lambda endpoint, **kw: f"/{endpoint}",
        "session": {"user": user} if user else {},
        **extra,
    }
    return ctx


# ---- Navigation ----

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    providers = [
        {"name": "google", "display_name": "Google", "icon": "google"},
    ]
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
        data_stats={"total_tables": total_tables, "total_rows": total_rows},
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

    # Build catalog data from config
    try:
        from src.config import get_config
        config = get_config()
        tables = []
        for tc in config.tables:
            table_data = {
                "id": tc.id,
                "name": tc.name,
                "description": tc.description,
                "dataset": getattr(tc, "dataset", None),
                "sync_strategy": tc.sync_strategy,
                "query_mode": getattr(tc, "query_mode", "local"),
                "profile": all_profiles.get(tc.id),
            }
            # Add sync state
            for state in all_states:
                if state["table_id"] == tc.id:
                    table_data["last_sync"] = state.get("last_sync")
                    table_data["rows"] = state.get("rows")
                    break
            tables.append(table_data)
    except Exception as e:
        tables = []
        logger.warning(f"Could not load catalog: {e}")

    ctx = _build_context(
        request, user=user,
        tables=tables,
        datasets=datasets,
        enabled_datasets=enabled_datasets,
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
    ctx = _build_context(
        request, user=user,
        knowledge_items=items,
        governance_mode=cm_config.get("distribution_mode"),
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
    ctx = _build_context(request, user=user, pending_items=pending)
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
    ctx = _build_context(request, user=user, stats=stats)
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
