"""Marketplace distribution endpoints.

Three endpoints plus a WSGI mount:
- GET /api/marketplace/info - JSON describing the caller's allowed plugins
- GET /api/marketplace/zip  - deterministic filtered ZIP
- /api/marketplace/git/*    - git smart-HTTP (WSGI, mounted by app.main)

Import `router` for the FastAPI handlers, `make_git_wsgi_app()` for the mount.
"""
from fastapi import APIRouter

from app.api.marketplace.info import router as _info_router
from app.api.marketplace.zip import router as _zip_router
from app.api.marketplace.git import make_git_wsgi_app

router = APIRouter()
router.include_router(_info_router)
router.include_router(_zip_router)

__all__ = ["router", "make_git_wsgi_app"]
