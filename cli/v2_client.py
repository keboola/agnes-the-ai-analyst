"""HTTP client helpers for /api/v2/* endpoints (CLI side)."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import io

import httpx
import pyarrow as pa

from cli.config import get_server_url, get_token


@dataclass
class V2ClientError(Exception):
    status_code: int
    body: Any
    message: str = ""

    def __str__(self) -> str:
        return f"HTTP {self.status_code}: {self.message or self.body}"


def _headers() -> dict:
    token = get_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def api_get_json(path: str, **params) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.get(url, headers=_headers(), params=params or None, timeout=30)
    if r.status_code >= 400:
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        raise V2ClientError(status_code=r.status_code, body=body, message=str(body)[:200])
    return r.json()


def api_post_json(path: str, payload: dict) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.post(url, json=payload, headers=_headers(), timeout=120)
    if r.status_code >= 400:
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        raise V2ClientError(status_code=r.status_code, body=body, message=str(body)[:200])
    return r.json()


def api_post_arrow(path: str, payload: dict) -> pa.Table:
    """Post JSON, expect Arrow IPC stream response."""
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.post(url, json=payload, headers=_headers(), timeout=600)
    if r.status_code >= 400:
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        raise V2ClientError(status_code=r.status_code, body=body, message=str(body)[:200])
    reader = pa.ipc.open_stream(io.BytesIO(r.content))
    return reader.read_all()
