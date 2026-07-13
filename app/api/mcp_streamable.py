"""Agnes Streamable-HTTP MCP server — OAuth 2.1 transport.

Mounted at /api/mcp/http in app/main.py.  Exposes the same tools as the
SSE MCP server (mcp_http.py) but over the modern Streamable-HTTP transport
that remote MCP connectors (Claude Desktop, Claude.ai, Cursor, Cline, …)
prefer, protected by native OAuth 2.1 + PKCE.

The SSE app continues to live at /api/mcp/sse for Cowork back-compat —
this module does NOT replace it.

Authentication path
-------------------
1. MCP client discovers  GET /.well-known/oauth-protected-resource
   which points to the authorization server at /api/mcp/http.
2. Client registers via POST /api/mcp/http/register (RFC 7591).
3. User browser is redirected through /api/mcp/oauth/consent (our
   bridge, mounted by main.py) which checks the Agnes session and shows
   a consent screen before minting a short-lived authorization code.
4. Client exchanges code for a JWT at POST /api/mcp/http/token.
5. All subsequent MCP requests carry  Authorization: Bearer <JWT>.
   The JWT is a standard Agnes session JWT — resolve_token_to_user
   accepts it and all RBAC applies unchanged.

Tools
-----
Re-uses every @mcp.tool() defined in mcp_http.py via a shared FastMCP
instance to avoid duplicating tool definitions.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.mcp_oauth import AgnesMCPOAuthProvider
from app.auth.public_url import mcp_issuer_url, pinned_public_base_url, public_base_url

logger = logging.getLogger(__name__)

_BASE = os.environ.get("AGNES_MCP_INTERNAL_URL", "http://localhost:8000").rstrip("/")


@contextlib.asynccontextmanager
async def streamable_session_manager_lifespan(app) -> AsyncIterator[None]:
    """Run the streamable MCP session manager for the lifetime of the app.

    Wire this into the main app lifespan (``async with …(app): yield``).
    Starlette does NOT run a mounted sub-app's lifespan, so without this the
    streamable endpoint raises "Task group is not initialized" on the first
    request.

    The FastMCP instance is read from ``app.state`` (set by create_app when
    the streamable app is mounted), never a module global — each app gets its
    own instance, so the SDK's "session_manager.run() once per instance" rule
    holds across repeated create_app() calls in tests. No-op if the streamable
    app was never mounted.
    """
    mcp = getattr(app.state, "mcp_streamable_instance", None)
    if mcp is None:
        yield
        return
    # The SDK's StreamableHTTPSessionManager.run() may be called at most once
    # per instance (and the mounted ASGI app captures that single manager, so
    # it can't be swapped). A real process enters the lifespan exactly once;
    # only tests re-enter it on the same app singleton to simulate a restart.
    # Guard so the second entry is a no-op rather than a hard RuntimeError.
    if getattr(mcp, "_agnes_session_manager_started", False):
        yield
        return
    mcp._agnes_session_manager_started = True
    async with mcp.session_manager.run():
        yield


def _headers() -> dict[str, str]:
    """Forward the caller's verified OAuth access token to Agnes self-calls.

    The SDK's auth middleware authenticates the bearer token and exposes the
    resulting ``AccessToken`` via ``get_access_token()``.  Its ``.token`` is
    the raw JWT minted by ``exchange_authorization_code`` — a standard Agnes
    session JWT that ``resolve_token_to_user`` accepts unchanged.
    """
    access = get_access_token()
    if access is None or not access.token:
        raise RuntimeError("No authentication token in current MCP context")
    return {"Authorization": f"Bearer {access.token}"}


def _oauth_client_registration_options() -> ClientRegistrationOptions:
    return ClientRegistrationOptions(enabled=True, valid_scopes=["read"], default_scopes=["read"])


def _oauth_revocation_options() -> RevocationOptions:
    return RevocationOptions(enabled=True)


def _oauth_metadata_for_request(request: Request):
    """Build OAuth AS + protected-resource metadata for the incoming host."""
    from urllib.parse import urlparse

    from mcp.server.auth.routes import build_metadata, build_resource_metadata_url
    from mcp.shared.auth import ProtectedResourceMetadata

    issuer = AnyHttpUrl(mcp_issuer_url(request=request))
    as_metadata = build_metadata(
        issuer_url=issuer,
        service_documentation_url=None,
        client_registration_options=_oauth_client_registration_options(),
        revocation_options=_oauth_revocation_options(),
    )
    # RFC 8252: public clients (VS Code, native apps) use
    # token_endpoint_auth_method=none. build_metadata() hardcodes only
    # confidential auth methods — extend the list so VS Code's discovery
    # check passes and it proceeds with Dynamic Client Registration instead
    # of showing the manual client-ID dialog.
    _supported = list(as_metadata.token_endpoint_auth_methods_supported or [])
    if "none" not in _supported:
        as_metadata.token_endpoint_auth_methods_supported = _supported + ["none"]

    pr_metadata = ProtectedResourceMetadata(
        resource=issuer,
        authorization_servers=[issuer],
        scopes_supported=["read"],
        resource_name="Agnes",
    )
    pr_path = urlparse(str(build_resource_metadata_url(issuer))).path
    return as_metadata, pr_metadata, pr_path


_MCP_OAUTH_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource/api/mcp/http"


def _mcp_oauth_discovery_routes() -> list:
    """Return root-level OAuth discovery routes (RFC 8414 + RFC 9728).

    The streamable sub-app already serves these relative to its mount at
    ``/api/mcp/http``, but standards-compliant MCP clients probe the origin
    root — ``GET https://host/.well-known/oauth-authorization-server`` and
    ``GET https://host/.well-known/oauth-protected-resource/api/mcp/http`` —
    so we publish identical documents there too. Endpoint URLs inside the
    documents are derived from the request host when ``AGNES_BASE_URL`` /
    ``SERVER_URL`` are unset, so production behind a TLS proxy advertises the
    public connector URL without requiring a separate env var.

    Because the authorization-server issuer carries a path component
    (``/api/mcp/http``), RFC 8414 §3 says a compliant client builds the
    metadata URL by inserting the well-known segment *between host and path*:
    ``/.well-known/oauth-authorization-server/api/mcp/http``. Lenient clients
    (Claude) fall back to the bare root document, but stricter ones (Cursor,
    GitHub Copilot, ChatGPT web) probe only the path-aware location and 404 →
    they never discover the authorize/token/register endpoints and surface
    "authentication required" before OAuth can even start. We therefore serve
    the *same* AS document at the path-aware ``oauth-authorization-server`` and
    OpenID-Connect ``openid-configuration`` locations as well. The root
    protected-resource document already uses the path-aware form, so no
    sibling is needed there.
    """
    from mcp.server.auth.handlers.metadata import (
        MetadataHandler,
        ProtectedResourceMetadataHandler,
    )
    from starlette.routing import Route

    async def oauth_authorization_server(request: Request):
        as_metadata, _, _ = _oauth_metadata_for_request(request)
        return await MetadataHandler(as_metadata).handle(request)

    async def oauth_protected_resource(request: Request):
        _, pr_metadata, _ = _oauth_metadata_for_request(request)
        return await ProtectedResourceMetadataHandler(pr_metadata).handle(request)

    # The AS document is published at the bare root *and* at the path-aware
    # RFC 8414 / OIDC locations that carry the issuer's ``/api/mcp/http`` path
    # suffix, so strict clients (Cursor, Copilot, ChatGPT web) discover it.
    as_paths = [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/api/mcp/http",
        "/.well-known/openid-configuration",
        "/.well-known/openid-configuration/api/mcp/http",
    ]
    routes = [Route(p, endpoint=oauth_authorization_server, methods=["GET", "OPTIONS"]) for p in as_paths]
    routes.append(
        Route(
            _MCP_OAUTH_PROTECTED_RESOURCE_PATH,
            endpoint=oauth_protected_resource,
            methods=["GET", "OPTIONS"],
        )
    )
    return routes


class _ServeStreamableAtMountRootMiddleware:
    """Serve the streamable MCP endpoint at the sub-app's mount root.

    FastMCP routes the streamable transport at its internal ``/mcp`` path, so
    after mounting at ``/api/mcp/http`` the JSON-RPC endpoint physically lives
    at ``/api/mcp/http/mcp`` — but the advertised connector URL (and the OAuth
    ``resource``) is the mount itself. Clients POST to the URL they were given
    verbatim, hit a 404, and surface it as "MCP endpoint not found" right
    after a successful OAuth. Rewrite mount-root requests to the transport
    path; ``/api/mcp/http/mcp`` keeps working unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            # Starlette's Mount keeps the full URL in scope["path"] and records
            # the mount prefix in root_path — compare the mount-relative part.
            root_path = scope.get("root_path", "")
            route_path = scope.get("path", "")
            if route_path.startswith(root_path):
                route_path = route_path[len(root_path) :]
            if route_path in ("", "/"):
                new_path = f"{root_path}/mcp"
                scope = {**scope, "path": new_path, "raw_path": new_path.encode()}
        await self._app(scope, receive, send)


