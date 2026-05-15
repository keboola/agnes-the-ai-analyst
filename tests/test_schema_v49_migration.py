"""v48 → v49 migration: unified stack for Data Packages + Memory.

Adds ``resource_grants.requirement`` enum, ``knowledge_items.is_required``
boolean (splitting the ``status='mandatory'`` overload), new tables
``data_packages``, ``data_package_tables``, ``memory_domains``,
``knowledge_item_domains``, ``user_stack_subscriptions``, seeds canonical
memory domains, re-points ``memory_domain`` grants from slug to id, and
drops the legacy scalar ``knowledge_items.domain`` column.
"""

import duckdb
import pytest

from src.db import _v48_to_v49


def _seed_v48(conn):
    """Minimal v48-shaped DB: schema_version + resource_grants + knowledge_items
    + table_registry. The v49 migration ALTERs knowledge_items and CREATEs
    junctions referencing table_registry, so all four must exist for the
    migration to succeed. Real v48 DBs always have these (knowledge_items
    since v15, table_registry since the v1 baseline)."""
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (48)")
    conn.execute("CREATE TABLE user_groups (id VARCHAR PRIMARY KEY, name VARCHAR)")
    conn.execute(
        """
        CREATE TABLE resource_grants (
            id VARCHAR PRIMARY KEY,
            group_id VARCHAR,
            resource_type VARCHAR,
            resource_id VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE knowledge_items (
            id VARCHAR PRIMARY KEY,
            title VARCHAR,
            status VARCHAR,
            domain VARCHAR
        )
        """
    )
    conn.execute("CREATE TABLE table_registry (id VARCHAR PRIMARY KEY, name VARCHAR)")


def test_v48_to_v49_adds_requirement_column():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v48_to_v49(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info('resource_grants')").fetchall()]
    assert "requirement" in cols


def test_v48_to_v49_requirement_defaults_to_available():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    conn.execute("INSERT INTO user_groups (id, name) VALUES ('g1', 'Sales')")
    conn.execute(
        "INSERT INTO resource_grants (id, group_id, resource_type, resource_id) "
        "VALUES ('grant1', 'g1', 'data_package', 'pkg_sales')"
    )
    _v48_to_v49(conn)
    row = conn.execute(
        "SELECT requirement FROM resource_grants WHERE id='grant1'"
    ).fetchone()
    assert row[0] == "available"


def test_v48_to_v49_migrates_status_mandatory_to_is_required():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    conn.execute("INSERT INTO knowledge_items VALUES ('k1', 'Mandatory rule', 'mandatory', NULL)")
    conn.execute("INSERT INTO knowledge_items VALUES ('k2', 'Regular rule', 'approved', NULL)")

    _v48_to_v49(conn)

    rows = conn.execute(
        "SELECT id, status, is_required FROM knowledge_items ORDER BY id"
    ).fetchall()
    assert rows[0] == ("k1", "approved", True)   # promoted from mandatory
    assert rows[1] == ("k2", "approved", False)  # unchanged


def test_v48_to_v49_adds_is_required_default_false_for_new_rows():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v48_to_v49(conn)
    conn.execute(
        "INSERT INTO knowledge_items (id, title, status) VALUES ('k3', 'New', 'approved')"
    )
    row = conn.execute("SELECT is_required FROM knowledge_items WHERE id='k3'").fetchone()
    assert row[0] is False


def test_v48_to_v49_creates_data_packages_tables():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v48_to_v49(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info('data_packages')").fetchall()}
    assert {
        "id",
        "slug",
        "name",
        "description",
        "icon",
        "color",
        "created_by",
        "created_at",
        "updated_at",
    }.issubset(cols)

    jt_cols = {r[1] for r in conn.execute("PRAGMA table_info('data_package_tables')").fetchall()}
    assert {"package_id", "table_id", "added_at", "added_by"}.issubset(jt_cols)


def test_v48_to_v49_data_packages_slug_unique():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v48_to_v49(conn)
    conn.execute("INSERT INTO data_packages(id, slug, name) VALUES ('p1', 'sales', 'Sales')")
    with pytest.raises(duckdb.ConstraintException):
        conn.execute("INSERT INTO data_packages(id, slug, name) VALUES ('p2', 'sales', 'Sales B')")
