"""Shared SSRF-safe outbound HTTP client machinery.

Extracted from ``src.marketplace_asset_mirror`` (the curated-marketplace
asset mirror was the first caller to need it) so every OTHER outbound fetch
of an admin/curator/upstream-controlled URL gets the exact same
DNS-rebinding-safe, redirect-revalidating guard without re-implementing it
per caller. The first new consumer is ``connectors.mcp.oauth_client``
(RFC 9728 / RFC 8414 / RFC 7591 discovery, dynamic client registration, and
token exchange/refresh traffic against an upstream MCP source's
authorization server — 2026-07-30 outbound MCP OAuth sources spec).

Two threats against the naive "validate URL, then GET" pattern:

1. **Redirect bypass** — without revalidation, an attacker 302s to
   ``http://169.254.169.254/...`` and the caller fetches cloud metadata.
2. **DNS rebinding** — without IP pinning, the connect-time DNS lookup can
   return a different IP than the validation lookup.

httpx makes both defences collapse into a single custom ``Transport``:
httpx invokes ``handle_request()`` (sync) / ``handle_async_request()``
(async) on EVERY outgoing request — including every redirect hop — so
re-running SSRF validation in the transport closes the redirect bypass for
free. Within that hook we also rewrite the URL host to the IP we just
validated and stash the original hostname in the ``Host`` header + the
``sni_hostname`` extension so TLS SNI / certificate verification still bind
to the caller-supplied hostname.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

#: Sane default timeout/redirect caps for callers that don't override them.
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_REDIRECTS = 5


def resolve_safe(url: str, *, https_only: bool = False) -> Tuple[bool, str, str]:
    """Reject URLs we shouldn't follow and return the IP the caller MUST connect to.

    Returns ``(ok, reason, pinned_ip)``. On rejection ``pinned_ip`` is empty.

    Why the pinned IP matters: ``urllib`` would otherwise re-resolve the
    hostname at connection time, and an attacker-controlled DNS server can
    return a public IP for the validation lookup and ``127.0.0.1`` /
    ``169.254.169.254`` for the connection lookup (DNS rebinding). Resolving
    once here and connecting to that exact IP defeats the rebind. ALL
    addresses returned by ``getaddrinfo`` are validated — round-robin DNS
    that mixes public + private IPs is treated as unsafe regardless of which
    one we'd have picked first.

    ``https_only`` rejects a plain ``http://`` scheme outright — used by
    callers (e.g. the outbound MCP OAuth client) whose traffic must never
    downgrade to cleartext, even for a same-origin redirect hop.
    """
    try:
        parts = urlparse(url)
    except ValueError as e:
        return False, f"bad_url: {e}", ""
    allowed_schemes = ("https",) if https_only else ("http", "https")
    if parts.scheme not in allowed_schemes:
        return False, f"unsupported_scheme: {parts.scheme}", ""
    host = parts.hostname or ""
    if not host:
        return False, "missing_host", ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"dns_failure: {e}", ""

    chosen_ip = ""
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"unparseable_address: {ip_str}", ""
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return False, f"address_in_blocked_range: {ip_str}", ""
        # AWS / GCP / Azure metadata endpoints fall under is_link_local
        # (169.254.169.254) above — explicit additional check for IPv6
        # ULA + the broad metadata-style catchall would be belt-and-
        # suspenders only.
        # Prefer the first IPv4 result for connection pinning (broader CDN
        # compatibility); fall back to the first record otherwise.
        if not chosen_ip and info[0] == socket.AF_INET:
            chosen_ip = ip_str
    if not chosen_ip and infos:
        chosen_ip = infos[0][4][0]
    if not chosen_ip:
        return False, "no_address", ""
    return True, "", chosen_ip


class SSRFRejected(Exception):
    """Raised inside the SSRF-guard transports when a (initial or redirected)
    URL fails :func:`resolve_safe`.

    Distinct from ``httpx.RequestError`` so callers can map this to a
    terminal outcome (never retry) rather than a transient network error.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class SSRFGuardTransport(httpx.HTTPTransport):
    """Sync transport that re-validates SSRF rules on every outgoing request
    and pins the connection to the IP it just resolved.

    Redirect re-validation comes for free because httpx invokes
    ``handle_request()`` once per redirect hop (when the client is
    configured with ``follow_redirects=True``). DNS-rebinding defence comes
    from rewriting the URL host to the validated IP — httpcore no longer
    re-resolves the hostname at connect time.

    ``_resolve`` is a small overridable seam (rather than calling
    :func:`resolve_safe` directly from ``handle_request``) so a subclass
    defined in a caller module can route through that module's own
    re-exported, monkeypatchable name — see
    ``src.marketplace_asset_mirror._SSRFGuardTransport`` for the pattern
    that preserves its pre-extraction test seams.
    """

    def __init__(self, *args, https_only: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._https_only = https_only

    def _resolve(self, url: str) -> Tuple[bool, str, str]:
        return resolve_safe(url, https_only=self._https_only)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        ok, reason, ip = self._resolve(str(request.url))
        if not ok:
            raise SSRFRejected(reason)
        original_host = request.url.host
        # Rewrite the URL host to the validated IP. httpcore opens the
        # connection to whatever ``request.url.host`` says, so this is what
        # actually pins the connection.
        request.url = request.url.copy_with(host=ip)
        # Preserve the original hostname for vhost routing + TLS SNI / cert
        # verification. ``sni_hostname`` is a documented httpx extension
        # honored by the TLS layer in 0.24+.
        request.headers["Host"] = original_host
        request.extensions = {
            **request.extensions,
            "sni_hostname": original_host,
        }
        return super().handle_request(request)


class SSRFGuardAsyncTransport(httpx.AsyncHTTPTransport):
    """Async mirror of :class:`SSRFGuardTransport` — same guarantees, for
    callers (e.g. the outbound MCP OAuth client) that run on ``httpx.AsyncClient``.
    """

    def __init__(self, *args, https_only: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._https_only = https_only

    def _resolve(self, url: str) -> Tuple[bool, str, str]:
        return resolve_safe(url, https_only=self._https_only)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ok, reason, ip = self._resolve(str(request.url))
        if not ok:
            raise SSRFRejected(reason)
        original_host = request.url.host
        request.url = request.url.copy_with(host=ip)
        request.headers["Host"] = original_host
        request.extensions = {
            **request.extensions,
            "sni_hostname": original_host,
        }
        return await super().handle_async_request(request)


def build_client(
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    follow_redirects: bool = True,
    https_only: bool = False,
    headers: Optional[dict] = None,
) -> httpx.Client:
    """Build a sync ``httpx.Client`` wired to :class:`SSRFGuardTransport`.

    ``https_only=True`` rejects a plain ``http://`` scheme on the initial
    request AND on every redirect hop — appropriate for traffic that must
    never downgrade to cleartext (e.g. OAuth discovery/token endpoints).
    """
    return httpx.Client(
        transport=SSRFGuardTransport(https_only=https_only),
        timeout=timeout,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        headers=headers or {},
    )


def build_async_client(
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    follow_redirects: bool = True,
    https_only: bool = False,
    headers: Optional[dict] = None,
) -> httpx.AsyncClient:
    """Async counterpart of :func:`build_client`."""
    return httpx.AsyncClient(
        transport=SSRFGuardAsyncTransport(https_only=https_only),
        timeout=timeout,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        headers=headers or {},
    )
