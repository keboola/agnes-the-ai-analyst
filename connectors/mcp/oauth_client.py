"""Outbound OAuth client for upstream MCP sources (2026-07-30 spec, PR 1).

Agnes acting as an OAuth **client** against an upstream MCP server's
authorization server — the mirror image of ``app/auth/mcp_oauth.py``
(Agnes acting as the *issuer* on the inbound side). Implements:

* RFC 9728 — OAuth 2.0 Protected Resource Metadata discovery.
* RFC 8414 — OAuth 2.0 Authorization Server Metadata discovery.
* RFC 7591 — OAuth 2.0 Dynamic Client Registration.
* RFC 6749 §4.1 + RFC 7636 (PKCE) — authorization-code token exchange +
  refresh.

Every outbound call in this module MUST go through an
``httpx.AsyncClient`` built by :func:`build_oauth_http_client` (SSRF-safe,
https-only, per-hop redirect re-validation — see
``src.net.ssrf_safe_client``). Token exchange/refresh additionally disable
redirect-following altogether (``follow_redirects=False`` per call) — an AS
redirecting a token response is never a legitimate flow.

**Mix-up defense (RFC 9700 §4.4):** every function here that needs a token
endpoint or client identity takes them as EXPLICIT parameters. Callers
(``connectors.mcp.client``, the admin registration endpoints) must source
those parameters from the stored ``mcp_source_oauth_clients`` row for the
target ``source_id`` — never from request/callback data or an AS response —
so a malicious or compromised second AS can never redeem a code/refresh
token against a different source's client identity. This module has no way
to look up that row itself (no DB access here) — the calling convention
itself is the defense.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from connectors.mcp.client import exc_summary
from src.net.ssrf_safe_client import build_async_client

logger = logging.getLogger(__name__)

USER_AGENT = "Agnes-MCP-OAuth-Client/1.0 (+https://github.com/keboola/agnes-the-ai-analyst; agnes-mcp-oauth)"

DEFAULT_TIMEOUT_SEC = 30.0

#: RFC 9700 §4.1 — PKCE S256 is mandatory; downgrading to "plain" or no PKCE
#: at all is a fail-closed error, never a silent fallback.
REQUIRED_CODE_CHALLENGE_METHOD = "S256"

#: Single non-ambiguous capture group over a bounded run of non-`"` chars —
#: linear time regardless of input size (security playbook F5).
_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="([^"]*)"')


class OAuthDiscoveryError(Exception):
    """Raised when RFC 9728 / RFC 8414 discovery or RFC 7591 registration
    cannot produce a usable client registration."""


class OAuthTokenError(Exception):
    """Raised when a token exchange/refresh call fails, or the AS's
    response cannot be parsed into a usable token set."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def build_oauth_http_client(*, timeout: float = DEFAULT_TIMEOUT_SEC) -> httpx.AsyncClient:
    """SSRF-safe, https-only ``httpx.AsyncClient`` for all outbound OAuth
    traffic (discovery, DCR, token exchange, refresh, best-effort revoke).

    ``https_only=True`` — outbound MCP OAuth traffic must never downgrade to
    cleartext, even mid-redirect-chain (spec §6 SSRF checklist).
    """
    return build_async_client(
        timeout=timeout,
        https_only=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


# ---------------------------------------------------------------------------
# RFC 9728 — protected resource metadata discovery
# ---------------------------------------------------------------------------


def _join_well_known(base_url: str, well_known_name: str) -> str:
    """RFC 9728 §3.1 well-known URI construction: insert the well-known path
    segment between the authority and the resource's own path."""
    parts = urlparse(base_url)
    suffix = parts.path.rstrip("/")
    path = f"/.well-known/{well_known_name}{suffix}"
    return urlunparse((parts.scheme, parts.netloc, path, "", "", ""))


def _json_or_discovery_error(resp: httpx.Response, url: str) -> Dict[str, Any]:
    """Parse a 200 discovery response as JSON, or raise a translated
    ``OAuthDiscoveryError`` — a junk body must surface as an actionable
    message, not an unhandled ``ValueError``/500 (Devin Review on #1124)."""
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthDiscoveryError(f"metadata document at {url!r} is not valid JSON") from exc
    if not isinstance(body, dict):
        raise OAuthDiscoveryError(f"metadata document at {url!r} is not a JSON object")
    return body


def _extract_resource_metadata_url(www_authenticate: str) -> Optional[str]:
    """Pull the ``resource_metadata`` challenge parameter out of a
    ``WWW-Authenticate`` header value, or ``None`` if absent."""
    m = _RESOURCE_METADATA_RE.search(www_authenticate or "")
    return m.group(1) if m and m.group(1) else None


def _simple_well_known(base_url: str, well_known_name: str) -> str:
    """Suffix ``base_url`` with ``/.well-known/<name>`` (spec §2's literal
    ``{source.url}/.well-known/oauth-protected-resource`` notation) — the
    resource's *own* well-known document lives directly under its own URL,
    unlike the authorization server's (see :func:`_join_well_known`, which
    implements RFC 8414's host/path-insertion rule for that case)."""
    return base_url.rstrip("/") + "/.well-known/" + well_known_name


async def discover_protected_resource_metadata(
    source_url: str,
    *,
    client: httpx.AsyncClient,
) -> Dict[str, Any]:
    """RFC 9728 protected-resource metadata for ``source_url``.

    Primary path: GET the well-known document directly. Fallback: probe
    ``source_url`` bare and read the ``resource_metadata`` challenge
    parameter off a ``401`` ``WWW-Authenticate`` header, then fetch THAT
    URL. Both hops run through ``client`` (the SSRF-safe transport) — the
    fallback URL is attacker-influenceable (comes from a live upstream
    response) if the upstream is later compromised.
    """
    # RFC 9728 §3.1 puts the well-known segment between the authority and
    # the resource's own path (path-insertion) — that form goes first. The
    # suffix form ({url}/.well-known/…) is kept as a lenient fallback for
    # servers that publish it there; the two collapse to the same URL when
    # the resource lives at the origin root. (Devin Review on #1124.)
    candidates: List[str] = []
    for url in (
        _join_well_known(source_url, "oauth-protected-resource"),
        _simple_well_known(source_url, "oauth-protected-resource"),
    ):
        if url not in candidates:
            candidates.append(url)
    primary_url = candidates[0]
    for candidate in candidates:
        try:
            resp = await client.get(candidate)
            if resp.status_code == 200:
                return _json_or_discovery_error(resp, candidate)
        except httpx.HTTPError as exc:
            logger.debug("protected-resource well-known fetch failed for %s: %s", candidate, exc_summary(exc))

    try:
        probe = await client.get(source_url)
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError(f"protected-resource discovery failed: {exc_summary(exc)}") from exc
    if probe.status_code != 401:
        raise OAuthDiscoveryError(
            "protected-resource discovery failed: no metadata document at "
            f"{primary_url!r} and no 401 challenge from {source_url!r} "
            f"(got HTTP {probe.status_code})"
        )
    meta_url = _extract_resource_metadata_url(probe.headers.get("WWW-Authenticate", ""))
    if not meta_url:
        raise OAuthDiscoveryError(
            "protected-resource discovery failed: 401 response from "
            f"{source_url!r} carries no resource_metadata challenge"
        )
    try:
        resp = await client.get(meta_url)
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError(
            f"failed to fetch resource_metadata document {meta_url!r}: {exc_summary(exc)}"
        ) from exc
    if resp.status_code != 200:
        raise OAuthDiscoveryError(f"resource_metadata document fetch {meta_url!r} returned HTTP {resp.status_code}")
    return _json_or_discovery_error(resp, meta_url)


def resolve_issuer(protected_resource_metadata: Dict[str, Any]) -> str:
    """Pick the authorization server issuer from RFC 9728 metadata.

    ``authorization_servers`` is a list; Agnes has no UI (yet) to choose
    among several, so the first entry wins — matches every other outbound
    connector's "first viable option" pattern (e.g. BigQuery IPv4
    preference in the SSRF resolver).
    """
    servers = protected_resource_metadata.get("authorization_servers") or []
    if not servers or not isinstance(servers, list):
        raise OAuthDiscoveryError("protected-resource metadata carries no 'authorization_servers' entry")
    issuer = servers[0]
    if not isinstance(issuer, str) or not issuer:
        raise OAuthDiscoveryError("protected-resource metadata's authorization_servers[0] is not a URL")
    return issuer


# ---------------------------------------------------------------------------
# RFC 8414 — authorization server metadata discovery
# ---------------------------------------------------------------------------


async def discover_as_metadata(issuer: str, *, client: httpx.AsyncClient) -> Dict[str, Any]:
    """RFC 8414 authorization-server metadata for ``issuer``.

    Enforces RFC 8414 §3.3: the document's ``issuer`` MUST be present and
    MUST match the issuer the metadata was requested for (trailing-slash
    tolerant). Beyond spec compliance this guarantees the stored client row
    and its audit record always carry a real provider identity, and adds a
    defense-in-depth layer against a compromised AS answering for a foreign
    issuer (Devin Review on #1124).
    """
    url = _join_well_known(issuer, "oauth-authorization-server")
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError(f"authorization-server metadata fetch failed: {exc_summary(exc)}") from exc
    if resp.status_code != 200:
        raise OAuthDiscoveryError(f"authorization-server metadata fetch {url!r} returned HTTP {resp.status_code}")
    body = _json_or_discovery_error(resp, url)
    meta_issuer = body.get("issuer")
    if not isinstance(meta_issuer, str) or not meta_issuer:
        raise OAuthDiscoveryError(f"authorization-server metadata at {url!r} carries no 'issuer' (RFC 8414 §3.3)")
    if meta_issuer.rstrip("/") != issuer.rstrip("/"):
        raise OAuthDiscoveryError(
            f"authorization-server metadata issuer mismatch: requested {issuer!r}, document says {meta_issuer!r}"
        )
    return body


def require_pkce_s256(as_metadata: Dict[str, Any]) -> None:
    """Fail closed (RFC 9700 §4.1) unless the AS advertises PKCE S256.

    Never downgrade to ``plain`` or no PKCE — raises
    :class:`OAuthDiscoveryError` with an explanatory message instead.
    """
    methods = as_metadata.get("code_challenge_methods_supported") or []
    if REQUIRED_CODE_CHALLENGE_METHOD not in methods:
        raise OAuthDiscoveryError(
            "authorization server does not advertise PKCE S256 support "
            f"(code_challenge_methods_supported={methods!r}); refusing to "
            "register — Agnes never downgrades to 'plain' or no PKCE (RFC 9700 §4.1)"
        )


def require_https_endpoints(as_metadata: Dict[str, Any]) -> None:
    """Fail closed unless every endpoint URL the AS advertises is https.

    Defense-in-depth alongside the SSRF-safe client's own https-only
    enforcement — this catches a misconfigured/malicious AS advertising a
    plain-http endpoint before we ever try to reach it.
    """
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
        url = as_metadata.get(key)
        if url and urlparse(url).scheme != "https":
            raise OAuthDiscoveryError(f"authorization server metadata field {key!r} is not https: {url!r}")


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` — S256 always.

    ``secrets.token_urlsafe(64)`` yields an unpadded base64url string built
    from ``[A-Za-z0-9_-]`` — a subset of RFC 7636's
    ``code-verifier`` charset (``[A-Za-z0-9-._~]``) at ~86 chars, comfortably
    inside the mandated 43-128 length window.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = create_s256_code_challenge(verifier)
    return verifier, challenge


# ---------------------------------------------------------------------------
# RFC 7591 — dynamic client registration
# ---------------------------------------------------------------------------


@dataclass
class RegisteredOAuthClient:
    """Result of a successful RFC 7591 dynamic registration (or a manually
    configured client — see ``PUT …/oauth/client``)."""

    issuer: str
    client_id: str
    client_secret: Optional[str]
    registration_access_token: Optional[str]
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str]
    scopes: Optional[str] = None


def _choose_token_endpoint_auth_method(as_metadata: Dict[str, Any]) -> str:
    """Pick the client-auth style to ANNOUNCE at registration.

    Only styles the token-call path actually implements may be announced —
    ``_client_auth_kwargs`` speaks HTTP Basic (confidential) or public-client
    (``client_id`` in the body). Announcing anything else (e.g. an AS that
    supports only ``client_secret_post``) would register a contract the token
    calls then violate, and the AS would reject every exchange/refresh
    (Devin Review on #1124) — fail closed with an actionable message instead.
    """
    supported = as_metadata.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if "client_secret_basic" in supported:
        return "client_secret_basic"
    if "none" in supported:
        return "none"
    raise OAuthDiscoveryError(
        "authorization server supports only these client-auth methods at the token endpoint: "
        f"{supported!r}; Agnes implements 'client_secret_basic' and 'none'. Configure the "
        "client manually via PUT …/oauth/client if the server offers another compatible option."
    )


async def register_dynamic_client(
    as_metadata: Dict[str, Any],
    *,
    redirect_uri: str,
    client_name: str = "Agnes",
    scopes: Optional[str] = None,
    client: httpx.AsyncClient,
) -> RegisteredOAuthClient:
    """RFC 7591 dynamic client registration against ``as_metadata``'s
    ``registration_endpoint``.

    Raises :class:`OAuthDiscoveryError` when the AS has no
    ``registration_endpoint`` (the caller should fall back to the manual
    ``PUT …/oauth/client`` escape hatch) or the response carries no
    ``client_id``.
    """
    require_https_endpoints(as_metadata)
    registration_endpoint = as_metadata.get("registration_endpoint")
    if not registration_endpoint:
        raise OAuthDiscoveryError(
            "authorization server has no 'registration_endpoint' — dynamic "
            "registration is unsupported; use PUT …/oauth/client to configure "
            "a manually-provisioned client instead"
        )
    auth_method = _choose_token_endpoint_auth_method(as_metadata)
    payload: Dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    }
    if scopes:
        payload["scope"] = scopes
    try:
        resp = await client.post(registration_endpoint, json=payload)
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError(f"dynamic client registration failed: {exc_summary(exc)}") from exc
    if resp.status_code not in (200, 201):
        raise OAuthDiscoveryError(
            f"dynamic client registration at {registration_endpoint!r} returned "
            f"HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthDiscoveryError("dynamic client registration response is not valid JSON") from exc
    client_id = body.get("client_id")
    if not client_id:
        raise OAuthDiscoveryError("dynamic client registration response carries no 'client_id'")
    # RFC 7591 §3.2.1: the AS MAY register a different auth method than the
    # one asked for, and its response — not the request — is authoritative.
    # Same fail-closed reasoning as _choose_token_endpoint_auth_method, on
    # the one path that check cannot see (Devin Review on #1124).
    granted_auth_method = body.get("token_endpoint_auth_method")
    if granted_auth_method and granted_auth_method not in ("client_secret_basic", "none"):
        raise OAuthDiscoveryError(
            f"authorization server registered the client with token_endpoint_auth_method="
            f"{granted_auth_method!r}; Agnes implements 'client_secret_basic' and 'none'. "
            "Configure the client manually via PUT …/oauth/client instead."
        )
    authorization_endpoint = as_metadata.get("authorization_endpoint")
    token_endpoint = as_metadata.get("token_endpoint")
    if not authorization_endpoint or not token_endpoint:
        raise OAuthDiscoveryError("authorization server metadata is missing authorization_endpoint/token_endpoint")
    issuer = as_metadata.get("issuer")
    if not issuer:
        # Unreachable via discover_as_metadata (which enforces RFC 8414 §3.3),
        # but direct callers with hand-built metadata must not record a blank
        # provider identity either.
        raise OAuthDiscoveryError("authorization server metadata carries no 'issuer'")
    return RegisteredOAuthClient(
        issuer=issuer,
        client_id=client_id,
        client_secret=body.get("client_secret"),
        registration_access_token=body.get("registration_access_token"),
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=registration_endpoint,
        scopes=scopes,
    )


async def best_effort_revoke_registration(
    *,
    registration_endpoint: Optional[str],
    client_id: str,
    registration_access_token: Optional[str],
    client: httpx.AsyncClient,
) -> None:
    """Best-effort RFC 7591 client deregistration ahead of a re-register.

    RFC 7591 doesn't mandate a URL shape for a client's configuration
    endpoint — the canonical way is the AS-returned ``registration_client_uri``,
    which Agnes does not persist (schema v109 keeps only the registration
    access token). Heuristic fallback: ``DELETE {registration_endpoint}/{client_id}``
    with the stored registration access token as bearer auth — the
    convention several AS implementations follow. Any failure (network,
    404/405 from an AS that doesn't support this shape, missing
    prerequisites) is swallowed — the caller proceeds to register the new
    client regardless.
    """
    if not registration_endpoint or not registration_access_token:
        return
    url = registration_endpoint.rstrip("/") + "/" + client_id
    try:
        await client.delete(url, headers={"Authorization": f"Bearer {registration_access_token}"})
    except Exception:
        logger.warning(
            "best-effort OAuth client deregistration failed for client_id=%s at %s",
            client_id,
            url,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Token exchange + refresh
# ---------------------------------------------------------------------------


@dataclass
class TokenSet:
    access_token: str
    refresh_token: Optional[str]
    expires_in: Optional[int]  # seconds; caller converts to an absolute expires_at
    scopes: Optional[str]


async def _raise_as_error(resp: httpx.Response, *, action: str) -> None:
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error", "")
            desc = body.get("error_description", "")
            detail = f"{err}: {desc}" if desc else err
    except ValueError:
        pass
    if not detail:
        detail = resp.text[:500]
    raise OAuthTokenError(f"{action} failed (HTTP {resp.status_code}): {detail}")


def _token_set_from_response(body: Dict[str, Any]) -> TokenSet:
    access_token = body.get("access_token")
    if not access_token:
        raise OAuthTokenError("token response carries no 'access_token'")
    scope = body.get("scope")
    return TokenSet(
        access_token=access_token,
        refresh_token=body.get("refresh_token"),
        expires_in=body.get("expires_in"),
        scopes=scope if isinstance(scope, str) else None,
    )


def _client_auth_kwargs(client_id: str, client_secret: Optional[str]) -> Dict[str, Any]:
    """HTTP Basic auth when a confidential client secret is present; the
    public-client (PKCE-only) path sends ``client_id`` in the form body
    instead — added by the caller."""
    if client_secret:
        return {"auth": (client_id, client_secret)}
    return {}


async def exchange_code_for_token(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: Optional[str],
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client: httpx.AsyncClient,
) -> TokenSet:
    """RFC 6749 §4.1.3 authorization-code token exchange with PKCE.

    ``token_endpoint``/``client_id``/``client_secret`` MUST come from the
    caller's ``mcp_source_oauth_clients`` row for the target source — see
    the module docstring's mix-up-defense note. Redirects are never
    followed on this call (an AS redirecting a token response is never
    legitimate).
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    try:
        resp = await client.post(
            token_endpoint,
            data=data,
            follow_redirects=False,
            **_client_auth_kwargs(client_id, client_secret),
        )
    except httpx.HTTPError as exc:
        raise OAuthTokenError(f"token exchange failed: {exc_summary(exc)}") from exc
    if resp.status_code != 200:
        await _raise_as_error(resp, action="token exchange")
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthTokenError("token exchange response is not valid JSON") from exc
    return _token_set_from_response(body)


async def refresh_access_token(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: Optional[str],
    refresh_token: str,
    client: httpx.AsyncClient,
) -> TokenSet:
    """RFC 6749 §6 refresh-token grant.

    Same mix-up-defense contract as :func:`exchange_code_for_token` —
    ``token_endpoint``/``client_id``/``client_secret`` MUST come from the
    stored ``mcp_source_oauth_clients`` row, never from caller-supplied
    request data.
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    try:
        resp = await client.post(
            token_endpoint,
            data=data,
            follow_redirects=False,
            **_client_auth_kwargs(client_id, client_secret),
        )
    except httpx.HTTPError as exc:
        raise OAuthTokenError(f"token refresh failed: {exc_summary(exc)}") from exc
    if resp.status_code != 200:
        await _raise_as_error(resp, action="token refresh")
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthTokenError("token refresh response is not valid JSON") from exc
    return _token_set_from_response(body)


def is_invalid_grant_error(exc: BaseException) -> bool:
    """True iff ``exc`` (an :class:`OAuthTokenError`) represents the AS's
    ``invalid_grant`` error — the signal that the stored refresh token is
    dead and the row should be deleted (forces re-connect) rather than
    retried."""
    return isinstance(exc, OAuthTokenError) and "invalid_grant" in str(exc)


__all__: List[str] = [
    "OAuthDiscoveryError",
    "OAuthTokenError",
    "TokenSet",
    "RegisteredOAuthClient",
    "build_oauth_http_client",
    "discover_protected_resource_metadata",
    "resolve_issuer",
    "discover_as_metadata",
    "require_pkce_s256",
    "require_https_endpoints",
    "generate_pkce_pair",
    "register_dynamic_client",
    "best_effort_revoke_registration",
    "exchange_code_for_token",
    "refresh_access_token",
    "is_invalid_grant_error",
]
