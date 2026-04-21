"""FastAPI main application — unified server for web UI + API."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

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
        version="2.0.0",
        lifespan=lifespan,
    )

    # Session middleware (required for OAuth state)
    from app.secrets import get_session_secret
    session_secret = get_session_secret()
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

    # Startup banner
    from src.db import SCHEMA_VERSION
    logger.info(
        "Agnes %s | channel: %s | schema v%s",
        os.environ.get("AGNES_VERSION", "dev"),
        os.environ.get("RELEASE_CHANNEL", "dev"),
        SCHEMA_VERSION,
    )

    # Seed admin user for testing/CI (when SEED_ADMIN_EMAIL is set).
    # Optional: SEED_ADMIN_PASSWORD sets password_hash on first seed so the user
    # can log in immediately without bootstrap. Only applied if the user has no
    # password_hash yet — never overwrites an existing password.
    seed_email = os.environ.get("SEED_ADMIN_EMAIL")
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

    # Web UI router (must be last — has catch-all routes)
    app.include_router(web_router)

    return app


app = create_app()
