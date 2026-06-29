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

from src.db import _v51_to_v52


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
    _v51_to_v52(conn)
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
    _v51_to_v52(conn)
    row = conn.execute(
        "SELECT requirement FROM resource_grants WHERE id='grant1'"
    ).fetchone()
    assert row[0] == "available"


def test_v48_to_v49_migrates_status_mandatory_to_is_required():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    conn.execute("INSERT INTO knowledge_items VALUES ('k1', 'Mandatory rule', 'mandatory', NULL)")
    conn.execute("INSERT INTO knowledge_items VALUES ('k2', 'Regular rule', 'approved', NULL)")

    _v51_to_v52(conn)

    rows = conn.execute(
        "SELECT id, status, is_required FROM knowledge_items ORDER BY id"
    ).fetchall()
    assert rows[0] == ("k1", "approved", True)   # promoted from mandatory
    assert rows[1] == ("k2", "approved", False)  # unchanged


def test_v48_to_v49_adds_is_required_default_false_for_new_rows():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v51_to_v52(conn)
    conn.execute(
        "INSERT INTO knowledge_items (id, title, status) VALUES ('k3', 'New', 'approved')"
    )
    row = conn.execute("SELECT is_required FROM knowledge_items WHERE id='k3'").fetchone()
    assert row[0] is False


def test_v48_to_v49_creates_data_packages_tables():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v51_to_v52(conn)

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
    _v51_to_v52(conn)
    conn.execute("INSERT INTO data_packages(id, slug, name) VALUES ('p1', 'sales', 'Sales')")
    with pytest.raises(duckdb.ConstraintException):
        conn.execute("INSERT INTO data_packages(id, slug, name) VALUES ('p2', 'sales', 'Sales B')")


def test_v48_to_v49_creates_memory_domain_tables():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v51_to_v52(conn)

    md_cols = {r[1] for r in conn.execute("PRAGMA table_info('memory_domains')").fetchall()}
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
    }.issubset(md_cols)

    jd_cols = {r[1] for r in conn.execute("PRAGMA table_info('knowledge_item_domains')").fetchall()}
    assert {"item_id", "domain_id", "added_at", "added_by"}.issubset(jd_cols)


def test_v48_to_v49_memory_domains_slug_unique():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v51_to_v52(conn)
    conn.execute(
        "INSERT INTO memory_domains(id, slug, name) VALUES ('md_x', 'custom', 'Custom')"
    )
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO memory_domains(id, slug, name) VALUES ('md_y', 'custom', 'Custom dup')"
        )


def test_v48_to_v49_seeds_canonical_domains():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v51_to_v52(conn)

    slugs = {r[0] for r in conn.execute("SELECT slug FROM memory_domains").fetchall()}
    expected = {"finance", "engineering", "product", "data", "operations", "infrastructure"}
    assert expected.issubset(slugs)

    # Deterministic IDs — frontend / migration callers can rely on the
    # md_<slug> convention.
    row = conn.execute("SELECT id FROM memory_domains WHERE slug='finance'").fetchone()
    assert row[0] == "md_finance"


def test_v48_to_v49_seeds_extra_non_canonical_domains():
    """Defensive: a v48 DB with a non-VALID_DOMAINS domain value gets its
    own memory_domains row so the junction backfill (task 1.6) doesn't
    drop the relation."""
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    conn.execute(
        "INSERT INTO knowledge_items VALUES ('k1', 't1', 'approved', 'sales-coaching')"
    )
    _v51_to_v52(conn)

    row = conn.execute(
        "SELECT id, slug, name FROM memory_domains WHERE name = 'sales-coaching'"
    ).fetchone()
    assert row is not None
    assert row[1] == "sales-coaching"
    assert row[0].startswith("md_")


def test_v48_to_v49_populates_item_domains_junction():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    conn.execute("INSERT INTO knowledge_items VALUES ('k1', 't1', 'approved', 'finance')")
    conn.execute("INSERT INTO knowledge_items VALUES ('k2', 't2', 'approved', 'sales-coaching')")
    conn.execute("INSERT INTO knowledge_items VALUES ('k3', 't3', 'approved', NULL)")
    conn.execute("INSERT INTO knowledge_items VALUES ('k4', 't4', 'approved', '')")
    _v51_to_v52(conn)

    junction = conn.execute(
        "SELECT kid.item_id, md.slug "
        "  FROM knowledge_item_domains kid "
        "  JOIN memory_domains md ON md.id = kid.domain_id "
        " ORDER BY kid.item_id"
    ).fetchall()
    assert ("k1", "finance") in junction
    assert ("k2", "sales-coaching") in junction
    # NULL and empty-string domain → no junction row
    assert not any(r[0] == "k3" for r in junction)
    assert not any(r[0] == "k4" for r in junction)


