"""Minimal fail-closed HTTP CONNECT proxy for chat-sandbox egress.

Runs as a sidecar dual-homed on the sandboxes' *internal* docker network
(which has no route to the outside) and a normal bridge. Sandboxes get
``HTTP_PROXY``/``HTTPS_PROXY`` pointed here; anything that ignores the
proxy simply has no route — the network is the enforcement layer, this
proxy is the policy layer (allowlist + post-resolution IP re-check via
``authz.decide``).

Supports the two shapes real clients use:

- ``CONNECT host:port`` (TLS tunnels — curl/https, pip, git-over-https):
  authorize, connect to the **vetted resolved address** (never
  re-resolve), reply ``200 Connection Established``, then pipe bytes
  both ways.
- Absolute-form plain HTTP (``GET http://host/path``): authorize, open
  the vetted connection, rewrite the request line to origin-form,
  forward, and pipe.

Deliberately NOT a caching/rewriting proxy — no header inspection
beyond the request line and Host, no TLS interception, stdlib only.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from functools import partial
from urllib.parse import urlsplit

from services.egress_proxy.authz import Decision, decide

logger = logging.getLogger("egress_proxy")

_MAX_HEADER_BYTES = 32 * 1024
_CONNECT_TIMEOUT = 15.0


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy until EOF, then HALF-close the destination (write_eof).

    A full ``close()`` on source-EOF would tear down the destination
    socket's read side too, racing away the response still in flight the
    other way (CONNECT tunnels are bidirectional; the client half-closing
    after its request must still receive the upstream's reply).
    """
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
        else:  # pragma: no cover — TCP transports support write_eof
            writer.close()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def _close_all(*writers: asyncio.StreamWriter) -> None:
    """Close every writer, letting none of them mask the others."""
    for w in writers:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


