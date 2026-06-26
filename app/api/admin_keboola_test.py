"""POST /api/admin/keboola/test-connection — admin-only health probe.

Lets an admin verify the saved Keboola config from /admin/server-config
WITHOUT having to wait for a sync failure. Reads stack_url and token_env
from instance config (same path as the Discover endpoint), then calls
KeboolaClient.buckets.list() — a minimal round-trip that confirms the
token is valid and the stack URL is reachable.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException

from app.auth.access import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/keboola", tags=["admin"])


@router.post("/test-connection")
def test_connection(_user: dict = Depends(require_admin)):
    """Verify the Keboola Storage API token by listing buckets.

    Declared as a plain ``def`` (not ``async``) so FastAPI runs it in the
    default threadpool executor — the underlying KeboolaClient does
    synchronous file I/O on init and synchronous HTTP on buckets.list(),
    neither of which is safe to call on the async event-loop thread.

    Returns 200 with ``{ok, stack_url, bucket_count, elapsed_ms}`` on success.

    Error responses:
    - 400 ``not_configured`` — token or URL not set
    - 400 ``invalid_token`` — Keboola returned 401
    - 502 ``keboola_upstream_error`` — other API error
    """
    from app.instance_config import get_value

    stack_url = get_value("data_source", "keboola", "stack_url", default="")
    if not stack_url:
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "not_configured",
                "hint": "stack_url is not set. Configure it in Instance settings → Data source.",
            },
        )

    token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
    token = os.environ.get(token_env, "").strip() if token_env else ""
    if not token:
        token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "").strip()
    if not token:
        try:
            from app.datasource_secrets import datasource_secret  # noqa: PLC0415

            token = (datasource_secret("KEBOOLA_STORAGE_TOKEN") or "").strip()
        except Exception:
            pass
    if not token:
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "not_configured",
                "hint": f"Token env var {token_env!r} is not set. Add it to your .env file or the datasource-credentials vault.",
            },
        )

    try:
        from connectors.keboola.client import KeboolaClient

        client = KeboolaClient(token=token, url=stack_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "not_configured",
                "hint": str(exc),
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "kind": "keboola_upstream_error",
                "hint": str(exc),
            },
        )

    started = time.monotonic()
    try:
        buckets = client.client.buckets.list()
        bucket_count = len(buckets) if buckets else 0
    except Exception as exc:
        # Inspect the HTTP status code directly from the requests HTTPError
        # rather than string-matching the message (fragile across library versions).
        http_status = None
        try:
            http_status = exc.response.status_code  # requests.exceptions.HTTPError
        except AttributeError:
            pass
        if http_status == 401 or http_status == 403:
            raise HTTPException(
                status_code=400,
                detail={
                    "kind": "invalid_token",
                    "hint": "Storage API token is invalid or expired. Check the token in your .env file.",
                },
            )
        raise HTTPException(
            status_code=502,
            detail={
                "kind": "keboola_upstream_error",
                "hint": str(exc),
            },
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": True,
        "stack_url": stack_url,
        "bucket_count": bucket_count,
        "elapsed_ms": elapsed_ms,
    }
