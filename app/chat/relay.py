"""Sandbox-local loopback relay for the chat secret broker.

Runs inside the E2B sandbox alongside the CLI subprocesses (``claude``,
``agnes``, ``agnes mcp``). It is the only thing in the sandbox that ever
holds a broker ticket, and it holds it **in memory only** — never in
``os.environ`` (subprocess envs are inherited/inspectable) and never on
disk. CLI subprocesses are pointed at this relay's loopback address with a
dummy key; the relay attaches the real ticket as the ``Authorization``
header on the outbound leg to the Agnes server's broker routes
(``/api/broker/{anthropic,agnes-api,agnes-mcp}``).

Tickets are pushed over stdin by the runner (see ``app/chat/runner.py``)
via ``set_tickets`` at spawn and after every resume. Until a fresh ticket
has been pushed since the most recent resume signal, the relay refuses to
serve (``disarm`` / the ``_armed`` flag) — see AC-G-resume-fresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Maps the inbound loopback path prefix to the ticket scope required to
# forward it. `/anthropic` and `/agnes-api` ride the `main` ticket;
# `/agnes-mcp` rides the `mcp` ticket (spawned as a separate subprocess with
# a narrower scope).
_SCOPE_FOR_PREFIX = {
    "/anthropic": "main",
    "/agnes-api": "main",
    "/agnes-mcp": "mcp",
}


class Relay:
    """In-sandbox HTTP forwarder that never persists real credentials.

    Binds a loopback-only (``127.0.0.1``) listener. CLI subprocesses talk to
    it with a dummy key; it attaches the real short-lived ticket on the
    outbound leg to the Agnes server's broker routes.
    """

    def __init__(self, server_url: str) -> None:
        self._server_url = server_url.rstrip("/")
        self._main_ticket: Optional[str] = None
        self._mcp_ticket: Optional[str] = None
        self._armed = False
        self._server: Optional[asyncio.base_events.Server] = None
        self._client: Optional[httpx.AsyncClient] = None

    def set_tickets(self, main: str, mcp: str) -> None:
        """Store fresh tickets in memory only.

        Never writes to ``os.environ`` or disk — the only copies of the
        ticket values live in these two instance attributes.
        """
        self._main_ticket = main
        self._mcp_ticket = mcp
        self._armed = True

    def disarm(self) -> None:
        """Refuse to serve until the next ``set_tickets`` call.

        Used on the resume path: between "the old runner paused" and "fresh
        tickets pushed for the resumed session", the relay must not forward
        with a stale ticket.
        """
        self._armed = False

    def _ticket_for_path(self, path: str) -> Optional[str]:
        prefix = "/" + path.lstrip("/").split("/", 1)[0]
        scope = _SCOPE_FOR_PREFIX.get(prefix)
        if scope == "main":
            return self._main_ticket
        if scope == "mcp":
            return self._mcp_ticket
        return None

    async def _forward(self, path: str, body: bytes) -> httpx.Response:
        """Forward an inbound loopback request to the real broker route.

        Raises ``RuntimeError`` if the relay is not armed or holds no ticket
        for the requested path's scope — callers must never fall back to an
        unauthenticated or stale-ticket request.
        """
        if not self._armed:
            raise RuntimeError("relay not armed: no tickets pushed since last resume")
        ticket = self._ticket_for_path(path)
        if not ticket:
            raise RuntimeError(f"relay has no ticket for the scope of path {path!r}")

        url = f"{self._server_url}/api/broker{path}"
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            return await client.post(
                url,
                content=body,
                headers={"Authorization": f"Bearer {ticket}"},
            )
        finally:
            if owns_client:
                await client.aclose()

    async def start(self, port_hint: int = 0) -> int:
        """Bind a loopback-only HTTP listener and return the bound port.

        Uses stdlib ``asyncio.start_server`` with a minimal HTTP/1.1 request
        parser — no new dependency for the listener side; the outbound leg
        to the Agnes server uses ``httpx`` (already a project dependency).
        """
        self._client = httpx.AsyncClient()

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await self._handle_connection(reader, writer)
            except Exception:
                logger.exception("relay: error handling loopback connection")
            finally:
                with contextlib.suppress(Exception):
                    writer.close()
                    await writer.wait_closed()

        self._server = await asyncio.start_server(_handle, host="127.0.0.1", port=port_hint)
        assert self._server.sockets is not None
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        if not request_line:
            return
        try:
            _method, path, _version = request_line.decode("latin-1").strip().split(" ", 2)
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return

        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()

        length = int(headers.get("content-length", "0") or "0")
        body = await reader.readexactly(length) if length else b""

        try:
            resp = await self._forward(path, body)
        except RuntimeError as exc:
            payload = str(exc).encode()
            writer.write(f"HTTP/1.1 503 Service Unavailable\r\nContent-Length: {len(payload)}\r\n\r\n".encode())
            writer.write(payload)
            await writer.drain()
            return

        content = resp.content
        writer.write(f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}\r\n".encode())
        # Forward Content-Type so the in-sandbox SDK/CLI (both httpx-based)
        # decode the body correctly — without it httpx defaults to
        # application/octet-stream and JSON parsing can take a wrong path
        # (Devin review on #849). Content-Length is recomputed from the fully
        # buffered body; other hop-by-hop headers are deliberately not relayed.
        ctype = resp.headers.get("content-type")
        if ctype:
            writer.write(f"Content-Type: {ctype}\r\n".encode())
        writer.write(f"Content-Length: {len(content)}\r\n\r\n".encode())
        writer.write(content)
        await writer.drain()

    async def stop(self) -> None:
        """Close the listener and the outbound client (used at session teardown)."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
