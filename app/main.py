"""FastAPI main application — unified server for web UI + API."""

# Silence authlib's internal forward-compat note. Authlib emits an
# AuthlibDeprecationWarning from its own _joserfc_helpers when our
# `from authlib.integrations.starlette_client import OAuth` import
# touches `authlib.jose` paths. The warning is upstream-internal — it's
# telling authlib to migrate to joserfc before its 2.0; it's not
# actionable on our side until either authlib ships the fix or we
# rewrite OAuth handling on top of joserfc directly. Filtering here
# (before authlib gets imported transitively) keeps `make local-dev`
# stdout clean without hiding warnings from any other package.
import warnings as _warnings
try:
    from authlib.deprecate import AuthlibDeprecationWarning as _AuthlibDepr
    _warnings.filterwarnings("ignore", category=_AuthlibDepr)
except ImportError:
    # authlib too old / class moved — fall back to message-based match
    # so the filter still keeps startup clean.
    _warnings.filterwarnings(
        "ignore",
        message=r"authlib\.jose module is deprecated.*",
    )

import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from urllib.parse import quote

import os

# Initialise structured logging BEFORE any module that emits logs at import
# time. setup_logging is idempotent and safe to call once at process start.
from app.logging_config import setup_logging

setup_logging("app")


def _app_version() -> str:
    """Product version for FastAPI title / OpenAPI schema.

    Single source of truth is `pyproject.toml` `[project].version`; we read
    it back via `importlib.metadata` at runtime so `/docs`, `/openapi.json`,
    `/api/version`, `/cli/latest`, and `da --version` can never drift.
    """
    try:
        return _pkg_version("agnes-the-ai-analyst")
    except PackageNotFoundError:
        return "dev"

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.middleware.request_id import RequestIdMiddleware


