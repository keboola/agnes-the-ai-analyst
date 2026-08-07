"""Where the line falls for an MCP source's own ``url`` (#1154).

Every forward to an http/sse MCP source dials this URL with a credential
attached, so it earns a configuration-time check. The interesting part is not
that a check exists — it is WHERE it refuses, because the obvious answer
(``resolve_safe(https_only=True)``, what the OAuth endpoints get) also outlaws
an organization's own MCP server on an internal address, which is an ordinary
deployment rather than an attack.

So the policy has two tiers, and both halves need pinning: the baseline must
refuse what nobody legitimately wants (metadata endpoints, cleartext across the
internet) while still accepting an intranet source, and strict mode must be the
stricter thing it claims to be. A regression in either direction is silent —
one re-opens the hole, the other bricks a customer's install.

The resolver is injected. These tests must not depend on DNS.
"""

from __future__ import annotations

import socket

import pytest

from src.net.mcp_source_url import check_source_url

PUBLIC = ["93.184.216.34"]
PRIVATE = ["10.10.0.7"]
LOOPBACK = ["127.0.0.1"]
METADATA = ["169.254.169.254"]
IPV6_LOOPBACK = ["::1"]
IPV6_LINK_LOCAL = ["fe80::1"]


def _res(ips):
    return lambda host: list(ips)


# ── the attack the issue names ──────────────────────────────────────────────


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_cloud_metadata_endpoint_is_refused(scheme):
    """`http://169.254.169.254/mcp` is the exfiltration target #1154 describes.
    Refused under BOTH schemes — https to the metadata endpoint is no better,
    it just encrypts the leak."""
    v = check_source_url(f"{scheme}://metadata.example/mcp", _resolver=_res(METADATA))
    assert not v.ok
    assert "blocked_range" in v.reason


def test_ipv6_link_local_is_refused():
    v = check_source_url("https://sneaky.example/mcp", _resolver=_res(IPV6_LINK_LOCAL))
    assert not v.ok


def test_link_local_is_refused_even_though_python_calls_it_private():
    """`ipaddress` reports `is_private` True for 169.254.0.0/16 and fe80::/10.
    Classifying on `is_private` before the blocked classes would therefore wave
    the cloud metadata endpoint through as an ordinary intranet host — the
    exact hole, reopened by an innocent-looking reordering."""
    import ipaddress

    for probe in ("169.254.169.254", "fe80::1"):
        assert ipaddress.ip_address(probe).is_private, "premise changed — revisit _classify"
        assert not check_source_url("https://x.example/mcp", _resolver=_res([probe])).ok


def test_mixed_public_and_internal_resolution_is_refused():
    """Round-robin DNS answering with both is the rebinding shape: whichever
    address we validated, the connection could take the other one."""
    v = check_source_url("https://rebind.example/mcp", _resolver=_res(PUBLIC + PRIVATE))
    assert not v.ok
    assert v.reason == "mixed_public_and_internal_addresses"


def test_cleartext_to_a_public_address_is_refused():
    """A bearer token in the clear across the internet has no legitimate use,
    so this is refused at the baseline rather than left to strict mode."""
    v = check_source_url("http://mcp.vendor.example/mcp", _resolver=_res(PUBLIC))
    assert not v.ok
    assert v.reason == "cleartext_http_to_public_address"


# ── what the baseline must NOT break ────────────────────────────────────────


def test_internal_http_source_is_allowed_with_a_warning():
    """The whole reason this is not `resolve_safe`: an organization's own tool
    server on the intranet is a deployment, not an attack. Allowed — and the
    warning is what stops that being silent."""
    v = check_source_url("http://mcp.internal:8080/mcp", _resolver=_res(PRIVATE))
    assert v.ok
    assert v.reach == "internal"
    assert v.warning


def test_localhost_source_is_allowed():
    v = check_source_url("http://localhost:3000/mcp", _resolver=_res(LOOPBACK))
    assert v.ok
    assert v.reach == "internal"


def test_ipv6_loopback_is_allowed():
    v = check_source_url("http://[::1]:3000/mcp", _resolver=_res(IPV6_LOOPBACK))
    assert v.ok


def test_ordinary_public_https_source_is_clean():
    v = check_source_url("https://mcp.vendor.example/mcp", _resolver=_res(PUBLIC))
    assert v.ok
    assert v.reach == "public"
    assert not v.warning


def test_internal_https_is_allowed_and_still_noted():
    """https to the intranet is fine on the wire, but "this instance forwards
    credentials somewhere internal" is still worth an audit line."""
    v = check_source_url("https://mcp.internal/mcp", _resolver=_res(PRIVATE))
    assert v.ok
    assert v.warning


# ── strict mode is actually stricter ────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "ips"),
    [
        ("http://mcp.internal:8080/mcp", PRIVATE),
        ("https://mcp.internal/mcp", PRIVATE),
        ("http://localhost:3000/mcp", LOOPBACK),
    ],
)
def test_strict_mode_refuses_everything_not_public_https(url, ips):
    assert check_source_url(url, _resolver=_res(ips)).ok, "baseline should accept these"
    assert not check_source_url(url, strict=True, _resolver=_res(ips)).ok