def mount_root_route(streamable_app: ASGIApp, mount_path: str = "/api/mcp/http"):
    """Exact-path route for the bare (slash-less) advertised connector URL.

    Starlette's ``Mount`` only matches ``<mount>/…`` — the bare URL, which is
    exactly what users paste into their MCP client, falls through to the next
    matching route: the broader SSE mount at ``/api/mcp``, whose router 404s
    it once auth passes. Forward it into the streamable app as a mount-root
    request so ``_ServeStreamableAtMountRootMiddleware`` lands it on the
    transport path. Register this on the main app next to the mount.
    """
    from starlette.routing import Route

    class _ForwardToStreamable:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            path = f"{mount_path}/"
            await streamable_app(
                {
                    **scope,
                    "path": path,
                    "raw_path": path.encode(),
                    "root_path": mount_path,
                    # Mirror Starlette's Mount: pin app_root_path to the
                    # top-level app root, else Request.base_url absorbs the
                    # connector mount path and the WWW-Authenticate
                    # resource_metadata URL comes out doubled.
                    "app_root_path": scope.get("app_root_path", scope.get("root_path", "")),
                },
                receive,
                send,
            )

    return Route(mount_path, endpoint=_ForwardToStreamable(), methods=["GET", "POST", "DELETE", "OPTIONS"])


