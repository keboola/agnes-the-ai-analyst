"""H6-NEW — _JSON_COLUMNS is derived from Base.metadata; every JSONB
column in any model is automatically present."""
from __future__ import annotations


def test_data_packages_tags_in_json_columns() -> None:
    """The H6-NEW repro target: data_packages.tags must appear without
    a code-edit when the model declares it as JSONB."""
    import src.models  # noqa: F401 — register all models
    from scripts.migrate_duckdb_to_pg.tasks import _JSON_COLUMNS

    assert ("data_packages", "tags") in _JSON_COLUMNS, (
        "_JSON_COLUMNS must include every (table, column) declared as "
        "JSONB in src.models. data_packages.tags is the H6-NEW repro."
    )


def test_json_columns_covers_every_jsonb_in_metadata() -> None:
    """Forward-compatibility: any future JSONB column lands in the set
    without manual sync."""
    import src.models  # noqa: F401
    from sqlalchemy.dialects.postgresql import JSONB
    from src.db_pg import Base
    from scripts.migrate_duckdb_to_pg.tasks import _JSON_COLUMNS

    missing = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB) and (table.name, col.name) not in _JSON_COLUMNS:
                missing.append(f"{table.name}.{col.name}")
    assert not missing, (
        "These JSONB columns are missing from _JSON_COLUMNS — derive "
        "the set dynamically:\n  " + "\n  ".join(missing)
    )


def test_build_insert_casts_jsonb_for_dynamic_table() -> None:
    """The INSERT statement built for ``data_packages`` must emit
    ``CAST(:tags AS JSONB)`` so a Python list serialises correctly."""
    from scripts.migrate_duckdb_to_pg.tasks import _build_insert

    sql = _build_insert("data_packages", ["id", "tags"], ["id"])
    assert "CAST(:tags AS JSONB)" in sql or "::JSONB" in sql, sql
