"""Configuration-time policy for an MCP source's own ``url``.

Every forward to an ``http``/``sse`` MCP source dials this URL with a
credential attached — the shared vault secret, a per-user secret, or an OAuth
access token. #1124 established that a URL Agnes will dial with credentials
must be checked when it is *configured*, not when a real caller trips over it,
and applied that to the OAuth ``authorization_endpoint`` / ``token_endpoint``.
The source's own url sat outside that check (#1154): an admin could register
``http://169.254.169.254/mcp`` and every forward would post user credentials to
the cloud metadata endpoint in cleartext.

**Why this is not simply ``resolve_safe(https_only=True)``.** That guard, right
for an authorization server (always a public service), rejects every private
and loopback address. An MCP source is not necessarily public — an organization
running its own tool server on ``http://mcp.internal:8080`` is an ordinary
deployment, and a developer's ``http://localhost:3000`` is an ordinary Tuesday.
Applying the OAuth guard verbatim would close the hole by breaking those.

So the line is drawn where "nobody legitimately wants this" actually falls:

* **Refused always** — link-local (the cloud metadata endpoints live at
  ``169.254.169.254`` / ``fd00:ec2::254``), multicast, reserved, unspecified,
  and any scheme other than http/https. None of these host an MCP server; all
  of them are exfiltration or SSRF-pivot targets.
* **Refused always** — plain ``http://`` to a *public* address. Sending a
  bearer token across the internet in cleartext has no legitimate use.
* **Allowed, and recorded** — private / loopback addresses over plain http.
  This is the intranet and dev case. The verdict carries a warning so the
  audit row says the instance has such a source rather than staying silent.
* **Mixed resolution** (a hostname resolving to both public and private
  addresses) is refused: that is the DNS-rebinding shape, and no honest
  deployment needs it.
* **Accepted, and recorded** — a hostname that does not resolve right now.
  See below.

**Why an unresolvable host is not refused by default.** Making a save depend on
DNS sounds fail-closed and is really a footgun: this check runs over the MERGED
row, so a DNS blip would block edits that have nothing to do with the url — an
admin could not rename a source while its host was briefly unresolvable. It
also forecloses pre-provisioning a source before its DNS exists. And the
honesty argument: configuration-time resolution is a tripwire, not the
enforcement point, because the name can resolve somewhere else by the time a
forward actually dials it. So the default records the unknown rather than
blessing or blocking it — while the checks that need NO resolution (scheme, and
a literal IP in the url) are enforced regardless, which is what stops the
``http://169.254.169.254/mcp`` of the issue.

Operators who want the fail-closed posture set ``mcp.source_url_strict`` (env
``AGNES_MCP_SOURCE_URL_STRICT``): https, to an address that resolves, and
resolves to something public — the same bar the OAuth endpoints are held to.

Strict mode honours ``security.ssrf_allowed_hosts``, the allowlist every other
admin-configured URL already consults (``_validate_url_not_private`` in
``app/api/admin.py`` — marketplace clone URLs, the Keboola stack_url, the
server-config URL fields). Without that, an instance with ONE internal MCP
source could never adopt strict mode at all, and an operator who had already
declared that host trusted for every other admin URL would find the declaration
did not carry here — the same "the two paths disagree" complaint #1154 is
about. One allowlist, honoured everywhere.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import List, Literal, Tuple
from urllib.parse import urlparse

#: Address classes that never host a legitimate MCP server. ``is_private``
#: and ``is_loopback`` are deliberately NOT here — see the module docstring.
_NEVER_ROUTABLE = ("is_link_local", "is_multicast", "is_reserved", "is_unspecified")

#: ``unknown`` is a real third answer, not a missing one: the host did not
#: resolve, so the address class could not be established either way.
Reach = Literal["public", "internal", "unknown"]


@dataclass(frozen=True)
class UrlVerdict:
    """The outcome of the policy check.

    ``ok`` False means refuse the write. ``warning`` is set on an accepted URL
    that the operator should know about (a credentialed forward to an internal
    address, or a host that could not be checked); callers put it in the audit
    row, which is what keeps an accepted-but-notable url from being silent.
    """

    ok: bool
    reason: str = ""
    reach: Reach = "public"
    warning: str = ""


def _as_literal_ip(host: str) -> str | None:
    """The host as an IP string when it IS one, else None.

    ``urlparse`` already strips the brackets from an IPv6 literal, so the
    hostname arrives in a form ``ip_address`` accepts directly.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


