"""Admin observability for chat sessions."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.auth.access import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/chat", tags=["admin-chat"])


# In-memory ticket store for admin tail-WS auth.  Mirrors the chat-WS pattern
# in `app/api/chat.py`: a short-TTL one-shot token gates the WebSocket open.
# Without this, the tail route streamed any session's run.log to any
# anonymous WS caller — a confidentiality bypass.
_ADMIN_TAIL_TICKETS: dict[str, tuple[str, float]] = {}  # ticket -> (admin_user_id, expires_at)
_ADMIN_TICKET_TTL_SEC = 60


def _issue_admin_ticket(user_id: str) -> str:
    ticket = secrets.token_urlsafe(32)
    _ADMIN_TAIL_TICKETS[ticket] = (user_id, time.time() + _ADMIN_TICKET_TTL_SEC)
    return ticket


def _consume_admin_ticket(ticket: str) -> Optional[str]:
    rec = _ADMIN_TAIL_TICKETS.pop(ticket, None)
    if rec is None:
        return None
    user_id, expires_at = rec
    if time.time() > expires_at:
        return None
    return user_id


@router.get("")
async def list_active(request: Request, admin: dict = Depends(require_admin)):
    """List active chat sessions (or render the dashboard shell).

    Content-negotiated: browsers (``Accept: text/html``) get the
    ``admin_chat.html`` shell which then re-fetches this endpoint with
    ``Accept: application/json`` to populate the table.  Programmatic
    callers and the dashboard JS get the JSON payload directly.

    Single-endpoint design (per Task B.3 + architect finding #8) — the
    dashboard URL must match the JSON URL so admins typing /admin/chat
    in the address bar see something, not a 404.
    """
    # Content-negotiated route. Browsers (Accept: text/html) get the admin_chat.html
    # template; XHR / tooling get JSON {"sessions": [...]}. If you add a new
    # /admin/chat/{subpath} route, mirror this pattern: don't add a separate HTML
    # route in app/web/router.py — it would never match because this prefix wins.
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        from app.web.router import templates as _templates, _build_context as _build_ctx
        ctx = _build_ctx(request, user=admin)
        return _templates.TemplateResponse(request, "admin_chat.html", ctx)
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


@router.delete("/{chat_id}", status_code=204)
async def admin_kill(chat_id: str, request: Request, _admin: dict = Depends(require_admin)):
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        raise HTTPException(503, detail="chat_disabled")
    await mgr.kill(chat_id, reason="admin_kill")


@router.get("/{chat_id}/debug")
async def admin_debug(
    chat_id: str,
    request: Request,
    _admin: dict = Depends(require_admin),
) -> dict:
    """Admin-only introspection of per-session in-process counters.

    Used by the E2E suite (notably ``tests/e2e/test_bq_budget.py``) to
    read counters that previously had to be poked via ``docker exec
    python -c ...`` against module globals. Under the E2B-provider
    model there is no ``docker exec`` into the runner — the runner is
    a remote E2B microVM — so the test reads from this endpoint
    instead. The shape is intentionally narrow: just the counters the
    suite needs to assert on.
    """
    # bq_bytes — process-local accumulator inside app/api/query.py.
    try:
        from app.api.query import _per_session_bq_bytes
        bq_bytes = int(_per_session_bq_bytes.get(chat_id, 0))
    except Exception:
        bq_bytes = 0
    # session_state — live-manager view, if attached
    mgr = getattr(request.app.state, "chat_manager", None)
    live = None
    if mgr is not None:
        live = next(
            (s for s in mgr.list_live() if s.chat_id == chat_id),
            None,
        )
    return {
        "chat_id": chat_id,
        "bq_bytes": bq_bytes,
        "live": (
            {
                "state": live.state.value,
                "crash_count": live.crash_count,
                "started_at": live.started_at.isoformat(),
                "last_activity": live.last_activity.isoformat(),
            }
            if live is not None
            else None
        ),
    }


@router.get("/{chat_id}/tail-ticket")
async def tail_ticket(
    chat_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """Issue a short-TTL one-shot ticket for the tail WebSocket.

    The WebSocket itself can't carry the admin's session cookie/Authorization
    reliably across browsers (Safari in particular strips cookies on WS
    upgrades from `fetch`), so we mint a ticket here under the normal admin
    auth flow and the JS hands it to the WS as a query parameter.
    """
    # Verify the session exists so 404 surfaces here rather than mid-WS.
    repo = getattr(request.app.state, "chat_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="chat_disabled")
    if repo.get_session(chat_id) is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    ticket = _issue_admin_ticket(admin["id"])
    return {
        "ticket": ticket,
        "ws_url": f"/admin/chat/{chat_id}/tail?ticket={ticket}",
    }


@router.websocket("/{chat_id}/tail")
async def admin_tail(ws: WebSocket, chat_id: str, ticket: str = ""):
    # Ticket auth BEFORE accept() — invalid callers get close(4401) without
    # ever seeing protocol-upgrade success.
    user_id = _consume_admin_ticket(ticket)
    if user_id is None:
        await ws.close(code=4401, reason="invalid_or_expired_ticket")
        return
    repo = getattr(ws.app.state, "chat_repo", None)
    if repo is None:
        await ws.close(code=4404)
        return
    s = repo.get_session(chat_id)
    if s is None:
        await ws.close(code=4404)
        return
    await ws.accept()
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