class _FixMcpOAuthResourceMetadataMiddleware:
    """Rewrite ``WWW-Authenticate`` resource_metadata for proxied deployments.

    The MCP SDK pins ``resource_metadata`` at app-build time from
    ``AuthSettings.issuer_url``. When neither ``AGNES_BASE_URL`` nor
    ``SERVER_URL`` is set, that defaults to ``http://localhost:8000`` even
    though clients reach us at the public host. Derive the correct URL from
    the incoming ASGI scope instead.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or pinned_public_base_url() is not None:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        correct_metadata_url = f"{public_base_url(request=request)}/.well-known/oauth-protected-resource/api/mcp/http"

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"www-authenticate":
                        text = value.decode("latin-1")
                        if "resource_metadata=" in text:
                            text = re.sub(
                                r'resource_metadata="[^"]*"',
                                f'resource_metadata="{correct_metadata_url}"',
                                text,
                            )
                        value = text.encode("latin-1")
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _PatchPublicClientDiscoveryMiddleware:
    """Append 'none' to token_endpoint_auth_methods_supported in FastMCP's OAuth discovery.

    build_metadata() in the MCP SDK hardcodes only confidential auth methods.
    VS Code native MCP is a public client (method=none, no client_secret + PKCE)
    and skips Dynamic Client Registration when it doesn't see 'none' in this list.
    This middleware patches the JSON body served by the FastMCP sub-app's own
    /.well-known/oauth-authorization-server endpoint (distinct from the root-level
    route patched in _oauth_metadata_for_request).
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].endswith("/.well-known/oauth-authorization-server"):
            await self._app(scope, receive, send)
            return

        # Collect the response from the inner app then patch the body.
        response_started = False
        status_code = 200
        headers: list = []
        body_chunks: list[bytes] = []

        async def _patched_send(message):  # type: ignore[no-untyped-def]
            nonlocal response_started, status_code, headers
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = list(message.get("headers", []))
                return
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    import json as _json

                    raw = b"".join(body_chunks)
                    try:
                        data = _json.loads(raw)
                        methods = data.get("token_endpoint_auth_methods_supported") or []
                        if "none" not in methods:
                            data["token_endpoint_auth_methods_supported"] = methods + ["none"]
                        patched = _json.dumps(data).encode()
                    except Exception:
                        patched = raw
                    new_headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
                    new_headers.append((b"content-length", str(len(patched)).encode()))
                    await send(
                        {
                            "type": "http.response.start",
                            "status": status_code,
                            "headers": new_headers,
                        }
                    )
                    await send({"type": "http.response.body", "body": patched, "more_body": False})
                return
            # Forward any other ASGI message types unchanged.
            await send(message)

        await self._app(scope, receive, _patched_send)


