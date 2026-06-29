"""Browser-loopback CLI login (gh-style `agnes auth login`).

Flow
----
1. The CLI starts a localhost listener on an ephemeral port, generates a
   random ``state``, and opens the browser to
   ``GET /cli/auth/start?port=<p>&state=<s>``.
2. If the browser has no session, ``/cli/auth/start`` bounces through the
   normal ``/login`` flow (Google / magic-link / password) and returns.
3. The authenticated user sees a confirmation page naming the machine/port
   requesting access and clicks **Authorize** (``POST /cli/auth/start``).
4. The server mints a single-use code (sha256 stored in ``cli_auth_codes``,
   bound to the user) and 302-redirects the browser to
   ``http://127.0.0.1:<port>/callback?code=<code>&state=<state>``.
5. The CLI's loopback handler captures the code, verifies ``state``, and
   POSTs the code to ``/cli/auth/exchange`` over HTTPS. The server consumes
   the code and returns a real Personal Access Token.

The durable credential (the PAT) only ever travels over the direct CLI→server
HTTPS response — never through the browser address bar or history. The code in
the loopback URL is single-use and expires in ~2 minutes, so even if it lands
in browser history it is inert.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlencode

import duckdb
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.auth.dependencies import _get_db, get_optional_user, require_session_token
from app.auth.jwt import create_access_token
from src.repositories.access_tokens import AccessTokenRepository
from src.repositories.audit import AuditRepository
from src.repositories.cli_auth_codes import CliAuthCodeRepository

router = APIRouter(prefix="/cli/auth", tags=["cli-auth"])

# Exchange-code lifetime. Long enough to cover a Google sign-in detour, short
# enough that a leaked code in browser history is useless.
_CODE_TTL = timedelta(seconds=120)
# Default lifetime of the PAT minted by this flow. Mirrors the CLI token
# default (90d) so analysts re-auth quarterly, not weekly.
_PAT_TTL_DAYS = 90


def _validate_loopback(port: int, state: str) -> None:
    """Reject anything that isn't a plausible loopback callback request.

    ``port`` must be a non-privileged TCP port; ``state`` must be a non-trivial
    opaque token. The redirect host is always hard-coded to 127.0.0.1, so there
    is no open-redirect surface — only the port is caller-supplied.
    """
    if port < 1024 or port > 65535:
        raise HTTPException(status_code=400, detail="invalid loopback port")
    if not state or len(state) < 16 or len(state) > 256 or not state.isascii():
        raise HTTPException(status_code=400, detail="invalid state")


@router.get("/start", response_class=HTMLResponse)
async def start(
    request: Request,
    port: int = Query(...),
    state: str = Query(...),
    user: Optional[dict] = Depends(get_optional_user),
):
    """Render the authorize-CLI confirmation page (or bounce through login)."""
    _validate_loopback(port, state)

    if not user:
        # Preserve the full start URL (incl. query) through sign-in. safe_next_path
        # accepts `/path?query`, so the loopback params survive the OAuth round-trip.
        nxt = "/cli/auth/start?" + urlencode({"port": port, "state": state})
        return RedirectResponse(url="/login?next=" + quote(nxt, safe=""), status_code=302)

    from app.web.router import templates, _build_context

    ctx = _build_context(
        request,
        user=user,
        cli_port=port,
        cli_state=state,
    )
    return templates.TemplateResponse(request, "cli_auth_confirm.html", ctx)


@router.post("/start", response_class=HTMLResponse)
async def confirm(
    request: Request,
    port: int = Form(...),
    state: str = Form(...),
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Mint a single-use code and redirect it to the CLI loopback listener."""
    _validate_loopback(port, state)

    raw_code = secrets.token_urlsafe(32)
    code_hash = hashlib.sha256(raw_code.encode()).hexdigest()
    CliAuthCodeRepository(conn).create(
        code_hash=code_hash,
        user_id=user["id"],
        email=user["email"],
        expires_at=datetime.now(timezone.utc) + _CODE_TTL,
    )
    try:
        AuditRepository(conn).log(
            user_id=user["id"], action="cli_auth.code_issued",
            resource="cli_auth", params={"port": port},
        )
    except Exception:
        pass

    target = f"http://127.0.0.1:{port}/callback?" + urlencode(
        {"code": raw_code, "state": state}
    )
    # 303 so the browser issues a GET to the loopback regardless of this being
    # a POST handler.
    return RedirectResponse(url=target, status_code=303)


class ExchangeRequest(BaseModel):
    code: str
    name: Optional[str] = None


class ExchangeResponse(BaseModel):
    token: str
    email: str
    expires_at: Optional[str]


@router.post("/exchange", response_model=ExchangeResponse)
async def exchange(
    payload: ExchangeRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trade a single-use code for a real Personal Access Token.

    No session required — the code itself is the bearer of authority, and it
    was minted only after an authenticated browser confirmation.
    """
    if not payload.code:
        raise HTTPException(status_code=400, detail="code is required")
    code_hash = hashlib.sha256(payload.code.encode()).hexdigest()
    claimed = CliAuthCodeRepository(conn).consume(code_hash)
    if not claimed:
        # Expired, already used, or never existed — all indistinguishable on
        # purpose so a guesser learns nothing.
        raise HTTPException(status_code=400, detail="code invalid or expired")

    user_id = claimed["user_id"]
    email = claimed["email"]
    name = (payload.name or "Agnes CLI").strip()[:80] or "Agnes CLI"

    # Mint a PAT exactly as POST /auth/tokens does (typ=pat, jti=token_id,
    # token_hash for the defense-in-depth check in verify_token).
    token_id = str(uuid.uuid4())
    expires_delta = timedelta(days=_PAT_TTL_DAYS)
    jwt_token = create_access_token(
        user_id=user_id, email=email,
        token_id=token_id, typ="pat",
        expires_delta=expires_delta,
        extra_claims={"scope": "cli-login"},
    )
    expires_at = datetime.now(timezone.utc) + expires_delta
    prefix = token_id.replace("-", "")[:8]
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    AccessTokenRepository(conn).create(
        id=token_id, user_id=user_id, name=name,
        token_hash=token_hash, prefix=prefix, expires_at=expires_at,
    )
    try:
        AuditRepository(conn).log(
            user_id=user_id, action="cli_auth.token_minted",
            resource=f"token:{token_id}", params={"name": name},
        )
    except Exception:
        pass

    return ExchangeResponse(
        token=jwt_token, email=email, expires_at=str(expires_at),
    )
