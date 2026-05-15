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
    """Minimal v48-shaped DB: schema_version + resource_grants table."""
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
