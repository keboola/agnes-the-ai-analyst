"""Parity test for internal-table schema introspection across both backends.

``/api/v2/schema/<id>`` (``agnes schema <table>``) for an internal source
(``agnes_audit`` / ``agnes_sessions`` / ``agnes_telemetry``) reads the
physical state table's columns through
``connectors.internal.access.get_schema``. That schema lives in the active
state backend — the pre-fix code queried the DuckDB-only
``information_schema`` unconditionally, so on a Postgres instance it
returned an empty (wrong) schema for a table that plainly exists.

Mirrors ``tests/db_pg/test_parity_internal_sample.py``'s fixture/pattern.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()
    return state_backend


def test_get_schema_returns_columns_on_both_backends(_env):
    from connectors.internal.access import INTERNAL_TABLES_BY_ID, get_schema

    audit_def = INTERNAL_TABLES_BY_ID["agnes_audit"]
    cols = get_schema("unused-path", audit_def.registry_id)

    names = {c["name"] for c in cols}
    assert cols, f"[{_env}] get_schema returned no columns for {audit_def.source_table!r}"
    assert "user_id" in names, f"[{_env}] expected user_id column, got {sorted(names)}"
    assert "action" in names, f"[{_env}] expected action column, got {sorted(names)}"
    assert all(set(c) == {"name", "type", "nullable"} for c in cols)


def test_get_schema_unknown_table_id_returns_empty(_env):
    from connectors.internal.access import get_schema

    assert get_schema("unused-path", "not_a_real_internal_table") == []