def _make_streamable_app() -> ASGIApp:
    """Build and return the Streamable-HTTP MCP ASGI app with OAuth 2.1."""
    # The MCP endpoint URL — clients paste this into their connector config.
    mcp_url = mcp_issuer_url()

    provider = AgnesMCPOAuthProvider()

    auth = AuthSettings(
        issuer_url=AnyHttpUrl(mcp_url),
        resource_server_url=AnyHttpUrl(mcp_url),
        client_registration_options=_oauth_client_registration_options(),
        revocation_options=_oauth_revocation_options(),
        required_scopes=["read"],
    )

    mcp = FastMCP(
        "Agnes",
        instructions=(
            "Agnes is a self-hosted AI harness for the organization's data, skills, and memory. "
            "Use `catalog` first to discover available tables, then `schema` to "
            "understand columns, `describe` for sample rows, and `query` to run SQL. "
            "Run `server_info` to check connectivity at the start of a session."
        ),
        # DNS-rebinding/Host-header protection is disabled deliberately: this is
        # a REMOTE connector reached through a TLS-terminating reverse proxy on a
        # fixed FQDN (operators set AGNES_BASE_URL to that host), and the proxy
        # rewrites Host. The SDK's allowed-hosts check would otherwise reject the
        # legitimate proxied Host. Every request is still OAuth-bearer-gated
        # before reaching a tool, so a rebound origin gains nothing without a
        # valid token. Mirrors the SSE server's stance in mcp_http.py.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        auth=auth,
        auth_server_provider=provider,
        stateless_http=True,
    )

    # ── tools (mirrors mcp_http.py) ─────────────────────────────────────

    @mcp.tool()
    async def server_info() -> dict:
        """Return Agnes server health and your account email.

        Useful as a quick connectivity check at the start of a session.
        """
        result: dict[str, Any] = {"authenticated": True}
        async with httpx.AsyncClient() as c:
            try:
                r = await c.get(f"{_BASE}/api/health", timeout=5)
                if r.status_code == 200:
                    result["health"] = r.json()
            except Exception:
                result["health"] = "unreachable"
            try:
                r = await c.get(f"{_BASE}/api/me", headers=_headers(), timeout=5)
                if r.status_code == 200:
                    result["user_email"] = r.json().get("email", "")
            except Exception:
                pass
        return result

    @mcp.tool()
    async def catalog() -> dict:
        """List all tables available to you (RBAC-filtered).

        Returns a dict with a ``tables`` list.  Each entry has:
        - ``id``         — use this in schema / describe / query calls
        - ``name``       — human-readable label
        - ``query_mode`` — local | remote | materialized
        - ``sql_flavor`` — duckdb or bigquery
        - ``rows``       — approximate row count (may be null)
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{_BASE}/api/v2/catalog", headers=_headers(), timeout=30)
            r.raise_for_status()
            return r.json()

    @mcp.tool()
    async def schema(table_id: str) -> dict:
        """Show column names, types, and SQL dialect hints for a table.

        Args:
            table_id: Table ID from the catalog.
        """
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{_BASE}/api/v2/schema/{table_id}", headers=_headers(), timeout=30)
            r.raise_for_status()
            return r.json()

    @mcp.tool()
    async def describe(table_id: str, rows: int = 5) -> dict:
        """Show schema plus sample rows for a table.

        Args:
            table_id: Table ID from the catalog.
            rows:     How many sample rows to return (default 5, max 50).
        """
        rows = min(max(1, rows), 50)
        async with httpx.AsyncClient() as c:
            rs = await c.get(f"{_BASE}/api/v2/schema/{table_id}", headers=_headers(), timeout=30)
            rs.raise_for_status()
            rm = await c.get(
                f"{_BASE}/api/v2/sample/{table_id}",
                headers=_headers(),
                params={"n": rows},
                timeout=30,
            )
            rm.raise_for_status()
        return {"schema": rs.json(), "sample": rm.json()}

    @mcp.tool()
    async def query(sql: str, limit: int = 1000) -> dict:
        """Execute a SQL query against Agnes data.

        Args:
            sql:   SQL statement.
            limit: Maximum rows to return (default 1000).
        """
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{_BASE}/api/query",
                json={"sql": sql, "limit": limit},
                headers=_headers(),
                timeout=60,
            )
            r.raise_for_status()
            return r.json()

    @mcp.tool()
    async def documentation_api() -> str:
        """Return the curated Agnes REST API reference as Markdown."""
        from pathlib import Path

        md_path = Path(__file__).resolve().parent.parent.parent / "docs" / "api-reference.md"
        try:
            return md_path.read_text(encoding="utf-8")
        except OSError:
            return "# API reference unavailable\n\nThe source markdown file is missing."

    _register_dynamic_tools(mcp)

    # Stash the FastMCP instance on the returned app's state so create_app can
    # lift it onto the main app and run its session manager in the lifespan.
    inner = mcp.streamable_http_app()
    inner.state.mcp_streamable_instance = mcp
    # Layer 0: serve the MCP endpoint at the mount root — the URL clients
    # actually paste — not only at the SDK-internal /mcp sub-path.
    rooted = _ServeStreamableAtMountRootMiddleware(inner)
    # Layer 1: patch WWW-Authenticate resource_metadata for proxied deployments.
    wrapped = _FixMcpOAuthResourceMetadataMiddleware(rooted)
    wrapped.state = inner.state
    # Layer 2: patch the FastMCP sub-app's own OAuth discovery to add 'none'
    # so VS Code native MCP proceeds with Dynamic Client Registration.
    patched = _PatchPublicClientDiscoveryMiddleware(wrapped)
    patched.state = inner.state  # type: ignore[attr-defined]
    return patched


def _register_dynamic_tools(mcp: FastMCP) -> None:
    """Best-effort registration of passthrough tools from tool_registry."""
    try:
        from app.api.mcp.tools_generator import register_passthrough_tools
    except Exception:
        logger.exception("Streamable MCP: dynamic tool imports unavailable")
        return
    try:
        names = register_passthrough_tools(mcp)
        if names:
            logger.info("Streamable MCP: registered %d passthrough tools", len(names))
    except Exception:
        logger.exception("Streamable MCP: passthrough tool registration failed")
