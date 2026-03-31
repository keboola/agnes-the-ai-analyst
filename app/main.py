"""FastAPI main application — unified server for web UI + API."""

import logging
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
from app.web.router import router as web_router

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Data Analyst",
        description="Data distribution platform for AI analytical systems",
        version="2.0.0",
    )

    # Session middleware (required for OAuth state)
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("JWT_SECRET_KEY", "dev-session-secret"),
    )

    # CORS for CLI and external clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Load instance config on startup
    try:
        from app.instance_config import load_instance_config
        load_instance_config()
        logger.info("Instance config loaded")
    except Exception as e:
        logger.warning(f"Could not load instance config: {e}")

    # Seed admin user for testing/CI (when SEED_ADMIN_EMAIL is set)
    seed_email = os.environ.get("SEED_ADMIN_EMAIL")
    if seed_email:
        try:
            from src.db import get_system_db
            from src.repositories.users import UserRepository
            conn = get_system_db()
            repo = UserRepository(conn)
            if not repo.get_by_email(seed_email):
                import uuid
                repo.create(id=str(uuid.uuid4()), email=seed_email, name="Admin", role="admin")
                logger.info("Seeded admin user: %s", seed_email)
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

    # Web UI router (must be last — has catch-all routes)
    app.include_router(web_router)

    return app


app = create_app()
