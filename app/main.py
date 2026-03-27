"""FastAPI main application — unified server for web UI + API."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Data Analyst",
        description="Data distribution platform for AI analytical systems",
        version="2.0.0",
    )

    # CORS for CLI and web UI
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers
    app.include_router(auth_router)
    app.include_router(health_router)
    app.include_router(sync_router)
    app.include_router(data_router)
    app.include_router(query_router)
    app.include_router(users_router)
    app.include_router(memory_router)
    app.include_router(upload_router)
    app.include_router(scripts_router)
    app.include_router(settings_router)

    return app


app = create_app()
