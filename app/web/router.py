"""Web UI routes — Jinja2 templates served by FastAPI.

Replicates all Flask webapp routes with DuckDB-backed data.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import duckdb

import jinja2

from app.auth.access import is_user_admin, require_admin
from app.auth.dependencies import get_current_user, get_optional_user, _get_db
from app.instance_config import (
    get_instance_name, get_instance_subtitle, get_datasets,
    get_theme, get_corporate_memory_config, get_home_route,
    get_gws_oauth_credentials, get_home_automode_visibility,
)
from src.repositories.sync_state import SyncStateRepository
from src.repositories.sync_settings import SyncSettingsRepository
from src.repositories.knowledge import KnowledgeRepository
from src.repositories.users import UserRepository
from src.repositories.profiles import ProfileRepository


def _resolved_home_route() -> str:
    """Lazy wrapper so tests/monkeypatch on env vars are honoured per-request."""
    return get_home_route()


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


def _humanbytes(value) -> str:
    """Render a byte count as the largest binary-prefixed unit it fits in.

    Below 1 KiB → integer bytes; otherwise two decimal places of KB / MB / GB
    (binary, 1024-based). Used by the Store detail template; intentionally
    permissive about input type so missing / undefined values render as
    ``0 B`` rather than crashing the page.
    """
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.2f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.2f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"


templates.env.filters["humanbytes"] = _humanbytes


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
    "setup": "/first-time-setup",
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
    self-signed (leaf issuer == subject), private-CA chain that doesn't
    terminate in a `certifi`-known root, or any case where we can't
    cheaply prove the OS would trust it. Returns None when the chain in
    the served fullchain.pem terminates in a publicly-trusted root that
    `certifi` already ships (Let's Encrypt's ISRG Root X1, DigiCert,
    etc.) — clients (Bun-compiled `claude.exe`, system git, Python with
    certifi) all accept the chain without help.

    Chain validation walks every cert in the served fullchain and
    succeeds the first time any cert's issuer matches a `certifi` root
    subject. That captures the standard fullchain shape (leaf +
    intermediate(s)) where `intermediate.issuer == publicly_trusted_root`,
    even though the leaf's *immediate* issuer is the intermediate (which
    is rarely shipped in trust stores — only roots are).

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
        chain = x509.load_pem_x509_certificates(pem.encode("utf-8"))
        if not chain:
            return None
        leaf = chain[0]

        if leaf.issuer == leaf.subject:
            # Self-signed — definitely needs bootstrap on the client.
            return pem

        # CA-signed leaf: walk every cert in the served fullchain (leaf +
        # intermediates) and check whether ANY of their issuers is in
        # `certifi`'s trust store. The first match means the chain
        # terminates in a publicly-trusted root, so the client OS / Bun
        # bundle / certifi already accept it.
        try:
            import certifi
            with open(certifi.where(), "rb") as fh:
                trust_pem = fh.read()
        except Exception:
            return pem  # can't enumerate trust → assume bootstrap needed

        trusted_subjects = {
            ca.subject.rfc4514_string()
            for ca in x509.load_pem_x509_certificates(trust_pem)
        }
        for cert in chain:
            if cert.issuer.rfc4514_string() in trusted_subjects:
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

    ctx_server_url = str(request.base_url).rstrip("/")

    # Lines for the "Setup a new Claude Code" preview/clipboard partial.
    #
    # When a DB connection is available, we go through render_agent_prompt_banner
    # which checks for an admin override first (stored in welcome_template) and
    # falls back to the live default from setup_instructions.resolve_lines().
    # This guarantees that both /setup and /dashboard clipboard CTA always reflect
    # the same content — the override is honoured everywhere.
    #
    # When no conn is supplied (e.g. public pages that don't need a DB round-trip)
    # we fall back to resolve_lines() directly with anonymous/no-plugin context.
    if conn is not None:
        from src.welcome_template import render_agent_prompt_banner
        _script_text = render_agent_prompt_banner(
            conn, user=user, server_url=ctx_server_url
        )
        setup_instructions_lines = _script_text.split("\n")
    else:
        # No DB connection — use the unauthenticated default (no override possible,
        # no marketplace plugins).
        from app.web.setup_instructions import resolve_lines
        from app.api.cli_artifacts import _find_wheel
        _wheel = _find_wheel()
        _wheel_filename = _wheel.name if _wheel else "agnes.whl"

        self_signed_tls = os.environ.get("AGNES_DEBUG_AUTH", "").strip().lower() in (
            "1", "true", "yes",
        )
        server_host = request.url.netloc
        ca_pem = _read_agnes_ca_pem()

        setup_instructions_lines = resolve_lines(
            _wheel_filename,
            plugin_install_names=[],
            self_signed_tls=self_signed_tls,
            server_host=server_host,
            ca_pem=ca_pem,
        )

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
        # Resolved per AGNES_HOME_ROUTE env > instance.home_route YAML >
        # /dashboard. The shared navbar's "Dashboard" link uses this so a
        # single env flip routes the primary nav target between /home
        # (state-aware landing) and /dashboard (legacy table inventory).
        "home_route": _resolved_home_route(),
        # Pre-configured Google Workspace CLI OAuth client for the
        # /home connector prompt. {} when unset → template falls back
        # to manual `gws auth setup`. See app.instance_config docstring.
        "gws_oauth": get_gws_oauth_credentials(),
        # Whether /home renders the "Step 3 — turn on auto-accept mode"
        # install-block. Operator can hide it via AGNES_HOME_SHOW_AUTOMODE=0
        # for cautious rollouts; same content stays on /setup-advanced.
        "home_automode": {"show": get_home_automode_visibility()},
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
        from app.instance_config import get_home_route
        return RedirectResponse(url=get_home_route(), status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/first-time-setup", response_class=HTMLResponse)
async def setup_wizard(request: Request, conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    """First-time setup wizard. Redirects to login if users already exist."""
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
        # Only short-circuit to the home route if the dev user is actually
        # seeded. Otherwise a 401 there would bounce back to /login and loop.
        from src.db import get_system_db
        conn = get_system_db()
        try:
            if _get_local_dev_user(conn):
                return RedirectResponse(url=get_home_route(), status_code=302)
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


@router.get("/home", response_class=HTMLResponse)
async def home_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """State-aware /home — full inline install for not-onboarded users,
    clean nav hub once onboarded. The boolean drives template selection;
    no auto-transition (manual reload picks up the flip after
    ``agnes init`` POSTs ``/api/me/onboarded``).

    See origin: docs/brainstorms/home-page-requirements.md.
    """
    row = conn.execute(
        "SELECT onboarded FROM users WHERE id = ?", [user["id"]]
    ).fetchone()
    onboarded = bool(row[0]) if row else False

    # Pull the latest published news intro for the bottom-of-page section.
    # Template renders the section only when intro is non-empty, so an
    # instance that has never published news shows nothing extra.
    from src.repositories.news_template import NewsTemplateRepository
    news = NewsTemplateRepository(conn).get_current_published()
    news_intro = news["intro"] if (news and news.get("intro")) else ""

    # Single template renders both states. The post-onboarding view keeps
    # the install-steps + connector prompts + auto-mode card visible —
    # they stay relevant for adding a second machine, a missing connector,
    # or re-running auto-mode setup. Hero copy + the self-mark control
    # branch on the boolean. The legacy `home_onboarded.html` is kept on
    # disk for a release as a fallback but no route renders it.
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        onboarded=onboarded,
        is_admin=is_user_admin(user["id"], conn),
        news_intro=news_intro,
    )
    return templates.TemplateResponse(request, "home_not_onboarded.html", ctx)


@router.get("/news", response_class=HTMLResponse)
async def news_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Permalink page for the latest published news. Renders empty-state
    copy when no version is published. Authed-only (same as /home).
    """
    from src.repositories.news_template import NewsTemplateRepository
    news = NewsTemplateRepository(conn).get_current_published()
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=is_user_admin(user["id"], conn),
        news=news,
    )
    return templates.TemplateResponse(request, "news.html", ctx)


