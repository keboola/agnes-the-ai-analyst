"""Every CLI surface that gates by ``can_access_table`` returns the
SAME actionable 403 detail string when an analyst hits a table not in
their stack.

Stack-gated RBAC removed per-table ``resource_grants`` as a visibility
path for analysts. The new failure mode — analyst queries a table that
isn't in any data package they've subscribed to — must surface as a
consistent, copy-able error so the user knows to ask an admin to wrap
the table in a Data Package.

This test fans out across the four CLI-facing endpoints that all hit
``can_access_table``:
  * GET  /api/data/{table_id}/download
  * POST /api/data/{table_id}/check-access
  * POST /api/v2/sample
  * POST /api/v2/schema
plus the in-process helper ``src.rbac.table_not_in_stack_message`` that
all of them route through.
"""

from __future__ import annotations


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_table(client, admin_token: str, table_id: str) -> None:
    """Admin registers a table WITHOUT wrapping it in any data_package
    so every analyst-side gate fires."""
    r = client.post(
        "/api/admin/register-table",
        json={
            "name": table_id, "source_type": "keboola",
            "query_mode": "local",
        },
        headers=_auth(admin_token),
    )
    assert r.status_code in (200, 201, 409), r.text


def _expect_stack_message(detail: object, table_id: str) -> None:
    """Assert the 403 detail contains the standard stack-gated copy."""
    if isinstance(detail, dict):
        detail = detail.get("detail") or detail.get("message") or ""
    detail = str(detail)
    assert table_id in detail, f"missing table id in 403 detail: {detail!r}"
    assert "stack" in detail.lower() or "data package" in detail.lower(), (
        f"403 detail must mention stack / Data Package — got {detail!r}"
    )


class TestTableNotInStackMessage:
    def test_helper_message_contains_table_id_and_data_package(self):
        """In-process helper — every API route should pipe through this
        so the wording stays consistent."""
        from src.rbac import table_not_in_stack_message
        msg = table_not_in_stack_message("foo_table")
        assert "foo_table" in msg
        assert "Data Package" in msg
        assert "agnes pull" in msg, "actionable next-step must mention `agnes pull`"

    def test_data_download_returns_stack_gated_403(self, seeded_app):
        _register_table(seeded_app["client"], seeded_app["admin_token"], "secret_data")
        r = seeded_app["client"].get(
            "/api/data/secret_data/download",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403
        _expect_stack_message(r.json().get("detail"), "secret_data")

    def test_check_access_returns_stack_gated_403(self, seeded_app):
        _register_table(seeded_app["client"], seeded_app["admin_token"], "secret_data2")
        r = seeded_app["client"].get(
            "/api/data/secret_data2/check-access",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403
        _expect_stack_message(r.json().get("detail"), "secret_data2")
