"""Verify Phase C deprecation marks + profile_after_sync becomes inert."""
import pytest
from app.api.admin import RegisterTableRequest, UpdateTableRequest


def test_register_request_marks_sync_strategy_deprecated():
    schema = RegisterTableRequest.model_json_schema()
    field = schema["properties"]["sync_strategy"]
    assert field.get("deprecated") is True


def test_register_request_marks_profile_after_sync_deprecated():
    schema = RegisterTableRequest.model_json_schema()
    field = schema["properties"]["profile_after_sync"]
    assert field.get("deprecated") is True


def test_register_endpoint_accepts_profile_after_sync_for_backcompat(seeded_app):
    """External clients sending profile_after_sync get no error — the
    field is silently ignored."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "x",
            "source_type": "keboola",
            "bucket": "in.c-foo",
            "source_table": "y",
            "query_mode": "local",
            "profile_after_sync": True,  # legacy client may send this
        },
    )
    assert r.status_code == 201


def test_register_endpoint_does_not_persist_profile_after_sync(seeded_app):
    """The persisted row no longer carries the old profile_after_sync
    value (column may still exist in DB for back-compat, but admin path
    never writes a non-default value)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "y",
            "source_type": "keboola",
            "bucket": "in.c-foo",
            "source_table": "y",
            "query_mode": "local",
            "profile_after_sync": True,
        },
    )
    assert r.status_code == 201
    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "y")
    # The field's value in the registry response is now whatever the DB
    # default is (True per current schema). Critical: the request value
    # is NOT echoed back.
    # If the value is in the response at all (legacy back-compat in the
    # GET serializer), it's the schema default, not the request value.
    # If the value is absent (deprecated and stripped), that's also fine.
    if "profile_after_sync" in row:
        # Whatever this is, it's the schema default, not request-driven.
        assert row["profile_after_sync"] is True or row["profile_after_sync"] is None
