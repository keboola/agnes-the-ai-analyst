"""#607 — `server_only` distribution flag validation + manifest plumbing.

The flag decouples distribution from query_mode. It is only meaningful for
query_mode IN ('local', 'materialized'); pairing server_only=true with
query_mode='remote' is incoherent (no server-stored parquet to suppress)
and must be rejected at register/update time.
"""
import pytest
from pydantic import ValidationError

from app.api.admin import RegisterTableRequest, UpdateTableRequest


def _base(**overrides):
    p = {
        "name": "tbl",
        "source_type": "keboola",
        "bucket": "in.c-x",
        "source_table": "tbl",
        "query_mode": "local",
    }
    p.update(overrides)
    return p


# ───────────────────────── RegisterTableRequest ─────────────────────────


def test_server_only_defaults_false():
    req = RegisterTableRequest(**_base())
    assert req.server_only is False


def test_server_only_true_with_local_accepted():
    req = RegisterTableRequest(**_base(query_mode="local", server_only=True))
    assert req.server_only is True


def test_server_only_true_with_materialized_accepted():
    req = RegisterTableRequest(**_base(
        query_mode="materialized",
        source_query='{"columns": ["id"]}',
        server_only=True,
    ))
    assert req.server_only is True


def test_server_only_true_with_remote_rejected():
    with pytest.raises(ValidationError, match="server_only"):
        RegisterTableRequest(**_base(query_mode="remote", server_only=True))


def test_server_only_false_with_remote_accepted():
    # The flag only conflicts when *true*; false is the default no-op.
    req = RegisterTableRequest(**_base(query_mode="remote", server_only=False))
    assert req.server_only is False


# ───────────────────────── UpdateTableRequest ─────────────────────────


def test_update_server_only_field_present():
    req = UpdateTableRequest(server_only=True)
    assert req.server_only is True


def test_update_server_only_omitted_is_none():
    # PUT-shape: omitted means "keep existing", surfaced as None / unset.
    req = UpdateTableRequest(name="x")
    assert req.server_only is None
    assert "server_only" not in req.model_dump(exclude_unset=True)


# ───────────────────────── manifest plumbing ─────────────────────────


def test_table_manifest_entry_includes_server_only_true():
    from app.api.sync import _table_manifest_entry
    entry = _table_manifest_entry(
        {"table_id": "t1", "hash": "h"},
        {"id": "t1", "query_mode": "local", "server_only": True},
    )
    assert entry["server_only"] is True


def test_table_manifest_entry_defaults_server_only_false():
    from app.api.sync import _table_manifest_entry
    entry = _table_manifest_entry(
        {"table_id": "t1", "hash": "h"},
        {"id": "t1", "query_mode": "local"},
    )
    assert entry["server_only"] is False


# ──────────────── BQ query_mode coercion bypass (#630 review) ────────────────


def test_bq_live_coercion_cannot_bypass_server_only_validator(monkeypatch):
    """A live BQ registration arrives with the default query_mode='local',
    passes the Pydantic validator, and is then coerced to 'remote' by
    ``_validate_bigquery_register_payload`` — the post-coercion check must
    reject server_only=true there instead of persisting the incoherent row."""
    from fastapi import HTTPException

    from app.api.admin import _validate_bigquery_register_payload

    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *a, **k: "my-test-project",
    )
    req = RegisterTableRequest(**_base(
        source_type="bigquery",
        bucket="analytics",
        source_table="orders",
        query_mode="local",  # pre-coercion default — Pydantic passes
        server_only=True,
    ))
    with pytest.raises(HTTPException) as exc:
        _validate_bigquery_register_payload(req)
    assert exc.value.status_code == 422
    assert "server_only" in str(exc.value.detail)


def test_bq_materialized_keeps_server_only(monkeypatch):
    """Materialized BQ rows early-return before the remote coercion — the
    flag stays valid there (that's the one coherent BQ pairing)."""
    from app.api.admin import _validate_bigquery_register_payload

    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *a, **k: "my-test-project",
    )
    req = RegisterTableRequest(**_base(
        source_type="bigquery",
        bucket="analytics",
        source_table="orders",
        query_mode="materialized",
        server_only=True,
    ))
    _validate_bigquery_register_payload(req)
    assert req.query_mode == "materialized"
    assert req.server_only is True
