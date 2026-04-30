"""Web UI routes — Jinja2 templates served by FastAPI.

Replicates all Flask webapp routes with DuckDB-backed data.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import duckdb

import jinja2

from app.auth.access import is_user_admin, require_admin
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


_URL_MAP = {
    # Flask-style endpoint names → FastAPI URL paths
    "dashboard": "/dashboard",
    "catalog": "/catalog",
    "corporate_memory": "/corporate-memory",
    "corporate_memory_admin": "/corporate-memory/admin",
    "activity_center": "/activity-center",
    "index": "/",
    "auth.login": "/login",
    "auth.logout": "/login",  # No logout route — redirect to login
    "password_auth.login_email": "/auth/password/login",
    "password_auth.reset_request": "/auth/password/reset",
    "password_auth.request_access": "/auth/password/setup",
    "email_auth.login_email_form": "/login/email",
    "email_auth.send_magic_link": "/auth/email/send-link",
    "register": "/auth/password/setup",
    "setup": "/setup",
}


def _url_for_shim(endpoint: str, **kw) -> str:
    """Flask url_for compatibility — maps endpoint names to FastAPI paths."""
    if endpoint == "static":
        filename = kw.get("filename", "")
        return f"/static/{filename}"
    return _URL_MAP.get(endpoint, f"/{endpoint}")


def _read_agnes_ca_pem() -> Optional[str]:
    """Read the Agnes server's TLS fullchain for inlining into the setup prompt.

    Returns the PEM string when the cert needs trust-bootstrapping —
    self-signed (subject == issuer of the leaf), private CA chain, or any
    case where we can't cheaply prove the OS would trust it. Returns None
    only for chains where the leaf's issuer matches a CA already in the
    server's `certifi`-backed default trust store (publicly-trusted CA
    like Let's Encrypt or a real corp PKI root that's distributed widely
    enough to be in `certifi`).

    Inlining a publicly-trusted cert is harmless (clients already trust
    it via OS roots), but it bloats the prompt and steers users into
    setting SSL_CERT_FILE unnecessarily, which narrows their Python TLS
    trust to just this host. So skip when we can confirm broad trust.

    Path is configurable via AGNES_TLS_FULLCHAIN_PATH (defaults to
    `/data/state/certs/fullchain.pem`, the location `agnes-tls-rotate.sh`
    writes on every VM and `docker-compose.host-mount.yml` rbinds into
    the app container). Missing / unreadable / unparseable → None, and
    the setup prompt falls back to its pre-cert behavior.
    """
    path = Path(os.environ.get("AGNES_TLS_FULLCHAIN_PATH", "/data/state/certs/fullchain.pem"))
    try:
        if not path.is_file():
            return None
        pem = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if "-----BEGIN CERTIFICATE-----" not in pem:
        return None

    try:
        from cryptography import x509
        # Parse just the first cert in the chain — that's the leaf, and
        # leaf issuer/subject is what determines self-signed vs CA-signed.
        first_block = pem.split("-----END CERTIFICATE-----", 1)[0] + "-----END CERTIFICATE-----\n"
        leaf = x509.load_pem_x509_certificate(first_block.encode("utf-8"))

        if leaf.issuer == leaf.subject:
            # Self-signed — definitely needs bootstrap on the client.
            return pem

        # CA-signed leaf: check whether `certifi`'s trust store already
        # contains the issuer. If yes, the user's `da`/uv (which both
        # use `certifi` by default) will validate without our help.
        try:
            import certifi
            with open(certifi.where(), "rb") as fh:
                trust_pem = fh.read()
        except Exception:
            return pem  # can't enumerate trust → assume bootstrap needed

        issuer_dn = leaf.issuer.rfc4514_string()
        for ca in x509.load_pem_x509_certificates(trust_pem):
            if ca.subject.rfc4514_string() == issuer_dn:
                return None  # publicly trusted; client OS already accepts
        return pem
    except Exception:  # pragma: no cover — defensive: bad PEM / x509 error
        logger.exception("Failed to evaluate Agnes TLS cert; skipping inline")
        return None


def _build_context(
    request: Request,
    user: Optional[dict] = None,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    **extra,
) -> dict:
    """Build template context with config, user, and theme.

    `conn` is optional: when supplied alongside a logged-in `user`, the
    setup-prompt preview/clipboard payload is rendered with that user's
    RBAC-allowed Claude Code marketplace plugins inlined as install
    commands. Routes that don't render the env-setup-cta block can omit it.
    """
    class ConfigProxy:
        INSTANCE_NAME = get_instance_name()
        INSTANCE_SUBTITLE = get_instance_subtitle()
        INSTANCE_COPYRIGHT = ""
        LOGO_SVG = ""
        TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")
        SSH_ALIAS = "data-analyst"
        SERVER_HOST = os.environ.get("SERVER_HOST", "")
        PROJECT_DIR = "data-analyst"
        # Drives whether the user dropdown renders the "Auth debug" link.
        # Same env var the route guard checks — keep them in lock-step so
        # the link never appears when the route would 404, and vice versa.
        DEBUG_AUTH_ENABLED = os.environ.get("AGNES_DEBUG_AUTH", "").strip().lower() in (
            "1", "true", "yes",
        )
        # Google Workspace prefix-mapping config — surfaced into templates
        # so client-side JS can derive a friendly display name from the
        # full Workspace email stored as the group's `name` (admin UI
        # strips the prefix and `@domain` for the big line, keeps the
        # full email as subtitle). Read at template render time so an
        # operator can flip these via env without an image rebuild.
        AGNES_GOOGLE_GROUP_PREFIX = os.environ.get(
            "AGNES_GOOGLE_GROUP_PREFIX", ""
        )
        AGNES_GROUP_ADMIN_EMAIL = os.environ.get(
            "AGNES_GROUP_ADMIN_EMAIL", ""
        )
        AGNES_GROUP_EVERYONE_EMAIL = os.environ.get(
            "AGNES_GROUP_EVERYONE_EMAIL", ""
        )

        @staticmethod
        def theme_overrides():
            theme = get_theme()
            # Return dict of CSS variable overrides (only non-empty values)
            if isinstance(theme, dict):
                return {k: v for k, v in theme.items() if v}
            return {}

    # Lines + server_url for the "Setup a new Claude Code" preview/clipboard
    # partial; single source of truth lives in app/web/setup_instructions.py.
    # Resolve the wheel filename server-side so the URL in the setup snippet
    # is a PEP 427-compliant path — `uv tool install` rejects bare `agnes.whl`.
    from app.web.setup_instructions import resolve_lines
    from app.api.cli_artifacts import _find_wheel
    _wheel = _find_wheel()
    _wheel_filename = _wheel.name if _wheel else "agnes.whl"

    # Inline the user's RBAC-allowed marketplace plugins as `claude plugin
    # install` commands so a single paste also bootstraps the marketplace
    # and plugin set. Anonymous viewers (no user, or no DB conn) get the
    # original 6-step layout.
    plugin_install_names: list[str] = []
    if user and conn is not None:
        try:
            from src import marketplace_filter
            plugin_install_names = [
                p["manifest_name"]
                for p in marketplace_filter.resolve_allowed_plugins(conn, user)
            ]
        except Exception:  # pragma: no cover — defensive: never block dashboard render
            logger.exception("Failed to resolve marketplace plugins for setup prompt")
            plugin_install_names = []

    # `AGNES_DEBUG_AUTH` is the existing dev/staging gate (see
    # `app/api/me_debug.py`, `app/web/router.py` template ConfigProxy).
    # When on, the setup prompt also disables host-scoped git TLS verify
    # so `claude plugin marketplace add` works against self-signed instances.
    # Subsumed by the cert trust block when `ca_pem` is loaded below.
    self_signed_tls = os.environ.get("AGNES_DEBUG_AUTH", "").strip().lower() in (
        "1", "true", "yes",
    )
    server_host = request.url.netloc

    ca_pem = _read_agnes_ca_pem()

    setup_instructions_lines = resolve_lines(
        _wheel_filename,
        plugin_install_names=plugin_install_names,
        self_signed_tls=self_signed_tls,
        server_host=server_host,
        ca_pem=ca_pem,
    )
    ctx_server_url = str(request.base_url).rstrip("/")

    ctx = {
        "request": request,
        "config": ConfigProxy,
        "user": _flex(user) if user else _FlexDict(),
        "now": datetime.now,
        "static_url": lambda path: f"/static/{path}",
        # Flask compatibility shims for templates
        "get_flashed_messages": lambda **kwargs: [],
        "url_for": lambda endpoint, **kw: _url_for_shim(endpoint, **kw),
        "session": _FlexDict({"user": user}) if user else _FlexDict(),
        "setup_instructions_lines": setup_instructions_lines,
        "server_url": ctx_server_url,
    }
    # Flex all extra context values for template compatibility
    # (but skip ones we just populated — extras with the same key win)
    for k, v in extra.items():
        ctx[k] = _flex(v) if isinstance(v, (dict, list)) else v
    return ctx


# ---- Navigation ----

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/setup", response_class=HTMLResponse)
async def setup_wizard(request: Request, conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    """First-time setup wizard. Redirects to dashboard if users already exist."""
    try:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count > 0:
            return RedirectResponse(url="/login", status_code=302)
    except Exception:
        pass  # No users table yet — show setup
    return templates.TemplateResponse(request, "setup.html", _build_context(request))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    from app.auth.dependencies import is_local_dev_mode, _get_local_dev_user
    if is_local_dev_mode():
        # Only short-circuit to /dashboard if the dev user is actually seeded.
        # Otherwise the 401 from /dashboard would bounce back to /login and loop.
        from src.db import get_system_db
        conn = get_system_db()
        try:
            if _get_local_dev_user(conn):
                return RedirectResponse(url="/dashboard", status_code=302)
        finally:
            conn.close()
        # Fall through to the normal login form so the missing-seed error is visible.

    next_path = request.query_params.get("next", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""

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

    # Convert to login_buttons format expected by template
    login_buttons = []
    for p in providers:
        if p["name"] == "google":
            _url = "/auth/google/login"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append({"url": _url, "text": "Sign in with Google", "css_class": "btn-primary", "icon_html": ""})
        elif p["name"] == "password":
            _url = "/login/password"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append({"url": _url, "text": "Sign in with Email & Password", "css_class": "btn-secondary", "icon_html": ""})
        elif p["name"] == "email":
            _url = "/login/email"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append({"url": _url, "text": "Sign in with Email Link", "css_class": "btn-secondary", "icon_html": ""})

    ctx = _build_context(request, providers=providers, login_buttons=login_buttons, next_path=next_path)
    return templates.TemplateResponse(request, "login.html", ctx)


@router.get("/login/password", response_class=HTMLResponse)
async def login_password_page(request: Request):
    """Password login form (email + password)."""
    next_path = request.query_params.get("next", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""
    google_ok = False
    try:
        from app.auth.providers.google import is_available as google_available
        google_ok = google_available()
    except Exception:
        pass
    ctx = _build_context(request, google_available=google_ok, next_path=next_path)
    return templates.TemplateResponse(request, "login_email.html", ctx)


@router.get("/login/email", response_class=HTMLResponse)
async def login_email_page(request: Request):
    """Email magic link login form."""
    next_path = request.query_params.get("next", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""
    google_ok = False
    try:
        from app.auth.providers.google import is_available as google_available
        google_ok = google_available()
    except Exception:
        pass
    ctx = _build_context(request, google_available=google_ok, next_path=next_path)
    return templates.TemplateResponse(request, "login_email.html", ctx)


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
    is_admin = is_user_admin(user["id"], conn)

    class UserInfo:
        def __init__(self):
            self.exists = True
            self.is_admin = is_admin
            # Legacy fields kept so existing templates don't blow up — admin is
            # implicitly analyst/privileged, non-admins are not. Granular roles
            # collapsed in v12.
            self.is_analyst = is_admin
            self.is_privileged = is_admin
            self.username = user.get("email", "").split("@")[0]
            self.home_dir = ""
            self.groups = []

    ctx = _build_context(
        request, user=user, conn=conn,
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

    # Build stats + filter dropdowns from the full item set so the dropdowns
    # match the data the page is rendering. `categories` and `domains` are
    # consumed by the filter pickers in `corporate_memory.html`; without
    # `domains` the "All domains" picker stays empty.
    all_items = repo.list_items(limit=10000)
    categories = sorted(set(i.get("category", "") for i in all_items if i.get("category")))
    domains = sorted(set(i.get("domain", "") for i in all_items if i.get("domain")))

    # "My contributions" — items the caller authored. Personal items are
    # always visible to their author regardless of audience filtering;
    # this is the surface the user uses to mark/unmark `is_personal`.
    user_email = user.get("email") or ""
    user_contributions = repo.get_user_contributions(user_email) if user_email else []
    for item in user_contributions:
        votes = repo.get_votes(item["id"])
        item["upvotes"] = votes["upvotes"]
        item["downvotes"] = votes["downvotes"]

    ctx = _build_context(
        request, user=user,
        knowledge_items=items,
        governance_mode=governance_mode,
        governance={"mode": governance_mode, "groups": cm_config.get("groups", {})},
        categories=categories,
        domains=domains,
        stats={"total": len(all_items), "approved": len([i for i in all_items if i.get("status") == "approved"])},
        user_votes={},
        is_km_admin=is_user_admin(user["id"], conn),
        user_contributions=user_contributions,
        user_stats={"authored": len(user_contributions), "votes_given": 0},
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
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = KnowledgeRepository(conn)
    pending = repo.list_items(statuses=["pending"], limit=100)
    all_items = repo.list_items(limit=10000)
    status_counts = {}
    for item in all_items:
        s = item.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    # Contradictions tab is server-rendered (no JS fetch on this tab — see
    # corporate_memory_admin.html). Fetch the unresolved set and enrich each
    # entry with the title/sensitivity of both sides so the template doesn't
    # need to re-query per row.
    contradictions = repo.list_contradictions(resolved=False)
    item_lookup = {it["id"]: it for it in all_items}
    for c in contradictions:
        for side in ("item_a_id", "item_b_id"):
            base = item_lookup.get(c.get(side)) or {}
            target = "item_a" if side == "item_a_id" else "item_b"
            c[target] = {
                "title": base.get("title", ""),
                "content": base.get("content", ""),
                "domain": base.get("domain"),
                "sensitivity": base.get("sensitivity"),
                "status": base.get("status"),
                "hidden": base.get("is_personal", False),
            }

    # Duplicate-candidate badge count (issue #62) — unresolved relations only.
    duplicates_count = conn.execute(
        "SELECT COUNT(*) FROM knowledge_item_relations "
        "WHERE relation_type = 'likely_duplicate' AND resolved = FALSE"
    ).fetchone()[0]

    ctx = _build_context(
        request, user=user,
        pending_items=pending,
        stats={
            "total": len(all_items),
            "by_status": status_counts,
            "pending": len(pending),
            "pending_count": status_counts.get("pending", 0),
            "approved_count": status_counts.get("approved", 0),
            "mandatory_count": status_counts.get("mandatory", 0),
            "knowledge_count": len(all_items),
            "contradictions": len(contradictions),
            "duplicates": duplicates_count,
        },
        governance=get_corporate_memory_config(),
        groups=get_corporate_memory_config().get("groups", {}),
        contradictions=contradictions,
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


@router.get("/install", response_class=HTMLResponse)
async def install_page(
    request: Request,
    user: Optional[dict] = Depends(get_optional_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Public install instructions for the CLI."""
    base_url = str(request.base_url).rstrip("/")
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        server_url=base_url,
        agnes_version=os.environ.get("AGNES_VERSION", "dev"),
    )
    return templates.TemplateResponse(request, "install.html", ctx)


