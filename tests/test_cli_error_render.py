"""CLI structured error renderer for typed BigQuery error responses.

The server side maps BQ Forbidden/auth/badrequest errors into typed
BqAccessError dicts that the FastAPI handler returns as `detail` JSON.
Today the CLI side flattens them via `f"HTTP {code}: {body[:200]}"`,
truncating the structured shape and hiding the operator-facing hints.

`cli.error_render.render_error` recognizes a few canonical shapes and
pretty-prints them; falls back to truncated form for anything else.

Closes part of #160 §4.7.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def render_error():
    from cli.error_render import render_error
    return render_error


def test_renders_typed_bq_access_error(render_error):
    """`{detail: {kind, hint, billing_project, data_project}}` from
    BqAccessError surfaces as a multi-line block with the kind line, the
    key/value pairs, and the hint word-wrapped at 80 cols."""
    body = {"detail": {
        "kind": "cross_project_forbidden",
        "message": "USER_PROJECT_DENIED on bigquery.googleapis.com",
        "billing_project": "",
        "data_project": "prj-example-data-001",
        "hint": (
            "Set data_source.bigquery.billing_project in /admin/server-config "
            "to a project where the SA has serviceusage.services.use, or "
            "grant the SA that role on the data project."
        ),
    }}
    out = render_error(502, body)
    # Single-line `f"HTTP {code}: ..."` style is the OLD form. The new
    # renderer must produce multi-line output.
    assert "\n" in out
    # Kind appears prominently in the output.
    assert "cross_project_forbidden" in out
    # Key/value pairs visible.
    assert "billing_project" in out
    assert "prj-example-data-001" in out
    # Hint text included.
    assert "serviceusage.services.use" in out


def test_renders_remote_scan_too_large(render_error):
    """`{detail: {reason: 'remote_scan_too_large', scan_bytes, limit_bytes,
    tables, suggestion}}` from the new /api/query guardrail formats with
    the bytes + tables + suggestion clearly visible."""
    body = {"detail": {
        "reason": "remote_scan_too_large",
        "scan_bytes": 10737418240,  # 10 GiB
        "limit_bytes": 5368709120,  # 5 GiB
        "tables": ["finance.unit_economics"],
        "suggestion": (
            "Use `da fetch <id> --select <cols> --where <predicate> "
            "--estimate` to materialize a filtered subset, then query "
            "the snapshot locally."
        ),
    }}
    out = render_error(400, body)
    assert "remote_scan_too_large" in out
    assert "10737418240" in out or "10 GiB" in out or "10737418240" in str(out)
    assert "finance.unit_economics" in out
    assert "da fetch" in out


def test_renders_bq_path_not_registered(render_error):
    """`{detail: {reason: 'bq_path_not_registered', path, hint}}` from the
    RBAC patch formats path + hint clearly."""
    body = {"detail": {
        "reason": "bq_path_not_registered",
        "path": 'bq."secret_ds"."secret_tbl"',
        "hint": "Direct bq.* references must point to a registered table.",
    }}
    out = render_error(403, body)
    assert "bq_path_not_registered" in out
    assert 'secret_ds' in out
    assert "registered table" in out


def test_falls_back_to_truncated_for_unrecognized_shape(render_error):
    """Body without recognizable typed shape falls back to truncated form."""
    body = "Internal Server Error: Something went wrong" * 20  # 800+ chars
    out = render_error(500, body)
    # Old-style truncation kicks in; output is single-line and short.
    assert len(out) < 600
    assert "500" in out


def test_falls_back_when_detail_is_string(render_error):
    """Many old endpoints return `detail: "<string message>"` — render that
    as-is without trying to walk it as a structured error dict."""
    body = {"detail": "Only single SELECT queries are allowed"}
    out = render_error(400, body)
    assert "Only single SELECT" in out