def test_strict_mode_still_accepts_public_https():
    v = check_source_url("https://mcp.vendor.example/mcp", strict=True, _resolver=_res(PUBLIC))
    assert v.ok


# ── the allowlist is the one every other admin URL already uses ─────────────


def test_allowlisted_internal_host_survives_strict_mode():
    """`security.ssrf_allowed_hosts` is how an operator declares an internal
    host trusted for admin URLs. Without honouring it here, an instance with a
    single on-prem MCP source could never adopt strict mode at all."""
    url = "https://mcp.internal/mcp"
    assert not check_source_url(url, strict=True, _resolver=_res(PRIVATE)).ok
    v = check_source_url(url, strict=True, allowed_hosts=frozenset({"mcp.internal"}), _resolver=_res(PRIVATE))
    assert v.ok


def test_allowlist_is_matched_case_insensitively():
    v = check_source_url(
        "https://MCP.Internal/mcp",
        strict=True,
        allowed_hosts=frozenset({"mcp.internal"}),
        _resolver=_res(PRIVATE),
    )
    assert v.ok


def test_allowlist_does_not_excuse_the_metadata_endpoint():
    """The allowlist says "this internal host is trusted", not "skip the
    checks" — a listed name that resolves to the metadata endpoint is still
    refused, in strict mode and out of it."""
    for strict in (False, True):
        v = check_source_url(
            "https://mcp.internal/mcp",
            strict=strict,
            allowed_hosts=frozenset({"mcp.internal"}),
            _resolver=_res(METADATA),
        )
        assert not v.ok, f"strict={strict}"


def test_allowlist_does_not_excuse_cleartext_to_a_public_address():
    v = check_source_url(
        "http://mcp.internal/mcp",
        strict=True,
        allowed_hosts=frozenset({"mcp.internal"}),
        _resolver=_res(PUBLIC),
    )
    assert not v.ok
    assert v.reason == "cleartext_http_to_public_address"


# ── malformed input ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", "missing_url"),
        ("   ", "missing_url"),
        ("ftp://mcp.example/x", "unsupported_scheme: ftp"),
        ("file:///etc/passwd", "unsupported_scheme: file"),
        ("https://", "missing_host"),
        ("not-a-url", "unsupported_scheme: (none)"),
    ],
)
def test_malformed_urls_are_refused(url, expected):
    v = check_source_url(url, _resolver=_res(PUBLIC))
    assert not v.ok
    assert v.reason == expected


# ── the literal-IP path must not depend on a resolver ───────────────────────


def _explode(host):
    raise AssertionError("resolver must not be consulted for a literal IP")


def test_literal_metadata_ip_is_refused_without_any_resolution():
    """`http://169.254.169.254/mcp` is the url in the issue, and it is the one
    input that must never fail open — so it is judged before the lookup and
    holds even with no resolver reachable at all."""
    v = check_source_url("http://169.254.169.254/mcp", _resolver=_explode)
    assert not v.ok
    assert "blocked_range" in v.reason


def test_literal_public_ip_over_cleartext_is_refused_without_resolution():
    v = check_source_url("http://93.184.216.34/mcp", _resolver=_explode)
    assert not v.ok
    assert v.reason == "cleartext_http_to_public_address"


def test_literal_private_ip_is_allowed_without_resolution():
    v = check_source_url("http://10.10.0.7:8080/mcp", _resolver=_explode)
    assert v.ok
    assert v.reach == "internal"


def test_literal_ipv6_loopback_is_allowed_without_resolution():
    """urlparse strips the brackets, so the hostname is already IP-shaped."""
    v = check_source_url("http://[::1]:3000/mcp", _resolver=_explode)
    assert v.ok
    assert v.reach == "internal"


# ── an unresolvable host is recorded, not refused ───────────────────────────


def _boom(host):
    raise socket.gaierror("nodename nor servname provided")


def test_unresolvable_host_is_accepted_with_a_warning():
    """Refusing here reads as fail-closed and is really a footgun: the check
    runs over the MERGED row, so a DNS blip would block edits that never
    touched the url — an admin could not rename a source. Config-time
    resolution is a tripwire, not the enforcement point, so the unknown is
    recorded instead of blessed or blocked."""
    v = check_source_url("https://not-up-yet.example/mcp", _resolver=_boom)
    assert v.ok
    assert v.reach == "unknown"
    assert "does not resolve" in v.warning


def test_empty_resolution_is_accepted_with_a_warning():
    v = check_source_url("https://void.example/mcp", _resolver=_res([]))
    assert v.ok
    assert v.reach == "unknown"
    assert v.warning


def test_strict_mode_refuses_an_unresolvable_host():
    """Strict is where "cannot verify" becomes "will not accept"."""
    v = check_source_url("https://not-up-yet.example/mcp", strict=True, _resolver=_boom)
    assert not v.ok
    assert v.reason.startswith("strict_mode_requires_resolvable_host")
