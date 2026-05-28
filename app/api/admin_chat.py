"""Admin observability for chat sessions."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.auth.access import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/chat", tags=["admin-chat"])


@router.get("")
async def list_active(request: Request, _admin: dict = Depends(require_admin)):
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        return {"sessions": [], "warning": "chat_disabled"}
    sessions = []
    for live in mgr.list_live():
        sessions.append({
            "id": live.chat_id,
            "user_email": live.user_email,
            "state": live.state.value,
            "pid": live.handle.pid if live.handle else None,
            "started_at": live.started_at.isoformat(),
            "last_activity": live.last_activity.isoformat(),
            "crash_count": live.crash_count,
        })
    return {"sessions": sessions}


@router.delete("/{chat_id}")
async def admin_kill(chat_id: str, request: Request, _admin: dict = Depends(require_admin)):
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        raise HTTPException(503, detail="chat_disabled")
    await mgr.kill(chat_id, reason="admin_kill")
    return {"ok": True}


@router.websocket("/{chat_id}/tail")
async def admin_tail(ws: WebSocket, chat_id: str):
    await ws.accept()
    repo = getattr(ws.app.state, "chat_repo", None)
    if repo is None:
        await ws.close(code=4404)
        return
    s = repo.get_session(chat_id)
    if s is None:
        await ws.close(code=4404)
        return
    chat_data_dir = getattr(ws.app.state, "chat_data_dir", None)
    if chat_data_dir is None:
        await ws.send_json({"type": "no_log", "reason": "chat_data_dir_not_configured"})
        await ws.close()
        return
    log_path = (
        Path(chat_data_dir) / "users" / s.user_email /
        "sessions" / chat_id / "run.log"
    )
    if not log_path.exists():
        await ws.send_json({"type": "no_log"})
        await ws.close()
        return
    with log_path.open("r") as f:
        f.seek(0, 2)  # tail: start from end
        try:
            while True:
                line = f.readline()
                if line:
                    await ws.send_json({"type": "line", "text": line.rstrip()})
                else:
                    await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            return
