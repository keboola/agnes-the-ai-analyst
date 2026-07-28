"""Keboola Metastore API client — semantic layer (datasets, metrics,
relationships, constraints, glossary).

Separate service at ``metastore.<stack>``, derived from the project's
Storage API URL (``connection.<stack>``) by string substitution. Same
``X-StorageApi-Token`` auth as Storage API.

**Requires a master (owner) Storage API token.** Verified live
(2026-07-15): a non-master token — even one with full bucket read/manage
permissions — gets ``401 {"exception": "Failed to create project scope"}``
on every repository endpoint; the project's master token succeeds
immediately. See ``connectors/keboola/semantic_layer.py``'s
``_require_master_token`` for the preflight check that turns this opaque
error into an actionable one.

GET-only for v1 — this client only reads the semantic layer, it never
writes to it.
"""

from __future__ import annotations

from typing import Any, Optional

import requests


class MetastoreApiError(RuntimeError):
    """Wraps a non-2xx Metastore API response with the parsed body for context."""

    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def derive_metastore_url(storage_api_url: str) -> str:
    """Derive the Metastore base URL from a Storage API stack URL.

    e.g. ``https://connection.us-east4.gcp.keboola.com`` ->
    ``https://metastore.us-east4.gcp.keboola.com``. Cloud/region-agnostic —
    only the ``connection.`` -> ``metastore.`` substitution matters.
    """
    return storage_api_url.rstrip("/").replace("connection.", "metastore.", 1)


class MetastoreClient:
    """GET-only client for the Keboola Metastore (semantic layer) API."""

    def __init__(self, *, url: str, token: str, session: Optional[requests.Session] = None):
        if not url or not token:
            raise ValueError("MetastoreClient requires url and token")
        self.base = derive_metastore_url(url) + "/api/v1"
        self.token = token
        self.session = session or requests.Session()

    def _headers(self) -> dict:
        return {"X-StorageApi-Token": self.token, "Accept": "application/json"}

    def _get(self, path: str) -> dict:
        url = f"{self.base}{path}"
        resp = self.session.get(url, headers=self._headers(), timeout=30)
        return self._parse(resp, "GET", url)

    def _parse(self, resp: requests.Response, method: str, url: str) -> dict:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        if resp.status_code >= 400:
            redacted = self._redact(body)
            raise MetastoreApiError(
                f"{method} {url} -> HTTP {resp.status_code}: {redacted}",
                status=resp.status_code,
                body=body,
            )
        if not isinstance(body, dict):
            raise MetastoreApiError(
                f"{method} {url} -> unexpected non-JSON response: {str(body)[:200]}",
                status=resp.status_code,
                body=body,
            )
        return body

    def _redact(self, body: Any) -> str:
        s = str(body)
        if self.token and self.token in s:
            s = s.replace(self.token, "<redacted-storage-token>")
        return s[:500]

    def list_items(self, item_type: str, model_uuid: Optional[str] = None) -> list[dict]:
        """List all items of ``item_type`` (e.g. ``'semantic-model'``,
        ``'semantic-dataset'``, ``'semantic-metric'``, ``'semantic-constraint'``),
        optionally filtered to one model.

        Returns the raw item shape: ``{"type", "id", "attributes", "meta"}``.
        Filtering is client-side on ``attributes.modelUUID`` — the server's
        ``?modelId=`` query param is documented upstream (kbagent CLI) as
        historically unreliable, so the defensive client-side filter wins.
        """
        body = self._get(f"/repository/{item_type}")
        items = body.get("data", []) if isinstance(body, dict) else []
        if model_uuid is None:
            return items
        return [i for i in items if (i.get("attributes") or {}).get("modelUUID") == model_uuid]
