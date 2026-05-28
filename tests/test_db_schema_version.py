"""v20 adds source_query column to table_registry.

Backs query_mode='materialized' for BigQuery: admin registers a SQL body
that the scheduler runs through the DuckDB BQ extension and writes as a
parquet to /data/extracts/bigquery/data/<id>.parquet.

The v19 step (#150) drops dataset_permissions, access_requests tables and
users.role, table_registry.is_public columns; v20 then ALTERs the post-v19
table_registry to add the source_query column.
"""

import duckdb

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def test_schema_version_is_60():
    # v27 → v28: explicit-install (Model B) for curated marketplace plugins.
    # user_plugin_optouts row presence flips meaning from "excluded" to
    # "subscribed"; migration wipes existing rows so the inverted reading
    # starts from a clean baseline. Also adds marketplace_plugins.created_at
    # (per-plugin "newest first" sort on /marketplace), backfilled from
    # parent marketplace_registry.registered_at.
    # v28 → v29: /home page rollout — instance_templates singleton
    # consolidation (welcome_template + claude_md_template merged) + new
    # users.onboarded column. See tests/test_v29_home_migration.py for
    # the exhaustive coverage of that step.
    # v29 → v30: news_template — single versioned table for the /home
    # news perex + /news permalink page. See
    # tests/test_news_template_repository.py.
    # v30 → v31: session-pipeline framework — session_processor_state
    #            replaces session_extraction_state with composite PK.
    # v31 → v32 (PR #233): flea-market upload guardrails — adds
    #            store_entities.visibility_status + creates store_submissions.
    # v32 → v33 (PR #233): forensic columns on store_submissions —
    #            file_size, bundle_sha256, bundle_purged_at. Underpins the
    #            persist-blocked-bundle behavior so admins can Rescan /
    #            Override / Download; 30-day TTL purge clears bytes while
    #            keeping the row + sha intact. See docs/STORE_GUARDRAILS.md.
    # v33 → v34: drop store_submissions.retry_count — counter mixed LLM
    #            error count + admin rescan count, redundant with audit_log.
    # v34 → v35 (PR #233): store_entities gains 'archived' visibility
    #            state + archived_at + archived_by audit columns. Owner
    #            soft-delete writes 'archived'; existing user_store_installs
    #            keep serving the bundle through marketplace.zip / .git.
    #            Hard delete (DELETE ?hard=true) remains admin-only.
    # v35 → v36 (PR #233 follow-up): re-apply NOT NULL + DEFAULT 'pending'
    #            on store_entities.visibility_status. Lost in the v34→v35
    #            column rebuild. Without this, an INSERT that omits the
    #            column lands NULL → repo reads None → undefined behavior
    #            in the visibility gates. Value-list invariant remains
    #            enforced application-side (DuckDB ADD CHECK on existing
    #            column not supported).
    # v36 → v37: curated marketplace enrichment from
    #            `.claude-plugin/marketplace-metadata.json` plus mandatory curator
    #            identity on marketplace_registry. Adds curator_name +
    #            curator_email to marketplace_registry, and
    #            cover_photo_url + video_url + doc_links to
    #            marketplace_plugins.
    # v37 → v38: flea-market edit feature with version
    #            history. Adds store_entities.version_no INTEGER and
    #            version_history JSON. Each new bundle upload via
    #            PUT bumps version_no and appends to version_history;
    #            metadata-only edits don't bump. Existing rows backfill
    #            to version_no=1 with a single-entry history seeded
    #            from the row's current `version` (hash). Bundle bytes
    #            for each version live on disk under
    #            ${DATA_DIR}/store/<id>/versions/v<N>/plugin/.
    # v38 → v39: system plugin tier — admin-toggleable mandatory plugin
    #            set. Adds marketplace_plugins.is_system BOOLEAN DEFAULT
    #            FALSE. The flag drives a fanout that materializes
    #            resource_grants + user_plugin_optouts rows for every
    #            existing user_groups + users row, so the resolver's
    #            existing (rbac ∩ subscriptions) computation naturally
    #            pulls system plugins into every user's stack. UI then
    #            locks the corresponding controls so users can't
    #            unsubscribe and admins can't revoke per-group grants.
    # v39 → v40: persistent BigQuery metadata cache. Adds
    #            bq_metadata_cache(table_id PK, rows, size_bytes,
    #            partition_by, clustered_by, refreshed_at, error_at,
    #            error_msg).
    # v40 → v41: Activity Center schema — audit_log gains params_before
    #            (JSON), client_ip (VARCHAR), client_kind (VARCHAR),
    #            correlation_id (VARCHAR). Three indices on (timestamp),
    #            (user_id, timestamp), (action, timestamp).
    # v41 → v42 (this PR): platform telemetry schema — 7 new usage_*
    #            tables: usage_events (per-event log), usage_session_summary
    #            (per-session aggregate), usage_tool_daily + usage_plugin_daily
    #            (daily rollups), usage_attribution_skills/agents/commands
    #            (plugin manifest attribution). 10 indices for fast queries.
    # v42 → v43: user_observability_views — per-user saved
    #            filter combinations backing the unified /admin/activity
    #            page (UNIQUE(user_id, name)). Schema is intentionally
    #            opaque JSON because the UI evolves faster than DB.
    # v43 → v44: homepage status frame backing columns —
    #            users.last_pull_at (per-user manifest fetch timestamp,
    #            bumped by GET /api/sync/manifest) plus four BIGINT token
    #            counters on usage_session_summary (input_tokens,
    #            output_tokens, cache_read_tokens, cache_creation_tokens).
    #            USAGE_PROCESSOR_VERSION simultaneously bumps 1→2 so the
    #            reprocess loop backfills tokens on next tick.
    # v44 → v45: user_id column on usage_session_summary + usage_events
    #            (stable RBAC filter — replaces the unstable email-local-part
    #            ``username`` column) plus matching indices.
    # v45 → v46: per-user opt-out (dismiss) for curated memory
    #            items. New table ``knowledge_item_user_dismissed``
    #            ((user_id, item_id) PK, dismissed_at) + index on user_id
    #            for the EXISTS subquery used by list_items / search /
    #            count_items / bundle. Mandatory items are governance-
    #            protected: the API rejects POSTs against them, and the
    #            SQL filter exempts ``status = 'mandatory'`` so any stale
    #            row from before an item was mandated is silently ignored.
    # v46 → v47: DuckDB FTS BM25 index over knowledge_items(title, content).
    #            Replaces ``ILIKE '%q%'`` ranking-by-insertion-order in
    #            ``KnowledgeRepository.search`` with BM25 relevance scoring.
    #            Migration is soft-fail: a missing fts extension leaves the
    #            DB at v46 (search falls back to ILIKE).
    # v47 → v48 (this PR): marketplace telemetry refactor. Drops 4 legacy
    #            tables (usage_attribution_skills/_agents/_commands,
    #            usage_plugin_daily — all verified empty or derivable).
    #            Adds usage_marketplace_item_daily (per-day fact with
    #            count + distinct_users + error_count) and
    #            usage_marketplace_item_window (sliding-window snapshot,
    #            labels 'last_7d' refreshed every tick, 'last_30d' hourly).
    #            New attribution logic = prefix split on `<plugin>:<local>`
    #            identifier + live lookup against marketplace_plugins /
    #            store_entities — no mapping tables needed.
    # v48 → v49: phase-1 Flea refactor — title, tagline, synthetic_name on
    #            store_entities, backfilled via humanize_name(strip_archive_suffix).
    # v49 → v50: UNIQUE INDEX on store_entities.synthetic_name (canonical
    #            attribution key — rollup keyspace, JSONL prefix, marketplace
    #            bundle naming). Migration pre-checks for duplicates and
    #            raises RuntimeError listing them rather than letting the
    #            CREATE UNIQUE INDEX fail mid-way.
    # v50 → v51: nullable ``table_registry.bq_fqn`` (issue #343) — fully-
    #            qualified BigQuery path that decouples the UX/RBAC
    #            ``bucket`` label from the physical BQ dataset name. Rows
    #            without it fall back to the legacy
    #            bucket+source_table+remote_attach.project path.
    #            Released on main as 0.54.29 (PR #346).
    # v51 → v52: unified stack — Data Packages + Memory Domains. Adds
    #            resource_grants.requirement enum, knowledge_items.is_required
    #            (splitting the status='mandatory' overload), data_packages
    #            + data_package_tables, memory_domains +
    #            knowledge_item_domains junction, and
    #            user_stack_subscriptions for per-user opt-in. Drops the
    #            scalar knowledge_items.domain column. (Originally v49
    #            on the branch; renumbered to v52 on the second merge
    #            with main to make room for main's v51 bq_fqn release.)
    # v52 → v53: cover_image_url on data_packages + memory_domains.
    # v53 → v54: lifecycle status + classification category for /catalog
    #            cards (data_packages adds status + category, memory_domains
    #            adds status only).
    # v54 → v55: per-table docs columns on table_registry — feeds the
    #            /catalog/t/<id> detail page (sample_questions,
    #            things_to_know, pairs_well_with).
    # v55 → v56: recipes table — admin-curated multi-table query templates
    #            surfaced as a third "Recipes" tab on /catalog.
    # v56 → v57: soft-delete columns (``deleted_at TIMESTAMP``) on
    #            data_packages, memory_domains, recipes for the Undo
    #            toast flow.
    # v57 → v58: ``memory_domain_suggestions`` table backs the non-admin
    #            "Suggest a domain" affordance on /corporate-memory's
    #            empty state.
    # v58 → v59: extended-content columns on ``data_packages``
    #            (owner_name, owner_team, tags, long_description,
    #            when_to_use, when_not_to_use, example_questions) +
    #            structured per-table doc columns on ``table_registry``
    #            (grain, platforms, partition_col, history, gotchas) for
    #            the /catalog/p/<slug> rewrite per the extended-
    #            descriptions admin spec. All additive + NULLABLE.
    # v59 → v60: backfill ``usage_events.username`` and
    #            ``usage_session_summary.username`` from ``users.email``
    #            where ``user_id`` is non-null. Collapses the admin
    #            telemetry dropdown which previously listed the same
    #            user under multiple identities (email from REST writers,
    #            UUID from upload-API sessions, OS-username from the
    #            legacy collector).
    # v60 → v61: ``cli_auth_codes`` table (browser-loopback login).
    # v61 → v62: ``setup_tokens`` table for Agnes Cowork one-click setup.
    # v62 → v63: ``mcp_sources``, ``tool_registry``, ``tool_grants``
    #            for Universal MCP inbound connector (RFC #461).
    assert SCHEMA_VERSION >= 66


