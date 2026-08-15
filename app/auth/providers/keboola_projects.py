"""Keboola multi-project discovery + project-scoped token minting.

The two platform APIs behind the multi-project login live on the OAuth host
(the same host that serves ``/oauth/authorize``), not the Storage API:

- ``GET  {oauth_host}/v1/auth/token/introspect`` — the projects (id, name,
  role) the OAuth access token's user can reach, across the whole stack.
- ``POST {oauth_host}/v1/auth/pat/exchange`` — mint a project-scoped
  Personal Access Token from the OAuth access token, so Agnes can hold a
  per-project credential without ever asking a human to copy one.

Same structure as ``keboola_verify``: HTTP goes through the ``_fetch_*``
functions exclusively (tests monkeypatch them; callers import THIS MODULE),
every target is SSRF-revalidated at use time, and neither the access token
nor a minted PAT may ever appear in a log record or an error message.

Named platform assumptions (the endpoints are real but publicly
undocumented — verified against the platform team's task description,
2026-08-15; the shapes are parsed defensively):

- introspect answers ``{"projects": [{"id", "name", "role"}, ...]}`` —
  extra keys are ignored, rows without an id are dropped.
- pat/exchange accepts ``{"scope": {"projects": [<id>]}, "readOnly": bool}``
  and answers the token under ``token`` (fallbacks: ``pat``, ``value``).
  Some platform builds serve the same operation at ``/v1/auth/pat``; a 404
  or 405 from ``/pat/exchange`` retries there once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from fastapi import HTTPException

from app.auth.providers import keboola_verify as kv

logger = logging.getLogger(__name__)

INTROSPECT_TIMEOUT_SECONDS = 5.0
PAT_EXCHANGE_TIMEOUT_SECONDS = 10.0


class KeboolaProjectApiError(Exception):
    """A discovery/minting failure. ``reason`` is machine-readable; ``detail``
    is the operator-facing sentence. No token material in either."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail or reason
        super().__init__(self.detail)


@dataclass(frozen=True)
class DiscoveredProject:
    """One project from the introspect response. ``id`` is normalized to a
    string — it round-trips through JSON config columns on two backends, and
    an int-vs-str disagreement must never read as a different project."""

    id: str
    name: str
    role: str


def _validated_oauth_host() -> str:
    """The configured OAuth host, https-only and SSRF-checked at use time —
    the same bar as ``keboola_verify._fetch_verify`` (a DNS rebind between
    store and use must not point a Bearer-carrying request at a private
    address)."""
    host = kv.oauth_host()
    if not host:
        raise KeboolaProjectApiError("not_configured", "No Keboola OAuth host configured")
    if not str(host).lower().startswith("https://"):
        raise KeboolaProjectApiError("not_configured", "auth.keboola.oauth_host must be https")
    from app.api.admin import _validate_url_not_private

    try:
        _validate_url_not_private(host, "auth.keboola.oauth_host")
    except HTTPException as exc:
        raise KeboolaProjectApiError("not_configured", str(exc.detail))
    return host


