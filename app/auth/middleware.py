"""Starlette middleware that transparently exchanges a verified Cloudflare Access
JWT for our standard `access_token` session cookie.

Runs before route handlers. On every request:

1. If the CF provider is not configured, pass through untouched.
2. If the request carries an `Authorization: Bearer` header (API/CLI/PAT
   client), pass through — those clients don't need a cookie, and setting
   one could leak into subsequent requests from shared clients.
3. If the request already has an `access_token` cookie, pass through
   (don't overwrite an active session — user may have logged in manually).
4. If a `Cf-Access-Jwt-Assertion` header is present and verifies, provision
   the user, mint our JWT, set the cookie, continue.
5. On any verification failure, pass through — the route handler will
   apply its normal auth logic (cookie/Bearer/redirect).

Never returns 401 from the middleware itself — that would break password/Google
login flows on deployments that enable CF as *one of several* auth methods.
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.auth.providers import cloudflare as cf

logger = logging.getLogger(__name__)

CF_HEADER = "Cf-Access-Jwt-Assertion"
COOKIE_NAME = "access_token"
COOKIE_MAX_AGE = 86400  # 24h — matches ACCESS_TOKEN_EXPIRE_HOURS in app/auth/jwt.py


class CloudflareAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not cf.is_available():
            return await call_next(request)
        # Bearer clients (PATs, API scripts) manage their own auth — don't set a cookie on them.
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            return await call_next(request)
        if request.cookies.get(COOKIE_NAME):
            return await call_next(request)
        token = request.headers.get(CF_HEADER)
        if not token:
            return await call_next(request)

        claims = cf.verify_cf_jwt(token)
        if claims is None:
            return await call_next(request)

        # Import inside dispatch to avoid circular imports at module load time
        from src.db import get_system_db
        from app.auth.jwt import create_access_token

        email = claims.get("email", "")
        name = claims.get("name", "")
        conn = get_system_db()
        try:
            user = cf.get_or_create_user_from_cf(email=email, name=name, conn=conn)
        finally:
            conn.close()

        if user is None:
            # Email outside allowlist or deactivated — pass through so the
            # normal 401 → /login redirect tells the user why.
            return await call_next(request)

        app_jwt = create_access_token(
            user_id=user["id"],
            email=user["email"],
            role=user["role"],
        )

        response = await call_next(request)
        import os
        use_secure = os.environ.get("TESTING", "").lower() not in ("1", "true")
        response.set_cookie(
            key=COOKIE_NAME,
            value=app_jwt,
            httponly=True,
            max_age=COOKIE_MAX_AGE,
            samesite="lax",
            secure=use_secure,
        )
        # Stash on request.state so this-request handlers can see the identity.
        # (Not strictly needed — the next request will use the cookie — but it
        # makes the first CF-authenticated request behave identically to a
        # cookie-authenticated one.)
        request.state.cf_user = user
        return response
