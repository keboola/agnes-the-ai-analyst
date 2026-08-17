"""Databricks SQL Statement Execution API client.

Runs SQL on a Databricks SQL warehouse over plain REST
(``POST /api/2.0/sql/statements``) — no Databricks SDK dependency, mirroring
the ``requests``-based shape of ``connectors/keboola/storage_api.py``.

Two result paths:

- ``execute_rows`` — small metadata/discovery queries. ``INLINE`` disposition
  + ``JSON_ARRAY`` format; the whole result rides the statement response.
- ``execute_to_arrow_batches`` — bulk extraction for materialization.
  ``EXTERNAL_LINKS`` disposition + ``ARROW_STREAM`` format: the warehouse
  stages the result in cloud storage and hands back short-lived presigned
  URLs, each holding an Arrow IPC stream. Links are fetched lazily per chunk
  (they expire in ~15 min) and — critically — WITHOUT the workspace bearer
  token: a presigned URL is already the credential, and forwarding the
  Databricks token to a cloud-storage host would leak it to a third party.

Auth is a workspace PAT (or OAuth M2M access token) sent only as an
``Authorization: Bearer`` header — never on the URL (see
``.claude/skills/agnes-conventions/references/security.md`` §7).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

_STATEMENTS_PATH = "/api/2.0/sql/statements"

# Statement lifecycle states, per the Statement Execution API. Anything not
# terminal keeps the poll loop alive until the caller's deadline.
_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})

# Server-side wait before the API returns PENDING and we fall back to
# polling. The API caps this parameter at 50s; 30s keeps a fast statement
# synchronous without long-blocking a scheduler thread on a cold warehouse.
_WAIT_TIMEOUT = "30s"


class DatabricksApiError(RuntimeError):
    """A statement failed, the API answered an error, or a response was
    malformed. ``status`` carries the HTTP status when the transport layer
    failed (None for statement-level failures); ``code`` carries the
    Databricks ``error_code`` when one was provided."""

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None):
        self.status = status
        self.code = code
        super().__init__(message)


class DatabricksStatementTimeoutError(DatabricksApiError):
    """The statement did not reach a terminal state before the caller's
    deadline. The client cancels the statement best-effort before raising so
    the warehouse doesn't keep burning on a result nobody will read."""

    def __init__(self, statement: str, *, timeout_s: Optional[float]):
        self.timeout_s = timeout_s
        super().__init__(
            f"Databricks statement exceeded {timeout_s}s (cancelled): {statement[:80]!r}",
            code="statement_timeout",
        )