def test_v37_marketplace_curator_columns(tmp_path):
    """Fresh install reaches the current schema with the v37 marketplace
    columns present."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    registry_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_registry'"
        ).fetchall()
    }
    assert {"curator_name", "curator_email"} <= registry_cols, (
        f"curator columns missing from marketplace_registry: {registry_cols}"
    )

    plugin_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_plugins'"
        ).fetchall()
    }
    assert {"cover_photo_url", "video_url", "doc_links"} <= plugin_cols, (
        f"enrichment columns missing from marketplace_plugins: {plugin_cols}"
    )
    conn.close()


def test_v36_db_migrates_to_current(tmp_path):
    """Pre-existing v36 DB upgrades cleanly through v37 (curator
    enrichment) and v38 (flea edit version history) without losing
    existing rows."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Stand up a minimal v36-shape registry + plugin row, plus the
    # schema_version row that pins us to 36.
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (36)")
    conn.execute("""CREATE TABLE marketplace_registry (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
        url VARCHAR NOT NULL, branch VARCHAR, token_env VARCHAR,
        description TEXT, registered_by VARCHAR,
        registered_at TIMESTAMP DEFAULT current_timestamp,
        last_synced_at TIMESTAMP, last_commit_sha VARCHAR, last_error TEXT
    )""")
    conn.execute("""CREATE TABLE marketplace_plugins (
        marketplace_id VARCHAR NOT NULL, name VARCHAR NOT NULL,
        description TEXT, version VARCHAR, author_name VARCHAR,
        homepage VARCHAR, category VARCHAR, source_type VARCHAR,
        source_spec JSON, raw JSON,
        created_at TIMESTAMP DEFAULT current_timestamp,
        updated_at TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (marketplace_id, name)
    )""")
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url) VALUES ('legacy', 'Legacy', 'https://example.com/repo.git')"
    )
    conn.execute("INSERT INTO marketplace_plugins (marketplace_id, name) VALUES ('legacy', 'foo')")

    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    # v37 enrichment columns exist; existing rows preserved with NULL.
    row = conn.execute("SELECT curator_name, curator_email FROM marketplace_registry WHERE id = 'legacy'").fetchone()
    assert row == (None, None)

    row = conn.execute(
        "SELECT cover_photo_url, video_url, doc_links FROM marketplace_plugins "
        "WHERE marketplace_id = 'legacy' AND name = 'foo'"
    ).fetchone()
    assert row == (None, None, None)
    conn.close()