@router.get("/admin/tables", response_class=HTMLResponse)
async def admin_tables(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.repositories.table_registry import TableRegistryRepository
    from app.instance_config import get_data_source_type
    repo = TableRegistryRepository(conn)
    tables = repo.list_all()
    # Branch the register-modal layout server-side so the JS doesn't have
    # to round-trip /api/admin/server-config to learn the source type.
    data_source_type = get_data_source_type() or "keboola"
    ctx = _build_context(
        request,
        user=user,
        registered_tables=tables,
        data_source_type=data_source_type,
    )
    return templates.TemplateResponse(request, "admin_tables.html", ctx)


@router.get("/admin/server-config", response_class=HTMLResponse)
async def admin_server_config_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Server configuration editor — instance.yaml fields grouped by section.

    Shell-only page. The form is populated client-side from
    GET /api/admin/server-config (which redacts secrets) and submitted
    section-by-section to POST /api/admin/server-config. Auth/server
    sections require an explicit confirmation dialog before save (see
    ``_DANGER_SECTIONS`` in the API). Saves trigger the "restart required"
    banner — hot-reload is out of scope for #91.
    """
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_server_config.html", ctx)


@router.get("/admin/permissions", response_class=HTMLResponse)
async def admin_permissions_page(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin page for managing permissions and access requests."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_permissions.html", ctx)


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin page for user management."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_users.html", ctx)


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail_page(
    user_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-user detail page — core role + module capabilities + effective-roles debug.

    Renders shell HTML; the JS bootstraps all role data via the admin REST API
    (/api/admin/internal-roles, /api/admin/users/{id}/role-grants,
    /api/admin/users/{id}/effective-roles). Server-side we only need the
    target user's email + name so the page header renders before the API
    round-trips finish; everything role-related is loaded client-side so an
    admin reload picks up state changes from a sibling tab without a
    full-page reload elsewhere.
    """
    repo = UserRepository(conn)
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    ctx = _build_context(request, user=user, target_user=target)
    return templates.TemplateResponse(request, "admin_user_detail.html", ctx)


@router.get("/admin/groups", response_class=HTMLResponse)
async def admin_groups_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Group list view — full-width table of user_groups with origin chips,
    member/grant counts, and edit/delete affordances for non-system rows."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_groups.html", ctx)