def validate_workspace_host(host: str) -> str:
    """Normalize + validate a Databricks workspace URL.

    Accepts ``https://<workspace-host>`` (a bare host is upgraded), rejects
    anything carrying userinfo, a path, a query, or a non-https scheme —
    the host is where the bearer token is sent, so a malformed value must
    fail closed rather than leak the credential somewhere surprising.
    """
    raw = (host or "").strip().rstrip("/")
    if not raw:
        raise ValueError("Databricks host is empty")
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise ValueError(f"Databricks host must be https://, got: {host!r}")
    if not parts.hostname:
        raise ValueError(f"Databricks host has no hostname: {host!r}")
    if parts.username or parts.password:
        raise ValueError("Databricks host must not carry userinfo credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"Databricks host must be a bare workspace URL (no path/query), got: {host!r}")
    netloc = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return f"https://{netloc}"


class DatabricksStatementClient:
    """Thread-compatible Statement Execution API wrapper.

    One instance can be reused across calls — ``requests.Session`` keeps the
    HTTP keep-alive pool warm, same as ``KeboolaStorageClient``.
    """

    def __init__(
        self,
        *,
        host: str,
        token: str,
        warehouse_id: str,
        session: Optional[requests.Session] = None,
        poll_interval_s: float = 2.0,
        request_timeout_s: float = 60.0,
    ):
        self.host = validate_workspace_host(host)
        if not token:
            raise ValueError("Databricks token is empty")
        if not warehouse_id:
            raise ValueError("Databricks warehouse_id is empty")
        self.warehouse_id = warehouse_id
        self._poll_interval_s = poll_interval_s
        self._request_timeout_s = request_timeout_s
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
            session.mount("https://", adapter)
        self._session = session
        # The workspace token lives on the session's default headers so every
        # API call carries it; presigned-link downloads deliberately bypass
        # this session (see _download_external_link).
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    # ------------------------------------------------------------------ API

    def execute_rows(
        self,
        statement: str,
        *,
        catalog: Optional[str] = None,
        schema: Optional[str] = None,
        timeout_s: float = 120.0,
    ) -> Tuple[List[str], List[List[Any]]]:
        """Run a small statement and return ``(column_names, rows)``.

        INLINE + JSON_ARRAY — every value arrives as a string (or None).
        Intended for discovery/metadata queries whose result is bounded by
        construction; a truncated result raises rather than silently
        shortening what the caller believes is the full listing.
        """
        payload = self._submit_payload(
            statement, disposition="INLINE", fmt="JSON_ARRAY", catalog=catalog, schema=schema
        )
        final = self._run_to_terminal(payload, statement, timeout_s=timeout_s)
        manifest = final.get("manifest") or {}
        if manifest.get("truncated"):
            raise DatabricksApiError(
                f"inline result for {statement[:80]!r} was truncated by the API; "
                "this query class must stay under the inline result limit",
                code="inline_result_truncated",
            )
        columns = [c.get("name", "") for c in ((manifest.get("schema") or {}).get("columns") or [])]
        rows: List[List[Any]] = []
        result = final.get("result") or {}
        rows.extend(result.get("data_array") or [])
        statement_id = final.get("statement_id", "")
        next_chunk = result.get("next_chunk_index")
        while next_chunk is not None:
            chunk = self._get_json(f"{_STATEMENTS_PATH}/{statement_id}/result/chunks/{next_chunk}")
            rows.extend(chunk.get("data_array") or [])
            next_chunk = chunk.get("next_chunk_index")
        return columns, rows

    def execute_to_arrow_batches(
        self,
        statement: str,
        *,
        catalog: Optional[str] = None,
        schema: Optional[str] = None,
        byte_limit: Optional[int] = None,
        timeout_s: Optional[float] = 900.0,
        parameters: Optional[List[Dict[str, Any]]] = None,
    ) -> "ArrowResult":
        """Run a bulk statement; return an :class:`ArrowResult` whose
        ``iter_batches()`` lazily streams ``pyarrow.RecordBatch`` objects
        chunk by chunk.

        ``byte_limit`` (when positive) is passed to the API, which stops
        producing result bytes past the cap and flags the manifest
        ``truncated`` — the caller must check ``ArrowResult.truncated`` and
        treat a truncated bulk result as a failed extraction, never as data.

        ``parameters`` binds ``:name`` markers in ``statement`` through the
        API's own parameter mechanism (see ``_submit_payload``).
        """
        payload = self._submit_payload(
            statement,
            disposition="EXTERNAL_LINKS",
            fmt="ARROW_STREAM",
            catalog=catalog,
            schema=schema,
            parameters=parameters,
        )
        if byte_limit is not None and byte_limit > 0:
            payload["byte_limit"] = int(byte_limit)
        final = self._run_to_terminal(payload, statement, timeout_s=timeout_s)
        return ArrowResult(self, final)

    def cancel(self, statement_id: str) -> None:
        """Best-effort statement cancel — failure is logged, never raised."""
        try:
            resp = self._session.post(
                f"{self.host}{_STATEMENTS_PATH}/{statement_id}/cancel",
                timeout=self._request_timeout_s,
            )
            resp.raise_for_status()
        except Exception as e:  # pragma: no cover - best-effort by contract
            logger.warning("Databricks statement cancel failed for %s: %s", statement_id, e)

    # ------------------------------------------------------------- internals

    def _submit_payload(
        self,
        statement: str,
        *,
        disposition: str,
        fmt: str,
        catalog: Optional[str],
        schema: Optional[str],
        parameters: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "statement": statement,
            "warehouse_id": self.warehouse_id,
            "wait_timeout": _WAIT_TIMEOUT,
            "on_wait_timeout": "CONTINUE",
            "disposition": disposition,
            "format": fmt,
        }
        if catalog:
            payload["catalog"] = catalog
        if schema:
            payload["schema"] = schema
        if parameters:
            # The API's own named-parameter mechanism: each entry is
            # ``{"name": ..., "value": ..., "type": ...}`` and binds a ``:name``
            # marker in the statement. Values travel as request fields, never
            # spliced into SQL text — the whole point on the access-policy path,
            # where the bound values are the caller's identity.
            payload["parameters"] = list(parameters)
        return payload

    def _run_to_terminal(
        self, payload: Dict[str, Any], statement: str, *, timeout_s: Optional[float]
    ) -> Dict[str, Any]:
        # `None` and `0` both mean "no deadline" — the disable value every
        # Databricks timeout knob documents.
        deadline = time.monotonic() + timeout_s if timeout_s and timeout_s > 0 else None
        doc = self._post_json(_STATEMENTS_PATH, payload)
        while True:
            state = ((doc.get("status") or {}).get("state")) or ""
            if state in _TERMINAL_STATES:
                break
            statement_id = doc.get("statement_id")
            if not statement_id:
                raise DatabricksApiError("statement response carries no statement_id", code="malformed_response")
            if deadline is not None and time.monotonic() >= deadline:
                self.cancel(statement_id)
                raise DatabricksStatementTimeoutError(statement, timeout_s=timeout_s)
            time.sleep(self._poll_interval_s)
            doc = self._get_json(f"{_STATEMENTS_PATH}/{statement_id}")

        if state != "SUCCEEDED":
            err = (doc.get("status") or {}).get("error") or {}
            raise DatabricksApiError(
                f"Databricks statement {state}: {err.get('message') or 'no error message provided'}",
                code=err.get("error_code") or state.lower(),
            )
        return doc

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_json("POST", path, json=payload)

    def _get_json(self, path: str) -> Dict[str, Any]:
        return self._request_json("GET", path)

    def _request_json(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.host}{path}"
        try:
            resp = self._session.request(method, url, timeout=self._request_timeout_s, **kwargs)
        except requests.RequestException as e:
            raise DatabricksApiError(f"{method} {path} failed: {e}") from e
        if resp.status_code >= 400:
            detail = ""
            code = None
            try:
                body = resp.json()
                detail = body.get("message") or ""
                code = body.get("error_code")
            except ValueError:
                detail = (resp.text or "")[:200]
            raise DatabricksApiError(
                f"{method} {path} → HTTP {resp.status_code}: {detail}",
                status=resp.status_code,
                code=code,
            )
        try:
            parsed = resp.json()
        except ValueError as e:
            raise DatabricksApiError(f"{method} {path} returned non-JSON body") from e
        if not isinstance(parsed, dict):
            raise DatabricksApiError(f"{method} {path} returned non-object JSON")
        return parsed

    def _download_external_link(self, url: str) -> bytes:
        """Fetch a presigned result chunk. The URL is itself the credential;
        the workspace bearer token MUST NOT ride along (it would be handed to
        the cloud-storage host). https-only, defense-in-depth."""
        if not url.startswith("https://"):
            raise DatabricksApiError(f"refusing non-https external link: {url[:80]!r}", code="insecure_external_link")
        try:
            # Bare requests.get — deliberately NOT self._session, whose default
            # headers carry the Authorization bearer.
            resp = requests.get(url, timeout=self._request_timeout_s)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise DatabricksApiError(f"external link fetch failed: {e}") from e
        return resp.content


class ArrowResult:
    """Lazy reader over an EXTERNAL_LINKS + ARROW_STREAM statement result."""

    def __init__(self, client: DatabricksStatementClient, final_doc: Dict[str, Any]):
        self._client = client
        self._statement_id = final_doc.get("statement_id", "")
        manifest = final_doc.get("manifest") or {}
        self.truncated: bool = bool(manifest.get("truncated"))
        self.total_row_count: int = int(manifest.get("total_row_count") or 0)
        self.total_byte_count: int = int(manifest.get("total_byte_count") or 0)
        self._total_chunks: int = int(manifest.get("total_chunk_count") or 0)
        self.schema_columns: List[Dict[str, Any]] = list(((manifest.get("schema") or {}).get("columns")) or [])
        # The submit/poll response may already carry the first chunk's links.
        self._initial_links: Dict[int, str] = {}
        for link in ((final_doc.get("result") or {}).get("external_links")) or []:
            idx = link.get("chunk_index")
            href = link.get("external_link")
            if idx is not None and href:
                self._initial_links[int(idx)] = href

    def iter_batches(self) -> Iterator[Any]:
        """Yield ``pyarrow.RecordBatch`` objects across all chunks, fetching
        each presigned link right before use (they expire in minutes)."""
        import pyarrow.ipc  # deferred: keep module importable without pyarrow

        for chunk_index in range(self._total_chunks):
            href = self._initial_links.get(chunk_index) or self._resolve_link(chunk_index)
            raw = self._client._download_external_link(href)
            with pyarrow.ipc.open_stream(raw) as reader:
                yield from reader

    def _resolve_link(self, chunk_index: int) -> str:
        doc = self._client._get_json(f"{_STATEMENTS_PATH}/{self._statement_id}/result/chunks/{chunk_index}")
        for link in doc.get("external_links") or []:
            if link.get("chunk_index") == chunk_index and link.get("external_link"):
                return link["external_link"]
        raise DatabricksApiError(f"no external link for chunk {chunk_index}", code="missing_chunk_link")