class _SelectiveGZipMiddleware:
    """GZipMiddleware wrapper that skips a set of path prefixes.

    Parquet-serving endpoints send responses that are already columnar-
    compressed (parquet's internal codec) and — for /api/data — can reach
    hundreds of MB. Gzipping them on the way out costs CPU and latency with
    no meaningful size reduction. Skip those paths; every other endpoint
    (JSON manifests, HTML previews, install.sh) still gets compressed.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1024, skip_prefixes: tuple[str, ...] = ()) -> None:
        # `self.app` is the Starlette middleware convention — outer middleware
        # (e.g. fastapi-debug-toolbar's APIRouter walker) traverses the chain
        # via `.app` to find the inner FastAPI app. Keep `_raw` as the public
        # alias used by our own __call__ for the skip-path branch.
        self.app = app
        self._raw = app
        self._gzip = GZipMiddleware(app, minimum_size=minimum_size)
        self._skip_prefixes = skip_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if any(path.startswith(p) for p in self._skip_prefixes):
                await self._raw(scope, receive, send)
                return
        await self._gzip(scope, receive, send)

from app.auth.router import router as auth_router
from app.api.health import router as health_router
from app.api.sync import router as sync_router
from app.api.data import router as data_router
from app.api.query import router as query_router
from app.api.users import router as users_router
from app.api.memory import router as memory_router
from app.api.upload import router as upload_router
from app.api.scripts import router as scripts_router
from app.api.settings import router as settings_router
from app.api.catalog import router as catalog_router
from app.api.telegram import router as telegram_router
from app.api.access import router as access_router, me_router as me_access_router
from app.api.me_debug import router as me_debug_router
from app.api.admin import router as admin_router
from app.api.permissions import router as permissions_router
from app.api.access_requests import router as access_requests_router
from app.api.jira_webhooks import router as jira_webhooks_router
from app.api.metrics import router as metrics_router
from app.api.metadata import router as metadata_router
from app.api.query_hybrid import router as query_hybrid_router
from app.api.cli_artifacts import router as cli_artifacts_router
from app.api.tokens import router as tokens_router, admin_router as tokens_admin_router
from app.api.v2_catalog import router as v2_catalog_router
from app.api.v2_schema import router as v2_schema_router
from app.api.v2_sample import router as v2_sample_router
from app.api.v2_scan import router as v2_scan_router
from app.api.marketplaces import router as marketplaces_router
from app.marketplace_server.router import router as marketplace_server_router
from app.marketplace_server.git_router import make_git_wsgi_app
from app.web.router import router as web_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    # Issue #81 Group A — log the effective remote_attach allowlist at
    # startup so an operator's typo in AGNES_REMOTE_ATTACH_EXTENSIONS
    # (which REPLACES, not extends, the default) is visible.
    try:
        from src.orchestrator_security import log_effective_policy
        log_effective_policy()
    except Exception:
        pass  # never block startup on a logging convenience
    yield
    from src.db import close_system_db
    close_system_db()


DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Data Analyst",
        description="Data distribution platform for AI analytical systems",
        version=_app_version(),
        lifespan=lifespan,
        debug=DEBUG,
    )

    # Compress JSON / HTML responses on the wire. Parquet downloads are
    # excluded — they're already columnar-compressed and re-gzipping them
    # just burns CPU with no size win. minimum_size=1024 keeps tiny
    # responses uncompressed too (cheaper than the header overhead).
    app.add_middleware(
        _SelectiveGZipMiddleware,
        minimum_size=1024,
        skip_prefixes=(
            "/api/data/",
            "/cli/wheel/",
            "/cli/download",
            "/marketplace.git",  # git smart-HTTP is self-chunked; double-gzip bloats
        ),
    )

    # Session middleware (required for OAuth state)
    from app.secrets import get_session_secret
    session_secret = get_session_secret()
    if len(session_secret) < 32:
        # Same gate JWT applies (app/auth/jwt.py:_get_secret_key) — keeps the
        # two HMAC surfaces consistent. session_internal_roles + google_groups
        # are trusted off the cookie signature; a weak SESSION_SECRET means
        # those gates are weak too.
        import warnings as _warnings
        _warnings.warn(
            f"SESSION_SECRET is {len(session_secret)} chars — minimum 32 recommended",
            UserWarning, stacklevel=2,
        )
    app.add_middleware(SessionMiddleware, secret_key=session_secret)

    # CORS for CLI and external clients
    cors_origins = os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # RequestIdMiddleware mounted LAST — Starlette inserts middleware at
    # index 0, so the last add_middleware call ends up OUTERMOST and runs
    # FIRST per request. The request_id ContextVar is set before any
    # downstream middleware or handler runs, and every response gets the
    # x-request-id header.
    app.add_middleware(RequestIdMiddleware)

    # FastAPI debug toolbar — only when DEBUG=1 in env. Injects per-request
    # HTML overlay (headers, routes, timer, profiling, logs) on any HTML
    # response; harmless on JSON. Inner try/except is for the import only:
    # if a developer sets DEBUG=1 without installing dev deps, log a warning
    # instead of crashing. The middleware mount itself fails loud if broken.
    if DEBUG:
        try:
            from debug_toolbar.middleware import DebugToolbarMiddleware
            # debug_toolbar.middleware splats **kwargs into DebugToolbarSettings
            # (a pydantic-settings model with case-insensitive UPPERCASE fields).
            # Pass field names as kwargs to add_middleware — `panels` becomes
            # `PANELS`, etc. Do NOT wrap them in a `settings={...}` dict —
            # that hits the model's actual `SETTINGS` field (Sequence[BaseSettings])
            # and fails validation. Field reference:
            # https://github.com/mongkok/fastapi-debug-toolbar/blob/master/debug_toolbar/settings.py
            # ProfilingPanel (pyinstrument) is intentionally omitted: it
            # raises "There is already a profiler running" under uvicorn's
            # async context because pyinstrument's stack sampler can't be
            # nested per task. Re-enable per-developer if you really want it
            # via env override; the rest of the panels are async-safe.
            app.add_middleware(
                DebugToolbarMiddleware,
                panels=[
                    "debug_toolbar.panels.headers.HeadersPanel",
                    "debug_toolbar.panels.routes.RoutesPanel",
                    "debug_toolbar.panels.settings.SettingsPanel",
                    "debug_toolbar.panels.versions.VersionsPanel",
                    "debug_toolbar.panels.timer.TimerPanel",
                    "debug_toolbar.panels.logging.LoggingPanel",
                ],
            )
        except ImportError:
            logger.warning(
                "DEBUG=1 but fastapi-debug-toolbar not installed; toolbar disabled",
            )

    # Load .env_overlay (persisted by /api/admin/configure)
    _overlay = Path(os.environ.get("DATA_DIR", "./data")) / "state" / ".env_overlay"
    if _overlay.exists():
        for line in _overlay.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    # Load instance config on startup
    try:
        from app.instance_config import load_instance_config
        load_instance_config()
        logger.info("Instance config loaded")
    except Exception as e:
        logger.warning(f"Could not load instance config: {e}")

    # Configure confidence scoring from instance config (corporate_memory.confidence section)
    try:
        from app.instance_config import get_corporate_memory_config
        from services.corporate_memory.confidence import configure as configure_confidence
        cm_config = get_corporate_memory_config()
        if cm_config and "confidence" in cm_config:
            configure_confidence(cm_config["confidence"])
            logger.info("Corporate memory confidence config applied")
    except Exception as e:
        logger.warning(f"Could not configure corporate memory confidence: {e}")

    # Startup banner
    from src.db import SCHEMA_VERSION
    logger.info(
        "Agnes %s | channel: %s | schema v%s",
        os.environ.get("AGNES_VERSION", "dev"),
        os.environ.get("RELEASE_CHANNEL", "dev"),
        SCHEMA_VERSION,
    )

    # LOCAL_DEV_MODE: bypass authentication for local development. DO NOT enable in prod.
    # When on, every protected route auto-logs in as a seeded admin user (default dev@localhost).
    from app.auth.dependencies import (
        is_local_dev_mode, get_local_dev_email, get_local_dev_groups,
    )
    if is_local_dev_mode():
        logger.warning("=" * 60)
        logger.warning("LOCAL_DEV_MODE is ON — authentication is bypassed.")
        logger.warning("All requests auto-authenticate as: %s", get_local_dev_email())
        # Validate + report LOCAL_DEV_GROUPS at startup so a malformed JSON
        # value gets surfaced loudly here instead of silently warning on the
        # first authenticated request. Empty when unset is fine — just say so.
        raw_groups_env = os.environ.get("LOCAL_DEV_GROUPS", "").strip()
        mocked_groups = get_local_dev_groups()
        if raw_groups_env and not mocked_groups:
            logger.warning(
                "LOCAL_DEV_GROUPS is set but produced no valid groups — "
                "check the WARNING above for the parse error.",
            )
        elif mocked_groups:
            logger.warning(
                "LOCAL_DEV_GROUPS: mocking %d group(s) into session: %s",
                len(mocked_groups),
                ", ".join(g["id"] for g in mocked_groups),
            )
        else:
            logger.warning("LOCAL_DEV_GROUPS is unset — session.google_groups will be empty.")
        logger.warning("NEVER enable this in a deployment reachable from the internet.")
        logger.warning("=" * 60)

    # Seed admin user (SEED_ADMIN_EMAIL) and add them to the Admin user_group.
    # Optional SEED_ADMIN_PASSWORD lets the seeded user sign in immediately
    # without going through bootstrap; never overwritten if already set.
    # The Admin/Everyone user_groups themselves are seeded inside
    # _ensure_schema (src.db._seed_system_groups), so this hook only has to
    # handle membership for the seed admin.
    seed_email = os.environ.get("SEED_ADMIN_EMAIL") or (get_local_dev_email() if is_local_dev_mode() else None)
    if seed_email:
        try:
            from src.db import SYSTEM_ADMIN_GROUP, get_system_db
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.users import UserRepository
            conn = get_system_db()
            repo = UserRepository(conn)
            seed_password = os.environ.get("SEED_ADMIN_PASSWORD") or None
            password_hash = None
            if seed_password:
                from argon2 import PasswordHasher
                password_hash = PasswordHasher().hash(seed_password)
            existing = repo.get_by_email(seed_email)
            if not existing:
                import uuid
                user_id = str(uuid.uuid4())
                repo.create(
                    id=user_id,
                    email=seed_email,
                    name="Admin",
                    role="admin",
                    password_hash=password_hash,
                )
                logger.info("Seeded admin user: %s (password=%s)", seed_email, "yes" if password_hash else "no")
            else:
                user_id = existing["id"]
                if password_hash and not existing.get("password_hash"):
                    repo.update(id=user_id, password_hash=password_hash)
                    logger.info("Set password on existing seed admin: %s", seed_email)
            # Make sure the seed admin is actually in the Admin group — this
            # is what gives them admin access in v12. Idempotent.
            admin_group = conn.execute(
                "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP],
            ).fetchone()
            if admin_group:
                UserGroupMembersRepository(conn).add_member(
                    user_id=user_id,
                    group_id=admin_group[0],
                    source="system_seed",
                    added_by="app.main:seed_admin",
                )
            conn.close()
        except Exception as e:
            logger.warning(f"Could not seed admin: {e}")

    # Seed the synthetic scheduler user when SCHEDULER_API_TOKEN is configured,
    # so the very first cron tick after a fresh deploy already has a valid
    # actor to attribute audit-log entries to. The lazy seed in
    # `app.auth.scheduler_token.get_scheduler_user` covers the case where the
    # secret is rotated mid-life, but doing it here keeps startup observable.
    from app.auth.scheduler_token import get_scheduler_secret
    if get_scheduler_secret():
        try:
            from app.auth.scheduler_token import (
                SCHEDULER_TOKEN_MIN_LENGTH,
                ensure_scheduler_user,
            )
            from src.db import get_system_db
            secret = get_scheduler_secret()
            if len(secret) < SCHEDULER_TOKEN_MIN_LENGTH:
                logger.warning(
                    "SCHEDULER_API_TOKEN is set but only %d chars — auth path"
                    " disabled (minimum %d). Generate a longer secret in .env.",
                    len(secret), SCHEDULER_TOKEN_MIN_LENGTH,
                )
            else:
                conn = get_system_db()
                try:
                    ensure_scheduler_user(conn)
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"Could not seed scheduler user: {e}")

    # C8: Warn when no user has a password_hash — bootstrap endpoint is open.
    # This is intentional UX (operator can claim seed admin), but the open
    # window should be visible in startup logs so it's not forgotten.
    if not is_local_dev_mode():
        try:
            from src.db import get_system_db
            from src.repositories.users import UserRepository
            conn = get_system_db()
            repo = UserRepository(conn)
            all_users = repo.list_all()
            has_password = any(u.get("password_hash") for u in all_users)
            if not has_password:
                logger.warning(
                    "No user has a password set — /auth/bootstrap is reachable. "
                    "Claim the seed admin (or set SEED_ADMIN_PASSWORD) to close this window."
                )
            conn.close()
        except Exception:
            pass  # never block startup on a logging convenience

    # Static files
    static_dir = Path(__file__).parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Auth providers (conditional registration)
    from app.auth.providers.google import router as google_auth_router, is_available as google_available
    from app.auth.providers.password import router as password_auth_router
    from app.auth.providers.email import router as email_auth_router, is_available as email_available

    # API routers
    app.include_router(auth_router)
    app.include_router(google_auth_router)
    app.include_router(password_auth_router)
    app.include_router(email_auth_router)  # Always register, check availability per-request
    app.include_router(health_router)
    app.include_router(sync_router)
    app.include_router(data_router)
    app.include_router(query_router)
    app.include_router(users_router)
    app.include_router(memory_router)
    app.include_router(upload_router)
    app.include_router(scripts_router)
    app.include_router(settings_router)
    app.include_router(catalog_router)
    app.include_router(telegram_router)
    app.include_router(admin_router)
    app.include_router(access_router)
    app.include_router(me_access_router)
    app.include_router(me_debug_router)
    app.include_router(permissions_router)
    app.include_router(access_requests_router)
    app.include_router(jira_webhooks_router)
    app.include_router(metrics_router)
    app.include_router(metadata_router)
    app.include_router(query_hybrid_router)
    app.include_router(cli_artifacts_router)
    app.include_router(tokens_router)
    app.include_router(tokens_admin_router)
    app.include_router(v2_catalog_router)
    app.include_router(v2_schema_router)
    app.include_router(v2_sample_router)
    app.include_router(v2_scan_router)
    app.include_router(marketplaces_router)
    app.include_router(marketplace_server_router)

    # Git smart-HTTP endpoint for Claude Code: /marketplace.git/*
    # WSGI → ASGI bridge (dulwich is WSGI-native; FastAPI is ASGI).
    from a2wsgi import WSGIMiddleware
    app.mount("/marketplace.git", WSGIMiddleware(make_git_wsgi_app()))

    # Web UI router (must be last — has catch-all routes)
    app.include_router(web_router)

    # Paths served as API responses (JSON / ZIP / git smart-HTTP) — never
    # redirect a 401 here to the HTML login page; clients expect the raw 401.
    _API_PATH_PREFIXES: tuple[str, ...] = (
        "/api/",
        "/auth/",
        "/marketplace.zip",
        "/marketplace.git",
        "/marketplace/",
    )

    @app.exception_handler(StarletteHTTPException)
    async def _html_auth_redirect_handler(request, exc: StarletteHTTPException):
        """Redirect unauthenticated HTML page loads (GET) to /login.

        Only GET requests outside the API prefixes are redirected — that
        targets browser navigations to HTML pages. POSTs, API prefixes, and
        non-401 errors fall through to Starlette's default JSON response so
        JSON clients (including `/auth/tokens` for PAT CRUD and
        `/marketplace.zip` consumed by Claude Code) keep their existing
        contract.
        """
        if (
            exc.status_code == 401
            and request.method == "GET"
            and not request.url.path.startswith(_API_PATH_PREFIXES)
        ):
            next_param = quote(request.url.path, safe="")
            return RedirectResponse(url=f"/login?next={next_param}", status_code=302)
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, exc)

    return app


app = create_app()