@router.get("/admin/news", response_class=HTMLResponse)
async def admin_news_editor(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin authoring surface — current published banner, draft editor,
    versions table. JS hits the /api/admin/news/* endpoints for the
    write paths."""
    from src.repositories.news_template import NewsTemplateRepository
    repo = NewsTemplateRepository(conn)
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=True,
        news_current=repo.get_current_published(),
        news_draft=repo.get_active_draft(),
        news_versions=repo.list_versions(limit=50),
    )
    return templates.TemplateResponse(request, "admin/news_editor.html", ctx)


@router.get("/setup-advanced", response_class=HTMLResponse)
async def setup_advanced_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Advanced setup reference — VS Code layout, recommended plugins,
    multi-model second opinions, custom skills, cost guidance.

    Pulls the deeper Chief-of-Stuff guide content out of /home so /home
    stays scannable for first-hour onboarding. Linked from /home's
    "Want to look around first?" explore card and from any deep-link
    anchors emitted by other pages (e.g. /home's auto-mode block points
    at #yolo).
    """
    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        is_admin=is_user_admin(user["id"], conn),
    )
    return templates.TemplateResponse(request, "setup_advanced.html", ctx)


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

    # Build catalog data from table_registry in DuckDB. Filter pre-render so
    # the page only lists tables the user actually has access to — Admin
    # group members see everything (can_access shortcut), other users see
    # only entries with a matching resource_grants(group, "table", id) row.
    try:
        from src.repositories.table_registry import TableRegistryRepository
        from app.auth.access import can_access
        from app.resource_types import ResourceType
        table_repo = TableRegistryRepository(conn)
        registered = table_repo.list_all()

        user_id = user.get("id", "")
        tables = []
        for tc in registered:
            table_id = tc.get("id", "")
            if not can_access(user_id, ResourceType.TABLE.value, table_id, conn):
                continue
            table_data = {
                "id": table_id,
                "name": tc.get("name", ""),
                "description": tc.get("description", ""),
                "dataset": tc.get("bucket"),
                "sync_strategy": tc.get("sync_strategy", "full_refresh"),
                "query_mode": tc.get("query_mode", "local"),
                "profile": all_profiles.get(table_id),
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

    # #176: surface the pending review queue to admins. Without this the
    # main page silently filtered status='pending' items and operators had
    # no breadcrumb to /corporate-memory/admin.
    pending_count = sum(1 for i in all_items if i.get("status") == "pending")

    # "My contributions" — items the caller authored. Personal items are
    # always visible to their author regardless of audience filtering;
    # this is the surface the user uses to mark/unmark `is_personal`.
    user_email = user.get("email") or ""
    user_contributions = repo.get_user_contributions(user_email) if user_email else []
    for item in user_contributions:
        votes = repo.get_votes(item["id"])
        item["upvotes"] = votes["upvotes"]
        item["downvotes"] = votes["downvotes"]

    is_admin_view = is_user_admin(user["id"], conn)
    ctx = _build_context(
        request, user=user,
        knowledge_items=items,
        governance_mode=governance_mode,
        governance={"mode": governance_mode, "groups": cm_config.get("groups", {})},
        categories=categories,
        domains=domains,
        stats={"total": len(all_items), "approved": len([i for i in all_items if i.get("status") == "approved"])},
        user_votes={},
        is_km_admin=is_admin_view,
        user_contributions=user_contributions,
        user_stats={"authored": len(user_contributions), "votes_given": 0},
        # Template expects knowledge as object with .items and .total_pages
        knowledge={"items": items, "total_pages": 1, "page": 1, "per_page": 100, "total": len(items)},
        total_pages=1,
        current_page=1,
        page=1,
        per_page=100,
        # #176: pending banner is admin-only.
        pending_review_count=pending_count if is_admin_view else 0,
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


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    user: Optional[dict] = Depends(get_optional_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Setup instructions for the local agent (CLI + Claude Code).

    Single unified flow for everyone — admin-vs-analyst is no longer a
    layout branch. The marketplace + plugins block appears iff the
    caller has plugin grants in `resource_grants` (resolved inside
    `compute_default_agent_prompt`).

    When an admin override is saved, the override replaces the
    auto-generated setup_instructions output everywhere (both the
    /setup page display and the dashboard clipboard CTA). When no
    override is set, the live default from
    setup_instructions.resolve_lines() is used.
    """
    from src.repositories.welcome_template import WelcomeTemplateRepository
    from src.welcome_template import compute_default_agent_prompt, _sanitize_banner_html
    from jinja2 import Environment, StrictUndefined, TemplateError

    base_url = str(request.base_url).rstrip("/")

    # Determine the script text: override (Jinja2-rendered) or live default.
    # The override is per-instance, applies to every caller — admins who set
    # an override are opting into the exact text they wrote.
    row = WelcomeTemplateRepository(conn).get()
    override_content = row.get("content")
    if override_content:
        # Admin override — render Jinja2 placeholders server-side.
        # {server_url} and {token} survive because Jinja2 only processes
        # double-brace {{ }} syntax; single-brace {x} pass through unchanged.
        try:
            from src.welcome_template import build_context as _build_banner_ctx
            env = Environment(undefined=StrictUndefined, autoescape=False)
            template = env.from_string(override_content)
            ctx_vars = _build_banner_ctx(user=user, server_url=base_url)
            setup_script_text = _sanitize_banner_html(template.render(**ctx_vars))
        except (TemplateError, Exception) as exc:
            logger.warning("setup_page: override render failed (%s); falling back to default", exc)
            setup_script_text = compute_default_agent_prompt(
                conn, user=user, server_url=base_url,
            )
    else:
        setup_script_text = compute_default_agent_prompt(
            conn, user=user, server_url=base_url,
        )

    # Split for the legacy setup_instructions_lines list variable that the
    # Jinja2 partial (_claude_setup_instructions.jinja) uses.
    setup_instructions_lines = setup_script_text.split("\n")

    ctx = _build_context(
        request,
        user=user,
        conn=conn,
        server_url=base_url,
        agnes_version=os.environ.get("AGNES_VERSION", "dev"),
        banner_html="",  # no separate banner — the script IS the content
        # Override both variables so the partial and the JS array stay in sync.
        setup_instructions_lines=setup_instructions_lines,
        setup_script_text=setup_script_text,
    )
    return templates.TemplateResponse(request, "install.html", ctx)


@router.get("/install", response_class=HTMLResponse)
async def install_redirect(request: Request):
    """Backwards-compat redirect: /install → /setup (302).

    Using 302 (temporary) rather than 301 (permanent) so browsers/proxies
    don't cache indefinitely — if the path ever changes again, cached 301s
    require manual cache clearing to recover.
    """
    return RedirectResponse(url="/setup", status_code=302)


# ---------------------------------------------------------------------------
# Store + My AI Stack — community marketplace + per-user composition page.
# ---------------------------------------------------------------------------


@router.get("/store", response_class=HTMLResponse)
async def store_listing(
    request: Request,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "store_listing.html", ctx)


@router.get("/store/new", response_class=HTMLResponse)
async def store_new(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.store_categories import STORE_CATEGORIES
    ctx = _build_context(request, user=user, categories=list(STORE_CATEGORIES))
    return templates.TemplateResponse(request, "store_upload.html", ctx)


@router.get("/store/{entity_id}", response_class=HTMLResponse)
async def store_detail(
    request: Request,
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.user_store_installs import UserStoreInstallsRepository
    from src.store_naming import suffixed_name
    from app.utils import get_store_dir
    from app.auth.access import is_user_admin

    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    # File listing for the detail page (read directly from disk).
    plugin_dir = get_store_dir() / entity_id / "plugin"
    files = []
    if plugin_dir.is_dir():
        for f in sorted(plugin_dir.rglob("*")):
            if f.is_file():
                files.append(
                    {
                        "path": f.relative_to(plugin_dir).as_posix(),
                        "size": f.stat().st_size,
                    }
                )

    # Owner display name.
    owner_row = conn.execute(
        "SELECT name, email FROM users WHERE id = ?", [entity["owner_user_id"]]
    ).fetchone()
    owner_display = (owner_row[0] or owner_row[1]) if owner_row else entity["owner_username"]

    is_installed = UserStoreInstallsRepository(conn).is_installed(
        user["id"], entity_id
    )
    is_owner = entity["owner_user_id"] == user["id"]
    # Admin can also Edit/Delete (parity with the API: store.py guards both
    # mutations on owner OR admin). Without this the store_detail buttons
    # would be hidden from admin even though they have authority.
    is_admin = is_user_admin(user["id"], conn)

    ctx = _build_context(
        request,
        user=user,
        entity=entity,
        invocation_name=suffixed_name(entity["name"], entity["owner_username"]),
        owner_display=owner_display,
        files=files,
        is_installed=is_installed,
        is_owner=is_owner,
        is_admin=is_admin,
    )
    return templates.TemplateResponse(request, "store_detail.html", ctx)


@router.get("/my-ai-stack", response_class=HTMLResponse)
async def my_ai_stack_page(
    request: Request,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "my_ai_stack.html", ctx)


# ---------------------------------------------------------------------------
# Marketplace — unified browse + detail pages.
# ---------------------------------------------------------------------------


@router.get("/marketplace", response_class=HTMLResponse)
async def marketplace_listing(
    request: Request,
    user: dict = Depends(get_current_user),
):
    import json as _json
    from src.category_icons import all_paths
    ctx = _build_context(
        request, user=user,
        category_icons_json=_json.dumps(all_paths()),
    )
    return templates.TemplateResponse(request, "marketplace.html", ctx)


@router.get("/marketplace/flea/{entity_id}", response_class=HTMLResponse)
async def marketplace_flea_detail(
    request: Request,
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Pick the right detail template based on the entity type:
    plugins reuse the unified plugin layout; skills / agents render the
    item-detail layout (matches curated nested skill / agent)."""
    from src.repositories.store_entities import StoreEntitiesRepository
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if entity["type"] == "plugin":
        ctx = _build_context(
            request, user=user,
            source="flea",
            entity_id=entity_id,
            plugin_name=entity["name"],
        )
        return templates.TemplateResponse(
            request, "marketplace_plugin_detail.html", ctx,
        )

    ctx = _build_context(
        request, user=user,
        source="flea",
        kind=entity["type"],
        entity_id=entity_id,
        item_name=entity["name"],
    )
    return templates.TemplateResponse(
        request, "marketplace_item_detail.html", ctx,
    )


@router.get(
    "/marketplace/curated/{marketplace_id}/{plugin_name}",
    response_class=HTMLResponse,
)
async def marketplace_curated_detail(
    request: Request,
    marketplace_id: str,
    plugin_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Server-renders only the shell — the page hydrates via
    ``GET /api/marketplace/curated/{slug}/{plugin}`` which carries the
    real RBAC guard. Direct URL access for users without the grant lands on
    a shell that 403s on the first XHR; UX-level the page renders an empty
    state and a back link."""
    ctx = _build_context(
        request,
        user=user,
        source="curated",
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
    )
    return templates.TemplateResponse(
        request, "marketplace_plugin_detail.html", ctx,
    )


@router.get(
    "/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}",
    response_class=HTMLResponse,
)
async def marketplace_curated_skill_detail(
    request: Request,
    marketplace_id: str,
    plugin_name: str,
    skill_name: str,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(
        request,
        user=user,
        source="curated",
        kind="skill",
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        inner_name=skill_name,
    )
    return templates.TemplateResponse(
        request, "marketplace_item_detail.html", ctx,
    )


@router.get(
    "/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}",
    response_class=HTMLResponse,
)
async def marketplace_curated_agent_detail(
    request: Request,
    marketplace_id: str,
    plugin_name: str,
    agent_name: str,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(
        request,
        user=user,
        source="curated",
        kind="agent",
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        inner_name=agent_name,
    )
    return templates.TemplateResponse(
        request, "marketplace_item_detail.html", ctx,
    )


@router.get("/marketplace/guide/curated", response_class=HTMLResponse)
async def marketplace_guide_curated(
    request: Request,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(
        request, user=user,
        guide_title="Submit a plugin to Curated Marketplace",
        guide_kind="curated",
    )
    return templates.TemplateResponse(request, "marketplace_guide.html", ctx)


@router.get("/marketplace/guide/flea", response_class=HTMLResponse)
async def marketplace_guide_flea(
    request: Request,
    user: dict = Depends(get_current_user),
):
    ctx = _build_context(
        request, user=user,
        guide_title="Upload to Flea Market",
        guide_kind="flea",
    )
    return templates.TemplateResponse(request, "marketplace_guide.html", ctx)


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


# Scheduler-driven admin actions audited by app/api/admin.py and
# app/api/marketplaces.py. Keep in sync with the JOBS list in
# services/scheduler/__main__.py.
#
# `data-refresh` (POST /api/sync/trigger) and `script-runner`
# (POST /api/scripts/run-due) are scheduler jobs but they do NOT write
# audit_log today, so they can't appear here. If you add audit calls to
# those endpoints, add the matching action strings to this list.
SCHEDULER_AUDIT_ACTIONS = [
    "run_session_collector",
    "run_verification_detector",
    "run_corporate_memory",
    "marketplace.sync_all",
]


@router.get("/admin/scheduler-runs", response_class=HTMLResponse)
async def admin_scheduler_runs_page(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Read-only view of the audit_log filtered to scheduler-driven actions.

    Failed scheduler ticks (HTTP 401, network errors) don't reach this view —
    they live only in the scheduler container's stdout. The audit_log shows
    only what reached the admin endpoint and was processed.
    """
    from src.repositories.audit import AuditRepository

    rows = AuditRepository(conn).query_actions(SCHEDULER_AUDIT_ACTIONS, limit=200)
    ctx = _build_context(request, user=user, rows=rows, actions=SCHEDULER_AUDIT_ACTIONS)
    return templates.TemplateResponse(request, "admin_scheduler_runs.html", ctx)


@router.get("/admin/agent-prompt", response_class=HTMLResponse)
async def admin_agent_prompt_page(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.repositories.welcome_template import WelcomeTemplateRepository
    from src.welcome_template import compute_default_agent_prompt

    row = WelcomeTemplateRepository(conn).get()
    base_url = str(request.base_url).rstrip("/")
    default_template = compute_default_agent_prompt(conn, user=user, server_url=base_url)
    ctx = _build_context(
        request,
        user=user,
        current=row["content"] or "",
        default_template=default_template,
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
        is_override=row["content"] is not None,
    )
    return templates.TemplateResponse(request, "admin_welcome.html", ctx)


@router.get("/admin/workspace-prompt", response_class=HTMLResponse)
async def admin_workspace_prompt_page(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.repositories.claude_md_template import ClaudeMdTemplateRepository
    from src.claude_md import compute_default_claude_md
    from app.api.claude_md import _scan_legacy_strings

    row = ClaudeMdTemplateRepository(conn).get()
    server_url = str(request.base_url).rstrip("/")
    default_template = compute_default_claude_md(conn, user=user, server_url=server_url)
    ctx = _build_context(
        request,
        user=user,
        current=row["content"] or "",
        default_template=default_template,
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
        is_override=row["content"] is not None,
        legacy_strings_detected=_scan_legacy_strings(row["content"] or ""),
    )
    return templates.TemplateResponse(request, "admin_workspace_prompt.html", ctx)



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
        """SELECT g.id, g.name, g.description, g.is_system, g.created_by,
                  m.source, m.added_at
           FROM user_group_members m
           JOIN user_groups g ON g.id = m.group_id
           WHERE m.user_id = ?
           ORDER BY g.is_system DESC, g.name""",
        [user["id"]],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    memberships = [dict(zip(cols, r)) for r in rows]
    # Project the same chip metadata the /admin/users/{id} page derives:
    # origin (single source of truth via app.api.access._derive_origin),
    # plus a display_name that shortens raw Workspace emails for
    # google_sync rows (`grp_acme_legal@workspace.example.com` → `Legal`). The
    # Jinja template just renders these without env lookups.
    from app.api.access import _derive_origin
    prefix = os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip().lower()
    for m in memberships:
        m["origin"] = _derive_origin(m)
        if m["origin"] == "google_sync" and m["name"] and m["name"] not in ("Admin", "Everyone"):
            local = m["name"].split("@", 1)[0]
            if prefix and local.lower().startswith(prefix):
                local = local[len(prefix):]
            local = local.lstrip("_- \t")
            if not local:
                local = m["name"].split("@", 1)[0]
            m["display_name"] = local[:1].upper() + local[1:]
        else:
            m["display_name"] = m["name"]

    ctx = _build_context(
        request,
        user=user,
        memberships=memberships,
        is_admin=is_user_admin(user["id"], conn),
    )
    return templates.TemplateResponse(request, "profile.html", ctx)


@router.get("/profile/sessions", response_class=HTMLResponse)
async def profile_sessions_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """User-self-view of own uploaded sessions and their extraction state.

    Walks `${DATA_DIR}/user_sessions/<user_id>/*.jsonl` for the caller's
    own user_id, joins each file against `session_extraction_state` to
    surface processed_at + items_extracted, and renders a table.
    Items_extracted = 0 means the verification_detector ran but the LLM
    found no claims worth tracking — that's the documented "no items"
    outcome; it does NOT mean the pipeline is broken.
    """
    import pathlib
    user_id = user["id"]
    data_dir = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
    user_sessions_dir = data_dir / "user_sessions" / user_id

    files = []
    if user_sessions_dir.is_dir():
        # Stat once per file with OSError tolerance, THEN sort. The previous
        # `sorted(..., key=lambda p: p.stat().st_mtime)` raised on any
        # transient stat failure (race with delete, permission flicker) and
        # 500-ed the whole page (Devin Review on #179).
        statted = []
        for jsonl in user_sessions_dir.glob("*.jsonl"):
            try:
                stat = jsonl.stat()
            except OSError:
                continue
            statted.append((jsonl, stat))
        statted.sort(key=lambda pair: pair[1].st_mtime, reverse=True)
        for jsonl, stat in statted:
            files.append({
                "name": jsonl.name,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            })

    state_map: dict = {}
    if files:
        keys = [f"{user_id}/{f['name']}" for f in files]
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"""SELECT session_file, processed_at, items_extracted, file_hash
                FROM session_extraction_state
                WHERE session_file IN ({placeholders})""",
            keys,
        ).fetchall()
        cols = [d[0] for d in conn.description]
        for row in rows:
            d = dict(zip(cols, row))
            state_map[d["session_file"]] = d

    rows_view = []
    for f in files:
        key = f"{user_id}/{f['name']}"
        state = state_map.get(key)
        rows_view.append({
            "name": f["name"],
            "size_kb": round(f["size_bytes"] / 1024, 1),
            "uploaded_at": f["mtime"],
            "processed_at": state["processed_at"] if state else None,
            "items_extracted": state["items_extracted"] if state else None,
            "is_processed": state is not None,
        })

    ctx = _build_context(
        request,
        user=user,
        sessions=rows_view,
        user_id=user_id,
    )
    return templates.TemplateResponse(request, "profile_sessions.html", ctx)


@router.get("/profile/sessions/{filename}")
async def profile_session_download(
    filename: str,
    user: dict = Depends(get_current_user),
):
    """Download a single jsonl session file owned by the caller.

    Path safety: filename is single-component (no separators, no `..`,
    must end in `.jsonl`); the served path is built under
    `${DATA_DIR}/user_sessions/<current_user.id>/` and must resolve into
    that directory. Any deviation yields 404 — never 403, so we don't
    leak the existence of files belonging to other users.
    """
    import pathlib

    if "/" in filename or "\\" in filename or filename.startswith(".") or ".." in filename:
        raise HTTPException(status_code=404, detail="Not found")
    if not filename.endswith(".jsonl"):
        raise HTTPException(status_code=404, detail="Not found")

    user_id = user["id"]
    data_dir = pathlib.Path(os.environ.get("DATA_DIR", "/data")).resolve()
    user_dir = (data_dir / "user_sessions" / user_id).resolve()
    target = (user_dir / filename).resolve()

    try:
        target.relative_to(user_dir)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    return FileResponse(
        path=str(target),
        filename=filename,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/_debug/throw/http/{code:int}", response_class=HTMLResponse, include_in_schema=False)
async def _debug_throw_http(request: Request, code: int):
    """Dev helper — raise an HTTPException with the given status code.

    Only mounted when DEBUG=1 (gated below). Lets you eyeball the error
    page chrome + debug-toolbar panels for any HTTP status code:
      /_debug/throw/http/404  → 404 page
      /_debug/throw/http/418  → 418 page (custom title falls back to "Error")
      /_debug/throw/http/500  → 500 page rendered via the StarletteHTTPException
                                handler (NOT the unhandled-exception handler —
                                use /_debug/throw/exc for that)
    """
    if not _is_debug():
        raise HTTPException(status_code=404, detail="Not found")
    raise HTTPException(status_code=code, detail=f"Forced {code} via /_debug/throw/http/{code}")


@router.get("/_debug/throw/exc", response_class=HTMLResponse, include_in_schema=False)
async def _debug_throw_exc(request: Request):
    """Dev helper — raise an unhandled exception to exercise the 500 path."""
    if not _is_debug():
        raise HTTPException(status_code=404, detail="Not found")
    # Force a real traceback so the DEBUG-only `<details>Traceback</details>`
    # block in error.html shows something interesting (not just "RuntimeError").
    payload = {"a": 1}
    return payload["nope"]  # KeyError with a useful traceback


def _is_debug() -> bool:
    return os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


@router.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def _catch_all_404(request: Request, full_path: str):
    """Catch-all 404 for unmatched routes.

    Provides a matched route so fastapi-debug-toolbar can inject its panels —
    the toolbar bails out of injection when ``matched_route(request)`` is None
    (the case on truly unrouted paths). The actual rendering is delegated to
    ``app.main._html_auth_redirect_handler`` via the raised ``HTTPException``,
    which routes API paths to JSON and HTML paths to the ``error.html``
    template.
    """
    raise HTTPException(status_code=404, detail="Page not found")
