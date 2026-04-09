"""Upload endpoints — sessions, artifacts, CLAUDE.local.md."""

import uuid
from datetime import datetime, timezone
from pathlib import Path as _Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.utils import get_data_dir as _get_data_dir

router = APIRouter(prefix="/api/upload", tags=["upload"])


@router.post("/sessions")
async def upload_session(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload a Claude session transcript (JSONL)."""
    user_id = user["id"]
    sessions_dir = _get_data_dir() / "user_sessions" / user_id
    sessions_dir.mkdir(parents=True, exist_ok=True)

    raw_name = file.filename or f"session_{uuid.uuid4().hex[:8]}.jsonl"
    filename = _Path(raw_name).name  # Strips directory traversal components
    if not filename or filename.startswith("."):
        filename = f"upload_{uuid.uuid4().hex[:8]}"
    target = sessions_dir / filename
    content = await file.read()
    target.write_bytes(content)
    return {"status": "ok", "path": str(target), "size": len(content)}


@router.post("/artifacts")
async def upload_artifact(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload an artifact (HTML report, PNG chart, etc.)."""
    user_id = user["id"]
    artifacts_dir = _get_data_dir() / "user_artifacts" / user_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    raw_name = file.filename or f"artifact_{uuid.uuid4().hex[:8]}"
    filename = _Path(raw_name).name  # Strips directory traversal components
    if not filename or filename.startswith("."):
        filename = f"upload_{uuid.uuid4().hex[:8]}"
    target = artifacts_dir / filename
    content = await file.read()
    target.write_bytes(content)
    return {"status": "ok", "path": str(target), "size": len(content)}


class LocalMdRequest(BaseModel):
    content: str


@router.post("/local-md")
async def upload_local_md(
    request: LocalMdRequest,
    user: dict = Depends(get_current_user),
):
    """Upload CLAUDE.local.md content for corporate memory processing."""
    user_id = user["id"]
    user_email = user["email"]
    md_dir = _get_data_dir() / "user_local_md"
    md_dir.mkdir(parents=True, exist_ok=True)

    target = md_dir / f"{user_email}.md"
    target.write_text(request.content, encoding="utf-8")
    return {
        "status": "ok",
        "user": user_email,
        "size": len(request.content),
    }
