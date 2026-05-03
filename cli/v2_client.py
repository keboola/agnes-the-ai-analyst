"""HTTP client helpers for /api/v2/* endpoints (CLI side)."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import io

import httpx
import pyarrow as pa

from cli.config import get_server_url, get_token
from cli.error_render import render_error


@dataclass
class V2ClientError(Exception):
    status_code: int
    body: Any
    # `message` retained for backwards compat with any existing caller
    # that reads `.message`. Renderer is the canonical str path now.
    message: str = ""

    def __str__(self) -> str:
        # Prefer the structured renderer — it pretty-prints typed BQ errors
        # (cross_project_forbidden, remote_scan_too_large, etc.) instead
        # of the historical truncate-and-flatten form. Falls back to
        # truncated form for unrecognized bodies, so we never make output
        # WORSE than the status-quo (#160 §4.7).
        return render_error(self.status_code, self.body)


def _headers() -> dict:
    token = get_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _parse_error_body(r: httpx.Response) -> Any:
    if "json" in r.headers.get("content-type", ""):
        try:
            return r.json()
        except Exception:
            return r.text
    return r.text


def api_get_json(path: str, **params) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.get(url, headers=_headers(), params=params or None, timeout=30)
    if r.status_code >= 400:
        raise V2ClientError(status_code=r.status_code, body=_parse_error_body(r))
    return r.json()


def api_post_json(path: str, payload: dict) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.post(url, json=payload, headers=_headers(), timeout=120)
    if r.status_code >= 400:
        raise V2ClientError(status_code=r.status_code, body=_parse_error_body(r))
    return r.json()


def api_post_arrow(path: str, payload: dict) -> pa.Table:
    """Post JSON, expect Arrow IPC stream response."""
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.post(url, json=payload, headers=_headers(), timeout=600)
    if r.status_code >= 400:
        raise V2ClientError(status_code=r.status_code, body=_parse_error_body(r))
    reader = pa.ipc.open_stream(io.BytesIO(r.content))
    return reader.read_all()