@router.get("/admin/groups/{group_id}", response_class=HTMLResponse)
async def admin_group_detail_page(
    group_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Single-group detail page — header + members table. Resource grants
    live on /admin/grants (deep-linked from here)."""
    from src.repositories.user_groups import UserGroupsRepository
    from app.api.access import _is_google_managed, _mapped_email
    g = UserGroupsRepository(conn).get(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    # Project the same flags the API derives so the template avoids env
    # lookups: `is_google_managed` (created_by='system:google-sync' OR
    # system + env mapping) and `mapped_email` (the Workspace group
    # funneling members into the Admin/Everyone system row, when set).
    g_view = dict(g)
    g_view["is_google_managed"] = _is_google_managed(g)
    g_view["mapped_email"] = _mapped_email(g)
    ctx = _build_context(request, user=user, target_group=g_view)
    return templates.TemplateResponse(request, "admin_group_detail.html", ctx)


@router.get("/admin/access", response_class=HTMLResponse)
async def admin_access_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Resource access management — master-detail layout with the group list
    on the left and per-resource-type checkbox tree on the right. Supports
    ``?group=<id>`` deep-link from the group detail page.

    Underlying entity is `resource_grants`; the UI label "Resource access"
    matches what admins think about (who has access) rather than the table
    name (grants)."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_access.html", ctx)


@router.get("/admin/grants", response_class=HTMLResponse)
async def admin_grants_redirect(request: Request):
    """Backward-compat redirect for the page's previous URL."""
    qs = request.url.query
    target = "/admin/access" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=308)