def _fetch_introspect(access_token: str) -> Dict[str, Any]:
    """GET {oauth_host}/v1/auth/token/introspect. The ONLY introspect call site."""
    host = _validated_oauth_host()
    try:
        resp = httpx.get(
            f"{host}/v1/auth/token/introspect",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=INTROSPECT_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Keboola token introspect call failed: %s", type(exc).__name__)
        raise KeboolaProjectApiError("introspect_failed", "Could not reach the Keboola OAuth host to discover projects")
    if resp.status_code in (400, 401, 403):
        raise KeboolaProjectApiError("invalid_token", "Keboola rejected the access token at introspect")
    if resp.status_code != 200:
        logger.warning("Keboola token introspect returned HTTP %s", resp.status_code)
        raise KeboolaProjectApiError("introspect_failed", f"Keboola introspect returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise KeboolaProjectApiError("introspect_failed", "Keboola introspect returned a non-JSON response")


def _fetch_pat_exchange(access_token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST the PAT mint request. The ONLY pat call site.

    ``/v1/auth/pat/exchange`` first; a 404/405 retries ``/v1/auth/pat`` once
    (see the module docstring's named assumption about platform builds).
    """
    host = _validated_oauth_host()
    headers = {"Authorization": f"Bearer {access_token}"}
    last_status: Optional[int] = None
    for path in ("/v1/auth/pat/exchange", "/v1/auth/pat"):
        try:
            resp = httpx.post(f"{host}{path}", headers=headers, json=body, timeout=PAT_EXCHANGE_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            logger.warning("Keboola PAT exchange call failed: %s", type(exc).__name__)
            raise KeboolaProjectApiError(
                "pat_exchange_failed", "Could not reach the Keboola OAuth host to mint a project token"
            )
        last_status = resp.status_code
        if resp.status_code in (404, 405):
            continue
        if resp.status_code in (400, 401, 403):
            raise KeboolaProjectApiError("pat_exchange_denied", "Keboola refused to mint a project token")
        if resp.status_code not in (200, 201):
            logger.warning("Keboola PAT exchange returned HTTP %s", resp.status_code)
            raise KeboolaProjectApiError(
                "pat_exchange_failed", f"Keboola PAT exchange returned HTTP {resp.status_code}"
            )
        try:
            return resp.json()
        except ValueError:
            raise KeboolaProjectApiError("pat_exchange_failed", "Keboola PAT exchange returned a non-JSON response")
    raise KeboolaProjectApiError(
        "pat_exchange_failed", f"Keboola PAT exchange endpoint not available (HTTP {last_status})"
    )


def introspect_projects(access_token: str) -> List[DiscoveredProject]:
    """The projects the access token's user can reach, per the OAuth host.

    Rows without an id are dropped (a project we cannot address is not a
    project we can provision); name/role default to empty strings — the
    caller's role filter treats an unknown role as not-allowed.
    """
    payload = _fetch_introspect(access_token)
    raw = payload.get("projects")
    if not isinstance(raw, list):
        raise KeboolaProjectApiError("introspect_failed", "Keboola introspect response carries no projects list")
    projects: List[DiscoveredProject] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        project_id = item.get("id")
        if project_id is None or str(project_id).strip() == "":
            continue
        projects.append(
            DiscoveredProject(
                id=str(project_id).strip(),
                name=str(item.get("name") or ""),
                role=str(item.get("role") or ""),
            )
        )
    return projects


def filter_projects(projects: List[DiscoveredProject]) -> List[DiscoveredProject]:
    """Apply the instance's gates to a discovered project list.

    - ``auth.keboola.allowed_roles`` (when set) keeps only matching roles —
      the same exact-string comparison the single-project role gate uses.
    - A concrete ``auth.keboola.project_id`` (multi-project mode with a
      pinned project) narrows discovery to that one project; the ``"*"``
      wildcard (or unset id) keeps them all.
    """
    roles = kv.allowed_roles()
    if roles is not None:
        projects = [p for p in projects if p.role in roles]
    if not kv.is_wildcard_project():
        pinned = kv.configured_project_id()
        projects = [p for p in projects if pinned is not None and p.id == str(pinned)]
    return projects


def discover_allowed_projects(access_token: str) -> List[DiscoveredProject]:
    """Introspect + the instance's gates in one call — what the login flow
    (and only the login flow) needs. Raises KeboolaProjectApiError on any
    introspect failure; an empty return means the user reaches no allowed
    project (the callback's rejection case under the wildcard gate)."""
    return filter_projects(introspect_projects(access_token))


def exchange_project_pat(access_token: str, project_id: str, *, read_only: bool) -> str:
    """Mint a project-scoped PAT for ``project_id`` from the OAuth token.

    Body per the platform contract: ``scope.projects`` + ``readOnly`` (an
    admin's connection token is minted writable, everyone else read-only —
    the caller decides from the discovered role).
    """
    payload = _fetch_pat_exchange(
        access_token,
        {"scope": {"projects": [project_id]}, "readOnly": read_only},
    )
    token = payload.get("token") or payload.get("pat") or payload.get("value")
    if isinstance(token, dict):
        # Some token APIs nest the secret under the created entity.
        token = token.get("token") or token.get("value")
    if not isinstance(token, str) or not token.strip():
        raise KeboolaProjectApiError("pat_exchange_failed", "Keboola PAT exchange response carries no token value")
    return token.strip()
