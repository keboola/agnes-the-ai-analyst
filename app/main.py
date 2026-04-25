"""FastAPI main application — unified server for web UI + API."""

import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from urllib.parse import quote

import os


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


class _SelectiveGZipMiddleware:
    """GZipMiddleware wrapper that skips a set of path prefixes.

    Parquet-serving endpoints send responses that are already columnar-
    compressed (parquet's internal codec) and — for /api/data — can reach
    hundreds of MB. Gzipping them on the way out costs CPU and latency with
    no meaningful size reduction. Skip those paths; every other endpoint
    (JSON manifests, HTML previews, install.sh) still gets compressed.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1024, skip_prefixes: tuple[str, ...] = ()) -> None:
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
from app.api.admin import router as admin_router
from app.api.permissions import router as permissions_router
from app.api.access_requests import router as access_requests_router
from app.api.jira_webhooks import router as jira_webhooks_router
from app.api.metrics import router as metrics_router
from app.api.metadata import router as metadata_router
from app.api.query_hybrid import router as query_hybrid_router
from app.api.cli_artifacts import router as cli_artifacts_router
from app.api.tokens import router as tokens_router, admin_router as tokens_admin_router
from app.web.router import router as web_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    yield
    from src.db import close_system_db
    close_system_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Data Analyst",
        description="Data distribution platform for AI analytical systems",
        version=_app_version(),
        lifespan=lifespan,
    )

    # Compress JSON / HTML responses on the wire. Parquet downloads are
    # excluded — they're already columnar-compressed and re-gzipping them
    # just burns CPU with no size win. minimum_size=1024 keeps tiny
    # responses uncompressed too (cheaper than the header overhead).
    app.add_middleware(
        _SelectiveGZipMiddleware,
        minimum_size=1024,
        skip_prefixes=("/api/data/", "/cli/wheel/", "/cli/download"),
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

    # Seed admin user for testing/CI (when SEED_ADMIN_EMAIL is set) OR for local dev.
    # Optional: SEED_ADMIN_PASSWORD sets password_hash on first seed so the user
    # can log in immediately without bootstrap. Only applied if the user has no
    # password_hash yet — never overwrites an existing password.
    seed_email = os.environ.get("SEED_ADMIN_EMAIL") or (get_local_dev_email() if is_local_dev_mode() else None)
    if seed_email:
        try:
            from src.db import get_system_db
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
                repo.create(
                    id=str(uuid.uuid4()),
                    email=seed_email,
                    name="Admin",
                    role="admin",
                    password_hash=password_hash,
                )
                logger.info("Seeded admin user: %s (password=%s)", seed_email, "yes" if password_hash else "no")
            elif password_hash and not existing.get("password_hash"):
                repo.update(id=existing["id"], password_hash=password_hash, role="admin")
                logger.info("Set password on existing seed admin: %s", seed_email)
            conn.close()
        except Exception as e:
            logger.warning(f"Could not seed admin: {e}")

    # Sync internal-role registry into DB. Modules call register_internal_role()
    # at import time; this hook reconciles the registry into the internal_roles
    # table so the mapping UI has something to show. Idempotent — safe to run
    # on every startup.
    try:
        from app.auth.role_resolver import (
            sync_registered_roles_to_db, list_registered_roles,
        )
        from src.db import get_system_db
        conn = get_system_db()
        try:
            sync_registered_roles_to_db(conn)
        finally:
            conn.close()
        registered = list_registered_roles()
        if registered:
            logger.info(
                "internal_roles registered: %s",
                ", ".join(s.key for s in registered),
            )
    except Exception as e:
        logger.warning("internal_roles sync failed at startup: %s", e)

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
    app.include_router(permissions_router)
    app.include_router(access_requests_router)
    app.include_router(jira_webhooks_router)
    app.include_router(metrics_router)
    app.include_router(metadata_router)
    app.include_router(query_hybrid_router)
    app.include_router(cli_artifacts_router)
    app.include_router(tokens_router)
    app.include_router(tokens_admin_router)

    # Web UI router (must be last — has catch-all routes)
    app.include_router(web_router)

    @app.exception_handler(StarletteHTTPException)
    async def _html_auth_redirect_handler(request, exc: StarletteHTTPException):
        """Redirect unauthenticated HTML page loads (GET) to /login.

        Only GET requests outside `/api/` and `/auth/` are redirected — that
        targets browser navigations to HTML pages. POSTs, API prefixes, and
        non-401 errors fall through to Starlette's default JSON response so
        JSON clients (including `/auth/tokens` for PAT CRUD) keep their
        existing contract.
        """
        if (
            exc.status_code == 401
            and request.method == "GET"
            and not request.url.path.startswith(("/api/", "/auth/"))
        ):
            next_param = quote(request.url.path, safe="")
            return RedirectResponse(url=f"/login?next={next_param}", status_code=302)
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, exc)

    return app


app = create_app()
