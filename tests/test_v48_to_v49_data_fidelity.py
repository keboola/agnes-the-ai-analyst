"""End-to-end v48 → v49 migration test against a realistic synthetic v48 fixture.

Verifies that ``status='mandatory'`` items, scalar ``knowledge_items.domain``
values, and ``memory_domain`` grants pointing at slug strings all land
correctly post-migration without data loss or orphans — and that the new
``data_packages`` / ``user_stack_subscriptions`` tables exist with the right
shape on a freshly-migrated v48 DB.

The fixture mirrors the shape a production v48 DB would have (corporate
memory + FTS + marketplace telemetry refactor already applied) so a regression
in any of the v49 steps is caught against realistic data, not just unit-level
seeds.
"""
import duckdb

from src.db import _v50_to_v51


def _seed_realistic_v48(conn):
    """Create a minimal but realistic v48-shaped DB."""
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (48)")
    conn.execute("CREATE TABLE user_groups (id VARCHAR PRIMARY KEY, name VARCHAR)")
    conn.execute(
        "INSERT INTO user_groups VALUES ('grp_sales', 'Sales'), ('grp_eng', 'Engineering')"
    )
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
    # Three memory_domain grants — two canonical (finance, engineering) plus
    # one non-canonical (sales-coaching) to exercise the defensive seed path.
    conn.execute(
        """
        INSERT INTO resource_grants(id, group_id, resource_type, resource_id) VALUES
            ('g1', 'grp_sales', 'memory_domain', 'finance'),
            ('g2', 'grp_eng',   'memory_domain', 'engineering'),
            ('g3', 'grp_sales', 'memory_domain', 'sales-coaching'),
            ('g4', 'grp_sales', 'data_package', 'pkg_pre_existing')
        """
    )
    conn.execute("CREATE TABLE table_registry (id VARCHAR PRIMARY KEY, name VARCHAR)")
    conn.execute(
        """
        CREATE TABLE knowledge_items (
            id VARCHAR PRIMARY KEY,
            title VARCHAR,
            content TEXT,
            status VARCHAR,
            domain VARCHAR
        )
        """
    )
    conn.execute(
        """
        INSERT INTO knowledge_items VALUES
            ('k1', 'GDPR rule',          'content', 'mandatory', 'finance'),
            ('k2', 'MEDDPICC',           'content', 'approved',  'sales-coaching'),
            ('k3', 'Code review SOP',    'content', 'mandatory', 'engineering'),
            ('k4', 'Onboarding script',  'content', 'pending',   NULL),
            ('k5', 'Personal pref',      'content', 'approved',  'product')
        """
    )
    # Marketplace telemetry tables present (v48 baseline includes them, and
    # we want to verify v49 doesn't disturb them).
    conn.execute(
        """
        CREATE TABLE usage_marketplace_item_daily (
            day            DATE,
            source         VARCHAR,
            type           VARCHAR,
            parent_plugin  VARCHAR,
            name           VARCHAR,
            count          INTEGER,
            distinct_users INTEGER,
            error_count    INTEGER
        )
        """
    )


def test_full_migration_fidelity():
    conn = duckdb.connect(":memory:")
    _seed_realistic_v48(conn)
    _v50_to_v51(conn)

    # 1) is_required correctly migrated. status returns to 'approved' for
    # all 'mandatory' rows; non-mandatory rows stay untouched.
    by_id = {
        r[0]: r
        for r in conn.execute(
            "SELECT id, status, is_required FROM knowledge_items ORDER BY id"
        ).fetchall()
    }
    assert by_id["k1"] == ("k1", "approved", True)
    assert by_id["k2"] == ("k2", "approved", False)
    assert by_id["k3"] == ("k3", "approved", True)
    assert by_id["k4"] == ("k4", "pending",  False)
    assert by_id["k5"] == ("k5", "approved", False)

    # 2) memory_domains seeded — six canonical + the one non-canonical
    # 'sales-coaching' picked up by the defensive backfill.
    slugs = {r[0] for r in conn.execute("SELECT slug FROM memory_domains").fetchall()}
    expected_canonical = {
        "finance", "engineering", "product", "data", "operations", "infrastructure",
    }
    assert expected_canonical.issubset(slugs)
    assert "sales-coaching" in slugs

    # 3) knowledge_item_domains junction populated. NULL domain on k4 →
    # no row; the remaining four items each land one row.
    j_rows = conn.execute(
        "SELECT kid.item_id, md.slug "
        "  FROM knowledge_item_domains kid "
        "  JOIN memory_domains md ON md.id = kid.domain_id "
        " ORDER BY kid.item_id"
    ).fetchall()
    assert ("k1", "finance") in j_rows
    assert ("k2", "sales-coaching") in j_rows
    assert ("k3", "engineering") in j_rows
    assert ("k5", "product") in j_rows
    assert not any(r[0] == "k4" for r in j_rows)
    assert len(j_rows) == 4

    # 4) Grants re-pointed from slug strings to memory_domains.id.
    grants = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT id, resource_id FROM resource_grants WHERE resource_type='memory_domain'"
        ).fetchall()
    }
    assert grants["g1"] == "md_finance"
    assert grants["g2"] == "md_engineering"
    # Non-canonical domain ID derives from the slug-normalized form; the
    # underscore variant matches the migration's regexp_replace expression.
    assert grants["g3"] == "md_sales_coaching"

    # 5) resource_grants.requirement default. All four pre-existing grants
    # get 'available' since the migration didn't promote any to 'required'.
    req_values = {
        r[0] for r in conn.execute("SELECT DISTINCT requirement FROM resource_grants").fetchall()
    }
    assert req_values == {"available"}

    # 6) ``domain`` column gone; ``is_required`` column present.
    ki_cols = [r[1] for r in conn.execute("PRAGMA table_info('knowledge_items')").fetchall()]
    assert "domain" not in ki_cols
    assert "is_required" in ki_cols

    # 7) New v49 tables exist with expected shape.
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

    # 8) Marketplace telemetry tables untouched by the v49 migration.
    assert "usage_marketplace_item_daily" in tables

    # 9) Schema version row bumped.
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 51