def test_v39_adds_marketplace_plugins_is_system(tmp_path):
    """Fresh install reaches the current schema with the v39 is_system
    column on marketplace_plugins. Default value is FALSE (not NULL) so
    the fanout helpers don't need to special-case absent rows."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_plugins'"
        ).fetchall()
    }
    assert "is_system" in cols, f"is_system missing from {cols}"

    # New rows default to FALSE — required so a freshly-synced plugin
    # doesn't accidentally land in everyone's stack.
    conn.execute("INSERT INTO marketplace_registry (id, name, url) VALUES ('m', 'M', 'https://example.com/repo.git')")
    conn.execute("INSERT INTO marketplace_plugins (marketplace_id, name) VALUES ('m', 'p')")
    row = conn.execute("SELECT is_system FROM marketplace_plugins WHERE marketplace_id = 'm' AND name = 'p'").fetchone()
    assert row[0] is False, f"new plugin defaulted to {row[0]!r}, expected False"
    conn.close()


def test_v38_db_migrates_to_v39(tmp_path):
    """Pre-existing v38 DB upgrades to v39 cleanly — adds is_system
    column, existing rows backfill to FALSE, schema_version updates."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Stand up the v38 minimal shape: schema_version row + the two
    # marketplace tables + a pre-existing plugin row that must survive
    # the migration with is_system = FALSE.
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (38)")
    conn.execute("""CREATE TABLE marketplace_registry (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
        url VARCHAR NOT NULL, branch VARCHAR, token_env VARCHAR,
        description TEXT, registered_by VARCHAR,
        registered_at TIMESTAMP DEFAULT current_timestamp,
        last_synced_at TIMESTAMP, last_commit_sha VARCHAR, last_error TEXT,
        curator_name VARCHAR, curator_email VARCHAR
    )""")
    conn.execute("""CREATE TABLE marketplace_plugins (
        marketplace_id VARCHAR NOT NULL, name VARCHAR NOT NULL,
        description TEXT, version VARCHAR, author_name VARCHAR,
        homepage VARCHAR, category VARCHAR, source_type VARCHAR,
        source_spec JSON, raw JSON,
        created_at TIMESTAMP DEFAULT current_timestamp,
        updated_at TIMESTAMP DEFAULT current_timestamp,
        cover_photo_url VARCHAR, video_url VARCHAR, doc_links JSON,
        PRIMARY KEY (marketplace_id, name)
    )""")
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url) VALUES ('legacy', 'Legacy', 'https://example.com/repo.git')"
    )
    conn.execute("INSERT INTO marketplace_plugins (marketplace_id, name) VALUES ('legacy', 'foo')")

    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_plugins'"
        ).fetchall()
    }
    assert "is_system" in cols

    # Existing pre-v39 row backfilled to FALSE — no plugin lands in
    # everyone's stack just because we ran the migration.
    row = conn.execute(
        "SELECT is_system FROM marketplace_plugins WHERE marketplace_id = 'legacy' AND name = 'foo'"
    ).fetchone()
    assert row[0] is False, f"pre-existing row backfilled to {row[0]!r}"
    conn.close()


