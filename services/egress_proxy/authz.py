"""Egress authorization core — hostname allowlist + post-resolution IP
re-check.

Pure decision logic for the chat-sandbox egress proxy (``proxy.py``).
Separated so it is unit-testable without sockets and reusable by any
other component that needs the same policy.

Decision sequence for a requested ``host:port``:

1. **Always-blocked hosts** — cloud metadata endpoints are denied
   unconditionally, even if allowlisted (an allowlist entry must never
   be able to reopen them).
2. **Hostname allowlist** — exact match or ``*.suffix`` wildcard.
   Bare IP literals are matched as hostnames too.
3. **DNS resolution + per-IP re-check** — the host is resolved and
   EVERY resolved address must pass: link-local/metadata ranges are
   denied unconditionally; loopback and private ranges are denied
   unless ``block_private=False``. This closes the classic
   DNS-rebinding gap where a hostname passes the allow check while
   actually resolving to a link-local/metadata/internal address.
4. **Fail closed** — resolution failure denies.

The decision returns the vetted, resolved addresses so the proxy can
connect to exactly what was checked (no second resolution → no
time-of-check/time-of-use window).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable

# Denied unconditionally — an allowlist entry cannot reopen these.
ALWAYS_BLOCKED_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "169.254.169.254",
        "fd00:ec2::254",
    }
)
_ALWAYS_BLOCKED_SUFFIXES = (".metadata.google.internal", ".metadata.goog")

# Networks denied unconditionally (cloud metadata + link-local).
_HARD_BLOCKED_NETS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)

# Denied by default (``block_private=True``): the sandbox must not reach
# the host's / deployment's internal networks through the proxy — the
# Agnes server itself is reached directly over the internal docker
# network (NO_PROXY), not through here.
_PRIVATE_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
)

Resolver = Callable[[str, int], list[tuple]]


def _default_resolver(host: str, port: int) -> list[tuple]:
    return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)


@dataclass
class Decision:
    allowed: bool
    reason: str
    #: Vetted (family, sockaddr) pairs — connect to these exact
    #: addresses, never re-resolve.
    addresses: list[tuple] = field(default_factory=list)


def _norm_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def _host_allowlisted(host: str, allow_hosts: Iterable[str]) -> bool:
    for entry in allow_hosts:
        e = _norm_host(str(entry))
        if not e:
            continue
        if e.startswith("*."):
            if host.endswith(e[1:]) and host != e[2:]:
                return True
        elif host == e:
            return True
    return False


def _unwrap_embedded_v4(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """Return the IPv4 address an IPv6 address carries, or the input.

    ``::ffff:169.254.169.254`` is an ``IPv6Address``, so a family-matched
    range walk compares it against the v6 ranges only — it is in none of
    them and sails through, while a dual-stack host connecting to it
    reaches the v4 metadata service. 6to4 (``2002::/16``) and Teredo
    (``2001::/32``) embed a v4 address the same way. Unwrapping first
    means one set of rules covers every spelling of an address
    (Devin Review on #1148).
    """
    if ip.version != 6:
        return ip
    for embedded in (ip.ipv4_mapped, ip.sixtofour):  # type: ignore[union-attr]
        if embedded is not None:
            return embedded
    teredo = ip.teredo  # type: ignore[union-attr]
    if teredo is not None:
        return teredo[1]  # the client address, i.e. what we would reach
    return ip


def _ip_blocked(ip: ipaddress._BaseAddress, *, block_private: bool) -> str | None:
    shown = ip
    ip = _unwrap_embedded_v4(ip)

    # Categorical rejections, ahead of the range walk: enumerating networks
    # cannot express these, and `0.0.0.0`/`::` in particular are in none of
    # the loopback ranges yet connecting to them reaches loopback on Linux.
    if ip.is_unspecified:
        return f"resolved address {shown} is the unspecified address"
    if ip.is_multicast:
        return f"resolved address {shown} is multicast"
    if ip.is_reserved:
        return f"resolved address {shown} is reserved"

    for net in _HARD_BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return f"resolved address {shown} is in blocked range {net}"
    if block_private:
        for net in _PRIVATE_NETS:
            if ip.version == net.version and ip in net:
                return f"resolved address {shown} is in private range {net}"
    return None


def decide(
    host: str,
    port: int,
    allow_hosts: Iterable[str],
    *,
    block_private: bool = True,
    resolver: Resolver = _default_resolver,
) -> Decision:
    """Authorize one egress connection. See module docstring for the
    sequence; ``addresses`` on an allowed decision are the vetted
    sockaddrs to connect to."""
    h = _norm_host(host)
    if not h:
        return Decision(False, "empty host")

    if h in ALWAYS_BLOCKED_HOSTS or any(h.endswith(s) for s in _ALWAYS_BLOCKED_SUFFIXES):
        return Decision(False, f"host {h!r} is always blocked (metadata endpoint)")

    if not _host_allowlisted(h, allow_hosts):
        return Decision(False, f"host {h!r} is not in the egress allowlist")

    try:
        infos = resolver(h, port)
    except OSError as exc:
        return Decision(False, f"DNS resolution failed for {h!r}: {exc}")
    if not infos:
        return Decision(False, f"DNS resolution returned no addresses for {h!r}")

    vetted: list[tuple] = []
    for info in infos:
        sockaddr = info[4] if len(info) >= 5 else None
        if not sockaddr:
            return Decision(False, f"unparseable addrinfo for {h!r}")
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return Decision(False, f"unparseable resolved address {sockaddr[0]!r}")
        blocked = _ip_blocked(ip, block_private=block_private)
        if blocked:
            # ANY bad address kills the whole request — a rebinding
            # answer that mixes a public and a link-local record must
            # not be connectable at all.
            return Decision(False, blocked)
        vetted.append(info)

    return Decision(True, "allowed", addresses=vetted)
