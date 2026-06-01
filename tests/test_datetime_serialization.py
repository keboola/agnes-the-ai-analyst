"""FastAPI default response class labels naive datetimes as UTC on the wire.

See `docs/superpowers/specs/2026-05-26-frontend-timezone-fix-design.md`.
"""

from datetime import datetime, timezone, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.serialization import AgnesJSONResponse, _encode_dt


def _has_offset(s: str) -> bool:
    return s.endswith("Z") or s.endswith("+00:00") or "+" in s[10:] or "-" in s[10:]


def test_encode_naive_assumes_utc_emits_offset():
    out = _encode_dt(datetime(2026, 5, 26, 12, 0, 0))
    assert _has_offset(out)


def test_encode_aware_utc_keeps_offset():
    out = _encode_dt(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc))
    assert _has_offset(out)


def test_encode_aware_offset_preserves_offset():
    dt = datetime(2026, 5, 26, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    out = _encode_dt(dt)
    assert out.endswith("+03:00")


def test_response_renders_nested_datetimes_with_offset():
    app = FastAPI(default_response_class=AgnesJSONResponse)

    @app.get("/probe")
    def probe():
        return {
            "naive": datetime(2026, 5, 26, 12, 0, 0),
            "aware": datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            "nested": {"items": [{"ts": datetime(2026, 5, 26, 12, 0, 0)}]},
        }

    body = TestClient(app).get("/probe").json()
    assert _has_offset(body["naive"])
    assert _has_offset(body["aware"])
    assert _has_offset(body["nested"]["items"][0]["ts"])


def test_response_passes_through_strings_unchanged():
    app = FastAPI(default_response_class=AgnesJSONResponse)

    @app.get("/probe")
    def probe():
        return {"label": "2026-05-26T12:00:00Z"}

    assert TestClient(app).get("/probe").json()["label"] == "2026-05-26T12:00:00Z"


import re

ISO_WITH_OFFSET = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+\-]\d{2}:\d{2})$"
)


def _all_iso_datetime_strings(obj):
    """Yield every string leaf that looks like a full ISO datetime."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _all_iso_datetime_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_iso_datetime_strings(v)
    elif isinstance(obj, str) and len(obj) >= 19 and obj[4] == "-" and obj[7] == "-" and "T" in obj:
        yield obj


def test_real_endpoint_datetimes_have_offset():
    """Smoke test against the live app — every datetime string in the
    response must carry an explicit offset."""
    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/health")
    if resp.status_code != 200:
        pytest.skip(f"/api/health returned {resp.status_code}; skip")
    for s in _all_iso_datetime_strings(resp.json()):
        assert ISO_WITH_OFFSET.match(s), f"datetime string lacks offset: {s!r}"