def test_v20_adds_source_query(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols, f"source_query missing from {cols}"
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_claude_md_template_seeded_in_instance_templates(tmp_path):
    """v23 introduced claude_md_template as a singleton table; v28 consolidates
    it into instance_templates keyed 'claude_md'. Post-v28 the legacy table is
    dropped — the canonical lookup is `instance_templates WHERE key='claude_md'`.

    See tests/test_v28_migration.py for the migration path coverage. This test
    just verifies the seeded row is present on a fresh install.
    """
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    tables = {
        r[0]
        for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
    }
    assert "instance_templates" in tables
    assert "claude_md_template" not in tables, "claude_md_template should be consolidated away post-v28"

    row = conn.execute("SELECT key, content FROM instance_templates WHERE key = 'claude_md'").fetchone()
    assert row is not None
    assert row[0] == "claude_md"
    assert row[1] is None  # default = no override
    conn.close()


def test_v19_db_migrates_to_v20(tmp_path):
    """Pre-existing v19 DB (post-RBAC-drop) without source_query upgrades
    cleanly without losing data."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Simulate a v19 DB at minimal but realistic shape: schema_version row +
    # a table_registry row in the post-v19 column shape (no is_public column,
    # since v19 finalize dropped it via the table-rebuild idiom).
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (19)")
    conn.execute("""CREATE TABLE table_registry (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
        source_type VARCHAR, bucket VARCHAR, source_table VARCHAR,
        sync_strategy VARCHAR DEFAULT 'full_refresh',
        query_mode VARCHAR DEFAULT 'local',
        sync_schedule VARCHAR, profile_after_sync BOOLEAN DEFAULT true,
        primary_key VARCHAR, folder VARCHAR, description TEXT,
        registered_by VARCHAR,
        registered_at TIMESTAMP DEFAULT current_timestamp
    )""")
    conn.execute("INSERT INTO table_registry (id, name) VALUES ('foo', 'foo')")

    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION  # bumped 19→28 forward
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols
    # Existing row preserved, new column NULL
    row = conn.execute("SELECT id, source_query FROM table_registry WHERE id='foo'").fetchone()
    assert row == ("foo", None)
    conn.close()


def _make_v34_store_entities(conn):
    """Build a minimal v34-shape store_entities table for v34→v35 path tests.

    Only includes the columns the v34→v35 migration touches; the rest of
    the schema isn't needed because the function operates only on
    store_entities's column set.
    """
    conn.execute("""
        CREATE TABLE store_entities (
            id VARCHAR PRIMARY KEY,
            visibility_status VARCHAR DEFAULT 'pending'
        )
    """)
    conn.execute(
        "INSERT INTO store_entities (id, visibility_status) VALUES ('a', 'approved'), ('b', 'pending'), ('c', 'hidden')"
    )


def test_v34_to_v35_clean_path_rebuilds_visibility_column(tmp_path):
    """Standard v34 → v35 path: ``visibility_status`` is present, no temp
    column. Migration rebuilds the column without the legacy CHECK so
    'archived' becomes a valid value, preserves all row values, and adds
    the audit columns.
    """
    from src.db import _v34_to_v35_migrate

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _make_v34_store_entities(conn)

    _v34_to_v35_migrate(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    assert "visibility_status" in cols
    assert "_vis_v35" not in cols, "temp column must be cleaned up"
    assert "archived_at" in cols
    assert "archived_by" in cols

    rows = dict(conn.execute("SELECT id, visibility_status FROM store_entities ORDER BY id").fetchall())
    assert rows == {"a": "approved", "b": "pending", "c": "hidden"}, f"row values must survive the rebuild: {rows}"
    conn.close()


def test_v34_to_v35_recovers_from_partial_rebuild_missing_visibility(tmp_path):
    """Partial-rebuild recovery: a previous migration attempt completed
    steps 3-5 (added _vis_v35, copied values, dropped visibility_status)
    but failed before step 6 (RENAME). Subsequent restarts hit
    DROP visibility_status (no IF EXISTS guard) and looped on the same
    error, leaving the DB stranded with schema_version stuck pre-v35.

    The new code detects this state — _vis_v35 present, visibility_status
    absent — and finishes the rebuild with the RENAME alone instead of
    re-running the full destructive sequence.
    """
    from src.db import _v34_to_v35_migrate

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    # Hand-build the broken state: store_entities with _vis_v35 instead of
    # visibility_status, populated with the canonical values.
    conn.execute("""
        CREATE TABLE store_entities (
            id VARCHAR PRIMARY KEY,
            _vis_v35 VARCHAR
        )
    """)
    conn.execute(
        "INSERT INTO store_entities (id, _vis_v35) VALUES ('a', 'approved'), ('b', 'pending'), ('c', 'hidden')"
    )

    _v34_to_v35_migrate(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    assert "visibility_status" in cols
    assert "_vis_v35" not in cols
    assert "archived_at" in cols
    assert "archived_by" in cols

    rows = dict(conn.execute("SELECT id, visibility_status FROM store_entities ORDER BY id").fetchall())
    assert rows == {"a": "approved", "b": "pending", "c": "hidden"}, (
        f"row values must come back via RENAME, not be lost: {rows}"
    )
    conn.close()


def test_v34_to_v35_recovers_from_partial_rebuild_both_columns(tmp_path):
    """Edge state: a prior attempt aborted before the DROP, leaving both
    visibility_status (canonical) and _vis_v35 (temp) on the table.
    The recovery path drops _vis_v35 and keeps visibility_status — the
    rest of the schema expects that name.
    """
    from src.db import _v34_to_v35_migrate

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("""
        CREATE TABLE store_entities (
            id VARCHAR PRIMARY KEY,
            visibility_status VARCHAR,
            _vis_v35 VARCHAR
        )
    """)
    conn.execute("INSERT INTO store_entities (id, visibility_status, _vis_v35) VALUES ('a', 'approved', 'approved')")

    _v34_to_v35_migrate(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    assert "visibility_status" in cols
    assert "_vis_v35" not in cols, "temp column must be dropped"

    row = conn.execute("SELECT id, visibility_status FROM store_entities WHERE id = 'a'").fetchone()
    assert row == ("a", "approved")
    conn.close()


def test_v32_db_with_partial_v35_recovers_through_full_ladder(tmp_path):
    """End-to-end: a DB stranded at schema_version=32 with the half-applied
    v34→v35 state (visibility_status dropped, _vis_v35 left behind) must
    upgrade cleanly through the full ladder when ``_ensure_schema`` runs.

    This is the production scenario observed in operator instances after
    the original list-form ``_V34_TO_V35_MIGRATIONS`` failed mid-run on
    a fresh restart.
    """
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Stand up the broken state. We only need enough of the schema for the
    # migration ladder to run — ``_ensure_schema`` will create the rest
    # via ``_SYSTEM_SCHEMA``'s IF NOT EXISTS guards.
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (32)")
    conn.execute("""
        CREATE TABLE store_entities (
            id VARCHAR PRIMARY KEY,
            owner_user_id VARCHAR,
            owner_username VARCHAR,
            type VARCHAR,
            name VARCHAR,
            archived_at TIMESTAMP,
            archived_by VARCHAR,
            _vis_v35 VARCHAR
        )
    """)
    conn.execute("INSERT INTO store_entities (id, type, name, _vis_v35) VALUES ('a', 'skill', 'alpha', 'approved')")

    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    assert "visibility_status" in cols
    assert "_vis_v35" not in cols
    # Existing row preserved, value carried over from _vis_v35.
    row = conn.execute("SELECT id, visibility_status FROM store_entities WHERE id = 'a'").fetchone()
    assert row == ("a", "approved")
    conn.close()


def test_v35_to_v36_reapplies_visibility_constraints(tmp_path):
    """v34→v35 dropped NOT NULL + DEFAULT when rebuilding the column to
    drop the legacy CHECK; v35→v36 re-applies them. Verifies that on a
    freshly migrated DB, an INSERT omitting visibility_status either
    inherits the default 'pending' or fails — never lands NULL.
    """
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = conn.execute(
        "SELECT column_name, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_name = 'store_entities' "
        "  AND column_name = 'visibility_status'"
    ).fetchall()
    assert cols, "visibility_status column missing from store_entities"
    name, is_nullable, default_expr = cols[0]
    assert is_nullable == "NO", f"visibility_status must be NOT NULL after v36; got is_nullable={is_nullable!r}"
    # DuckDB renders the default as a quoted literal — match either form.
    assert default_expr is not None, "visibility_status DEFAULT must be set"
    assert "pending" in str(default_expr).lower(), f"visibility_status DEFAULT must be 'pending'; got {default_expr!r}"

    conn.close()