async def _open_vetted(decision: Decision) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the first reachable vetted address from the decision."""
    last_exc: Exception | None = None
    for info in decision.addresses:
        sockaddr = info[4]
        try:
            return await asyncio.wait_for(asyncio.open_connection(sockaddr[0], sockaddr[1]), _CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            last_exc = exc
    raise OSError(f"no vetted address reachable: {last_exc}")


#: Dropped when relaying absolute-form requests: they are hop-by-hop, and
#: keeping them alive is what lets a client put a second request on a
#: connection whose destination was authorized for the first one.
_HOP_BY_HOP = (b"connection", b"proxy-connection", b"keep-alive", b"proxy-authorization")


def _relay_headers(rest: bytes) -> tuple[bytes, int, bool]:
    """Prepare an absolute-form request's headers for the upstream.

    Returns ``(headers, content_length, chunked)``. Hop-by-hop headers are
    dropped and ``Connection: close`` forced, so the upstream ends the
    response by closing and the client cannot reuse the socket.
    """
    lines = rest.split(b"\r\n")
    kept, length, chunked = [], 0, False
    for line in lines:
        if not line:
            continue
        name, _, value = line.partition(b":")
        key = name.strip().lower()
        if key == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                length = 0
        elif key == b"transfer-encoding":
            chunked = True
        if key not in _HOP_BY_HOP:
            kept.append(line)
    kept.append(b"Connection: close")
    return b"\r\n".join(kept) + b"\r\n\r\n", length, chunked


def _deny_response(reason: str) -> bytes:
    body = f"egress denied: {reason}\n".encode()
    return (
        b"HTTP/1.1 403 Forbidden\r\n"
        b"Content-Type: text/plain\r\n"
        b"Connection: close\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )


class EgressProxy:
    def __init__(
        self,
        allow_hosts: list[str],
        *,
        block_private: bool = True,
        resolver=None,
    ) -> None:
        self._allow_hosts = allow_hosts
        self._block_private = block_private
        self._decide = partial(
            decide,
            allow_hosts=allow_hosts,
            block_private=block_private,
            **({"resolver": resolver} if resolver is not None else {}),
        )

    async def _decide_off_loop(self, host: str, port: int) -> Decision:
        """``decide`` for the serving path — run in a worker thread.

        ``decide`` resolves the host, and the default resolver is the
        blocking ``socket.getaddrinfo``. Calling it inline from the
        connection handlers held the single event loop for the full
        resolver timeout on a slow or unresponsive DNS server — and this
        one process proxies every sandbox on the internal network, so
        that stalled the accept loop and every in-flight tunnel too, not
        just the request doing the lookup (Devin Review on #1148).

        The policy itself stays in the one synchronous ``decide`` the
        unit tests exercise; only *where it runs* changes. A second,
        async copy of the decision logic would be free to drift from the
        copy the tests pin.
        """
        return await asyncio.to_thread(self._decide, host, port)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _CONNECT_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            writer.close()
            return
        if len(head) > _MAX_HEADER_BYTES:
            writer.write(_deny_response("oversized request head"))
            await writer.drain()
            writer.close()
            return

        request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = request_line.split()
        if len(parts) != 3:
            writer.write(_deny_response("malformed request line"))
            await writer.drain()
            writer.close()
            return
        method, target, _version = parts

        try:
            if method.upper() == "CONNECT":
                await self._handle_connect(target, reader, writer)
            else:
                await self._handle_absolute(method, target, head, reader, writer)
        except Exception as exc:  # noqa: BLE001 — one bad conn never kills the server
            logger.warning("egress proxy connection failed: %s", exc)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handle_connect(self, target: str, reader, writer) -> None:
        host, _, port_s = target.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            writer.write(_deny_response("malformed CONNECT target"))
            await writer.drain()
            writer.close()
            return
        decision = await self._decide_off_loop(host, port)
        self._log(decision, host, port)
        if not decision.allowed:
            writer.write(_deny_response(decision.reason))
            await writer.drain()
            writer.close()
            return
        try:
            up_r, up_w = await _open_vetted(decision)
        except OSError as exc:
            writer.write(_deny_response(f"upstream connect failed: {exc}"))
            await writer.drain()
            writer.close()
            return
        # Same finally as the absolute-form path below. `_pipe` swallows
        # socket errors so this is far less exposed, but cancellation still
        # escapes — and the upstream socket has no other owner.
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        finally:
            _close_all(up_w, writer)

    async def _handle_absolute(self, method: str, target: str, head: bytes, reader, writer) -> None:
        split = urlsplit(target)
        if split.scheme != "http" or not split.hostname:
            writer.write(_deny_response("only absolute-form http:// or CONNECT supported"))
            await writer.drain()
            writer.close()
            return
        port = split.port or 80
        decision = await self._decide_off_loop(split.hostname, port)
        self._log(decision, split.hostname, port)
        if not decision.allowed:
            writer.write(_deny_response(decision.reason))
            await writer.drain()
            writer.close()
            return
        try:
            up_r, up_w = await _open_vetted(decision)
        except OSError as exc:
            writer.write(_deny_response(f"upstream connect failed: {exc}"))
            await writer.drain()
            writer.close()
            return
        # Everything past a successful upstream connect runs under finally:
        # a sandbox that resets mid-body raises out of reader.read(), and
        # `handle` above can only close the CLIENT socket — it has no
        # reference to this upstream one, which then survives until the GC
        # notices. On a shared sidecar those accumulate
        # (Devin Review on #1148).
        try:
            # Rewrite the request line to origin-form.
            origin_path = split.path or "/"
            if split.query:
                origin_path += "?" + split.query
            first, rest = head.split(b"\r\n", 1)
            _m, _t, version = first.decode("latin-1").split()
            headers, body_len, chunked = _relay_headers(rest)
            if chunked:
                # A chunked body has no length to bound the forward by, so
                # we could not tell where it ends and the next request
                # begins.
                writer.write(_deny_response("chunked request bodies are not supported over the egress proxy"))
                await writer.drain()
                return
            up_w.write(f"{method} {origin_path} {version}\r\n".encode("latin-1") + headers)

            # Forward EXACTLY this request's body, then half-close upstream.
            #
            # The client→upstream direction must not stay open: HTTP proxy
            # clients pool per-proxy, not per-destination, so a follow-up
            # `http://other-host/…` on the same socket used to be piped
            # straight into the connection opened for the FIRST host —
            # never re-checked against the allowlist, and carrying that
            # request's cookies and Authorization header to a host it was
            # never meant for. Only the response comes back.
            remaining = body_len
            while remaining > 0:
                chunk = await reader.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                up_w.write(chunk)
            await up_w.drain()
            # Deliberately NOT write_eof() here. Simply never reading from the
            # client again gives the same guarantee — nothing after this
            # request can reach the upstream — without half-closing the socket.
            # A TCP half-close after a request is legal but uncommon, and some
            # origin servers and load balancers treat the FIN as an abort and
            # close without answering (Devin Review on #1148). The upstream
            # already knows where the request ends: Content-Length bounds the
            # body and `Connection: close` tells it not to expect another.
            await _pipe(up_r, writer)
        finally:
            _close_all(up_w, writer)

    def _log(self, decision: Decision, host: str, port: int) -> None:
        logger.info(
            "egress %s %s:%s — %s",
            "ALLOW" if decision.allowed else "DENY",
            host,
            port,
            decision.reason,
        )


async def serve(
    allow_hosts: list[str],
    *,
    host: str = "0.0.0.0",
    port: int = 3128,
    block_private: bool = True,
) -> asyncio.AbstractServer:
    proxy = EgressProxy(allow_hosts, block_private=block_private)
    server = await asyncio.start_server(proxy.handle, host, port)
    logger.info(
        "egress proxy listening on %s:%s (allowlist: %s; block_private=%s)",
        host,
        port,
        ", ".join(allow_hosts) or "<empty — deny all>",
        block_private,
    )
    return server


DEFAULT_PORT = 3128


def _parse_listen(value: str) -> tuple[str, int]:
    """``EGRESS_LISTEN`` → ``(host, port)``, never raising.

    Splitting on the last colon and calling ``int()`` on the tail turns a
    perfectly reasonable value into a crash: ``"0.0.0.0"`` yields the port
    ``"0.0.0.0"``, and a bare IPv6 literal like ``"::"`` yields host ``":"``.
    Compose restarts the sidecar ``unless-stopped``, so that is a crash-loop —
    and in allowlist mode a dead proxy means every sandbox loses all egress,
    explained only by a stack trace (Devin Review on #1148).

    Accepts ``host:port``, ``[v6]:port``, and a bare host of either family,
    falling back to the default port with a warning rather than dying.
    """
    v = (value or "").strip()
    if not v:
        return "0.0.0.0", DEFAULT_PORT
    if v.startswith("["):  # [::1]:3128 or bare [::1]
        host, _, rest = v.partition("]")
        host = host[1:]
        port_s = rest.lstrip(":")
    elif v.count(":") > 1:  # bare IPv6 literal — no port to take
        return v, DEFAULT_PORT
    else:
        host, _, port_s = v.rpartition(":")
        if not host:  # no colon at all → the whole value is the host
            host, port_s = v, ""
    try:
        port = int(port_s)
        if not 0 < port < 65536:
            raise ValueError(port_s)
    except ValueError:
        logger.warning("EGRESS_LISTEN %r has no usable port — falling back to %d", value, DEFAULT_PORT)
        port = DEFAULT_PORT
    return host or "0.0.0.0", port


def main() -> None:
    import os

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    allow = [h.strip() for h in os.environ.get("EGRESS_ALLOW_HOSTS", "").split(",") if h.strip()]
    lhost, lport = _parse_listen(os.environ.get("EGRESS_LISTEN", "0.0.0.0:3128"))
    block_private = os.environ.get("EGRESS_BLOCK_PRIVATE", "1").lower() not in ("0", "false")

    async def _run() -> None:
        server = await serve(allow, host=lhost, port=lport, block_private=block_private)
        async with server:
            await server.serve_forever()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