def test_v48_to_v49_repoints_memory_domain_grants_to_id():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    conn.execute("INSERT INTO user_groups VALUES ('g1', 'Sales')")
    # Pre-v49 grant: resource_id is the slug
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('grant1', 'g1', 'memory_domain', 'finance')"
    )
    _v51_to_v52(conn)

    row = conn.execute(
        "SELECT resource_id FROM resource_grants WHERE id='grant1'"
    ).fetchone()
    assert row[0] == "md_finance"


def test_v48_to_v49_leaves_orphan_grants_intact():
    """Grants pointing at a non-existent domain slug are left as-is for admin
    cleanup per spec D14 (defensive — no silent data drop)."""
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    conn.execute("INSERT INTO user_groups VALUES ('g1', 'Sales')")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('orphan', 'g1', 'memory_domain', 'no-such-domain')"
    )
    _v51_to_v52(conn)
    row = conn.execute("SELECT resource_id FROM resource_grants WHERE id='orphan'").fetchone()
    assert row[0] == "no-such-domain"  # unchanged


def test_v48_to_v49_creates_user_stack_subscriptions():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v51_to_v52(conn)

    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info('user_stack_subscriptions')").fetchall()
    }
    assert {"user_id", "resource_type", "resource_id", "subscribed_at"}.issubset(cols)


def test_v48_to_v49_user_stack_subscriptions_composite_pk():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v51_to_v52(conn)
    conn.execute(
        "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id) "
        "VALUES ('u1', 'data_package', 'pkg_sales')"
    )
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id) "
            "VALUES ('u1', 'data_package', 'pkg_sales')"
        )


def test_v48_to_v49_drops_knowledge_items_domain_column():
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    conn.execute("INSERT INTO knowledge_items VALUES ('k1', 't1', 'approved', 'finance')")
    _v51_to_v52(conn)

    cols = [r[1] for r in conn.execute("PRAGMA table_info('knowledge_items')").fetchall()]
    assert "domain" not in cols

    # The migrated relation lives in the junction now.
    row = conn.execute(
        "SELECT COUNT(*) FROM knowledge_item_domains kid "
        "  JOIN memory_domains md ON md.id = kid.domain_id "
        " WHERE kid.item_id = 'k1' AND md.slug = 'finance'"
    ).fetchone()
    assert row[0] == 1


def test_schema_version_is_at_least_49():
    """After v50 cover_image_url bump, SCHEMA_VERSION should be >= 49 — the
    v49 unified-stack tables shipped at v49 and remain present through the
    cover-image bump. The exact-current-version guard lives in
    test_db_schema_version.py."""
    from src.db import SCHEMA_VERSION
    assert SCHEMA_VERSION >= 49


def test_v48_to_v49_bumps_schema_version_row():
    """The migration body stamps the schema_version row to the version
    it claims to land at. Originally v49 (called ``_v48_to_v49``), the
    branch's chain was renumbered to v51 on the first merge with main
    and again to v52 on the second merge to make room for main's new
    v51 (bq_fqn). The function name is now ``_v51_to_v52``."""
    conn = duckdb.connect(":memory:")
    _seed_v48(conn)
    _v51_to_v52(conn)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    assert row[0] == 52


def test_fresh_install_lands_at_current_version():
    """End-to-end: a brand-new DB hitting ``_ensure_schema`` ends at
    SCHEMA_VERSION with all v49 unified-stack tables in place."""
    from src.db import _ensure_schema, get_schema_version, SCHEMA_VERSION

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "data_packages" in tables
    assert "data_package_tables" in tables
    assert "memory_domains" in tables
    assert "knowledge_item_domains" in tables
    assert "user_stack_subscriptions" in tables

    # ``requirement`` column present on a fresh ``resource_grants``.
    rg_cols = {
        r[1] for r in conn.execute("PRAGMA table_info('resource_grants')").fetchall()
    }
    assert "requirement" in rg_cols

    # ``is_required`` present and ``domain`` absent on a fresh ``knowledge_items``.
    ki_cols = {
        r[1] for r in conn.execute("PRAGMA table_info('knowledge_items')").fetchall()
    }
    assert "is_required" in ki_cols
    assert "domain" not in ki_cols