def _classify(ip_str: str) -> Tuple[bool, Reach, str]:
    """(routable, reach, reason). ``routable`` False ⇒ refuse outright.

    The order of these three tests is load-bearing, because Python's
    ``ipaddress`` flags overlap in both directions:

    * ``::1`` is ``is_loopback`` AND ``is_reserved`` — testing the blocked
      classes first would refuse a developer's own machine.
    * ``169.254.169.254`` and ``fe80::1`` are ``is_link_local`` AND
      ``is_private`` — testing private first would wave the cloud metadata
      endpoint straight through as "internal", which is the entire hole.

    So loopback short-circuits as internal (unambiguous, and its ``reserved``
    flag is a quirk of the IPv6 registry rather than a hazard), THEN the
    blocked classes refuse, and only what survives both is judged on
    ``is_private``.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False, "public", f"unparseable_address: {ip_str}"
    if addr.is_loopback:
        return True, "internal", ""
    for attr in _NEVER_ROUTABLE:
        if getattr(addr, attr, False):
            return False, "public", f"address_in_blocked_range: {ip_str}"
    if addr.is_private:
        return True, "internal", ""
    return True, "public", ""


def check_source_url(
    url: str,
    *,
    strict: bool = False,
    allowed_hosts: frozenset[str] | None = None,
    _resolver=None,
) -> UrlVerdict:
    """Judge an MCP source ``url`` at configuration time.

    ``allowed_hosts`` is ``security.ssrf_allowed_hosts`` — operator-declared
    internal hosts. It only changes STRICT mode's answer (the default already
    accepts an internal address); passing it means an instance with one
    on-prem MCP source can still adopt strict mode for everything else.

    ``_resolver`` is a seam for tests: a callable taking a hostname and
    returning a list of IP strings. Production passes ``None`` and this
    resolves through ``socket.getaddrinfo``, which BLOCKS — an async caller
    must run this in a thread (same hazard, same remedy, as
    ``set_oauth_client_config``).
    """
    if not (url or "").strip():
        return UrlVerdict(False, "missing_url")
    try:
        parts = urlparse(url)
    except ValueError as exc:
        return UrlVerdict(False, f"bad_url: {exc}")

    if parts.scheme not in ("http", "https"):
        return UrlVerdict(False, f"unsupported_scheme: {parts.scheme or '(none)'}")
    host = parts.hostname or ""
    if not host:
        return UrlVerdict(False, "missing_host")

    # An operator-declared internal host is exempt from strict mode's
    # public-address demand — but NOT from anything else. It still may not be a
    # metadata endpoint, and it still may not carry credentials in cleartext to
    # a public address; the allowlist says "this internal host is trusted", not
    # "skip the checks". Scheme is judged before the exemption for the same
    # reason.
    strict_here = strict and host.lower() not in (allowed_hosts or frozenset())

    if strict_here and parts.scheme != "https":
        return UrlVerdict(False, "strict_mode_requires_https")
    strict = strict_here

    # A literal IP needs no resolver, and MUST be judged even when one is
    # unavailable — `http://169.254.169.254/mcp`, the url the issue is about,
    # is exactly this shape. Doing it before the lookup also means the most
    # dangerous input is the one case that can never fail open.
    literal = _as_literal_ip(host)
    if literal is not None:
        routable, reach, reason = _classify(literal)
        if not routable:
            return UrlVerdict(False, reason)
        return _finish(parts.scheme, host, reach, strict=strict)

    resolver = _resolver or _default_resolver
    try:
        ips = resolver(host)
    except socket.gaierror as exc:
        # Unknown, not refused — see the module docstring. Strict mode is where
        # "cannot verify" becomes "will not accept".
        if strict:
            return UrlVerdict(False, f"strict_mode_requires_resolvable_host: {exc}")
        return UrlVerdict(
            True,
            reach="unknown",
            warning=f"{host} does not resolve right now, so its address could not be checked ({exc})",
        )
    if not ips:
        if strict:
            return UrlVerdict(False, "strict_mode_requires_resolvable_host: no_address")
        return UrlVerdict(True, reach="unknown", warning=f"{host} resolved to no address")

    # EVERY resolved address is judged, not just the one we would connect to:
    # round-robin DNS mixing a public and a private answer is the rebinding
    # shape, and picking the first record would make the verdict a coin flip.
    reaches: List[Reach] = []
    for ip_str in ips:
        routable, reach, reason = _classify(ip_str)
        if not routable:
            return UrlVerdict(False, reason)
        reaches.append(reach)

    if len(set(reaches)) > 1:
        return UrlVerdict(False, "mixed_public_and_internal_addresses")
    return _finish(parts.scheme, host, reaches[0], strict=strict)


def _finish(scheme: str, host: str, reach: Reach, *, strict: bool) -> UrlVerdict:
    """Apply the scheme×reach rules once the address class is known.

    Shared by the literal-IP and the resolved-hostname paths so the two cannot
    drift — the literal path is the one that has to hold when DNS is down, and
    a second copy of these rules is exactly how it would stop matching.
    """
    if strict and reach != "public":
        return UrlVerdict(False, f"strict_mode_requires_public_address: {host}")

    # Cleartext to a public address: the credential would cross the internet in
    # the clear. Internal http is the intranet case and is allowed, loudly.
    if scheme == "http":
        if reach == "public":
            return UrlVerdict(False, "cleartext_http_to_public_address")
        return UrlVerdict(
            True,
            reach=reach,
            warning=f"credentialed forwards to {host} travel unencrypted over the internal network; prefer https",
        )

    if reach == "internal":
        return UrlVerdict(
            True,
            reach=reach,
            warning=f"{host} resolves to an internal address",
        )
    return UrlVerdict(True, reach=reach)


def _default_resolver(host: str) -> List[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]