@router.get("/admin/marketplaces", response_class=HTMLResponse)
async def admin_marketplaces_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin page for marketplace git repositories (register / sync / delete)."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_marketplaces.html", ctx)


@router.get("/tokens", response_class=HTMLResponse)
async def my_tokens_page(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """My tokens — ANY signed-in user (incl. admins' own).

    Always shows the user's own PATs. Create + reveal + revoke-own flow.
    Admins who need the org-wide view go to /admin/tokens.
    """
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "my_tokens.html", ctx)


@router.get("/admin/tokens", response_class=HTMLResponse)
async def admin_tokens_page(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Admin — list of ALL tokens for incident response + offboarding.

    Admin-only. No create form here (admins mint their own PATs via /tokens).
    URL param ?user=<email> pre-fills the owner filter (deep-link from
    /admin/users "Tokens" action).
    """
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "admin_tokens.html", ctx)


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """User profile — self-service view of identity and group memberships.

    Renders the user's account info plus a list of group memberships joined
    against ``user_groups`` (with the source label so users can tell which
    were added by an admin, by Google sync, or seeded at deploy).
    """
    rows = conn.execute(
        """SELECT g.id, g.name, g.description, g.is_system, m.source, m.added_at
           FROM user_group_members m
           JOIN user_groups g ON g.id = m.group_id
           WHERE m.user_id = ?
           ORDER BY g.is_system DESC, g.name""",
        [user["id"]],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    memberships = [dict(zip(cols, r)) for r in rows]

    ctx = _build_context(
        request,
        user=user,
        memberships=memberships,
        is_admin=is_user_admin(user["id"], conn),
    )
    return templates.TemplateResponse(request, "profile.html", ctx)
