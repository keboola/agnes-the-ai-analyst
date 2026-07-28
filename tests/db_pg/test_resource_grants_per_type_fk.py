"""E.3 — per-type FK on resource_grants.

When resource_type='table' and we INSERT a grant whose
resource_id_table points at a non-existent table_registry row,
the FK must reject it. Polymorphic resource_id stays as the
legacy column for backwards compatibility but the per-type column
is the FK-enforced source of truth for 5 of 6 ResourceTypes.
"""
import pytest
import sqlalchemy as sa


def test_per_type_fk_rejects_dangling_table_grant(pg_engine_with_schema):
    """Inserting a grant for a non-existent table_registry id must
    fail on the FK constraint."""
    eng = pg_engine_with_schema
    with eng.begin() as conn:
        # Seed a user_group so the FK on group_id is satisfied.
        conn.execute(sa.text(
            "INSERT INTO user_groups (id, name) VALUES ('g1', 'Test')"
        ))
    with pytest.raises(sa.exc.IntegrityError):
        with eng.begin() as conn:
            conn.execute(sa.text(
                """INSERT INTO resource_grants
                   (id, group_id, resource_type, resource_id, resource_id_table)
                   VALUES ('rg1', 'g1', 'table', 'nonexistent', 'nonexistent')"""
            ))


def test_per_type_fk_cascade_delete_on_table_removal(pg_engine_with_schema):
    """When a table_registry row is removed, dependent resource_grants
    rows cascade-delete via the new FK."""
    eng = pg_engine_with_schema
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO user_groups (id, name) VALUES ('g1', 'T')"))
        conn.execute(sa.text(
            "INSERT INTO table_registry (id, name) VALUES ('t1', 'tbl1')"
        ))
        conn.execute(sa.text(
            """INSERT INTO resource_grants
               (id, group_id, resource_type, resource_id, resource_id_table)
               VALUES ('rg1', 'g1', 'table', 't1', 't1')"""
        ))
        conn.execute(sa.text("DELETE FROM table_registry WHERE id = 't1'"))
        remaining = conn.execute(sa.text(
            "SELECT COUNT(*) FROM resource_grants WHERE id = 'rg1'"
        )).scalar()
    assert remaining == 0


def test_check_constraint_rejects_wrong_type_column_combination(pg_engine_with_schema):
    """resource_type='table' but only resource_id_data_package
    populated -> CHECK violation."""
    eng = pg_engine_with_schema
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO user_groups (id, name) VALUES ('g1', 'T')"))
        conn.execute(sa.text(
            "INSERT INTO data_packages (id, slug, name) VALUES ('dp1', 'dp1', 'DP1')"
        ))
    with pytest.raises(sa.exc.IntegrityError):
        with eng.begin() as conn:
            conn.execute(sa.text(
                """INSERT INTO resource_grants
                   (id, group_id, resource_type, resource_id, resource_id_data_package)
                   VALUES ('rg-bad', 'g1', 'table', 'dp1', 'dp1')"""
            ))


def test_marketplace_plugin_grant_allowed_with_all_per_type_columns_null(pg_engine_with_schema):
    """The 6th ResourceType (marketplace_plugin) uses the composite
    <slug>/<plugin_name> path in the legacy resource_id column;
    none of the per-type columns apply. CHECK constraint must allow
    this pattern."""
    eng = pg_engine_with_schema
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO user_groups (id, name) VALUES ('g1', 'T')"))
        conn.execute(sa.text(
            """INSERT INTO resource_grants
               (id, group_id, resource_type, resource_id)
               VALUES ('rg-mp', 'g1', 'marketplace_plugin', 'my-mp/my-plugin')"""
        ))
        n = conn.execute(sa.text(
            "SELECT COUNT(*) FROM resource_grants WHERE id = 'rg-mp'"
        )).scalar()
    assert n == 1
