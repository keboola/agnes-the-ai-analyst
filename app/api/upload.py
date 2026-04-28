"""Upload endpoints — sessions, artifacts, CLAUDE.local.md."""

import hashlib
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.utils import get_data_dir as _get_data_dir

router = APIRouter(prefix="/api/upload", tags=["upload"])

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
_CHUNK_SIZE = 64 * 1024  # 64 KB read chunks for streaming size check


async def _stream_to_temp(file: UploadFile) -> tuple[tempfile.NamedTemporaryFile, int]:
    """Stream-upload with cumulative size check. Returns (tempfile, size).

    Aborts once total > MAX_UPLOAD_SIZE — avoids buffering the entire
    body in memory before the size cap rejects it (OOM prevention).
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
    total = 0
    try:
        while True:
            chunk = await file.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_SIZE:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
                )
            tmp.write(chunk)
        tmp.flush()
    except HTTPException:
        raise
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    tmp.seek(0)
    return tmp, total


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
    filename = Path(raw_name).name  # Strips directory traversal components
    if not filename or filename.startswith("."):
        filename = f"upload_{uuid.uuid4().hex[:8]}"
    target = sessions_dir / filename

    tmp, size = await _stream_to_temp(file)
    try:
        tmp.close()
        shutil.move(tmp.name, str(target))
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    return {"status": "ok", "filename": filename, "size": size}


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
    filename = Path(raw_name).name  # Strips directory traversal components
    if not filename or filename.startswith("."):
        filename = f"upload_{uuid.uuid4().hex[:8]}"
    target = artifacts_dir / filename

    tmp, size = await _stream_to_temp(file)
    try:
        tmp.close()
        shutil.move(tmp.name, str(target))
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    return {"status": "ok", "filename": filename, "size": size}


class LocalMdRequest(BaseModel):
    content: str


@router.post("/local-md")
async def upload_local_md(
    request: LocalMdRequest,
    user: dict = Depends(get_current_user),
):
    """Upload CLAUDE.local.md content for corporate memory processing."""
    user_email = user["email"]
    md_dir = _get_data_dir() / "user_local_md"
    md_dir.mkdir(parents=True, exist_ok=True)

    # Hashed filename — stable per user, no charset surprises from email
    safe_name = hashlib.sha256(user_email.encode()).hexdigest()[:24] + ".md"
    target = md_dir / safe_name
    target.write_text(request.content, encoding="utf-8")
    return {
        "status": "ok",
        "user": user_email,
        "size": len(request.content),
    }
