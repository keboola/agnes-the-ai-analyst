"""DuckDB connection management and schema versioning.

Provides get_system_db() for the system state database
and get_analytics_db() for the analytics database with parquet views.
"""

import logging
import os
import re
import shutil
import time
from pathlib import Path

import duckdb

from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError

logger = logging.getLogger(__name__)

# Dev-only DuckDB query capture. When DEBUG=1 in the environment, every
# connection returned from get_system_db / get_analytics_db /
# get_analytics_db_readonly is wrapped with an InstrumentedConnection that
# records `.execute()` calls into a contextvar buffer the debug toolbar reads
# at response time. In prod (DEBUG unset), `_maybe_instrument` is a no-op pass-
# through, so the wrapper is never even constructed on the hot path.


def _maybe_instrument(con, db_tag: str):
    """Wrap a duckdb connection with InstrumentedConnection when DEBUG=1, else return as-is.

    DEBUG is read on each call so tests can toggle it via monkeypatch.setenv
    without reloading this module. Connection creation is not a hot path.
    """
    if os.environ.get("DEBUG", "").lower() not in ("1", "true", "yes"):
        return con
    from app.debug.duckdb_panel import InstrumentedConnection

    return InstrumentedConnection(con, db_tag)


_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

SCHEMA_VERSION = 39

_SYSTEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TIMESTAMP DEFAULT current_timestamp
);

-- v13: authorization is now via user_groups + user_group_members + resource_grants.
-- v19: legacy `role` column physically dropped via _v18_to_v19_finalize table
-- rebuild (was a NULL artifact since v13 — ignored at runtime, but the column
-- shape persisted in DBs upgraded through v8→v18).
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR PRIMARY KEY,
    email VARCHAR UNIQUE NOT NULL,
    name VARCHAR,
    password_hash VARCHAR,
    setup_token VARCHAR,
    setup_token_created TIMESTAMP,
    reset_token VARCHAR,
    reset_token_created TIMESTAMP,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_at TIMESTAMP,
    deactivated_by VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP,
    -- v26: onboarded flag flipped by `agnes init` success path or by the
    -- self-mark "I've already set up Agnes locally" button on /home.
    -- Default FALSE; explicit signal required to flip (no PAT-heuristic
    -- auto-flip per the brainstorm decision §D).
    onboarded BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS sync_state (
    table_id VARCHAR PRIMARY KEY,
    last_sync TIMESTAMP,
    rows BIGINT,
    file_size_bytes BIGINT,
    uncompressed_size_bytes BIGINT,
    columns INTEGER,
    hash VARCHAR,
    status VARCHAR DEFAULT 'ok',
    error TEXT
);

-- v10: view-name collision detection across connectors. The orchestrator
-- writes views into the master analytics.duckdb under a flat namespace; two
-- connectors with the same `_meta.table_name` would otherwise silently
-- overwrite each other (last-write-wins). This table records the FIRST
-- source to register a given view name; subsequent attempts from a different
-- source are refused with a `name_collision` log line until the operator
-- renames one side. Issue #81 Group C.
CREATE TABLE IF NOT EXISTS view_ownership (
    view_name     VARCHAR PRIMARY KEY,
    source_name   VARCHAR NOT NULL,
    registered_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS sync_history (
    id VARCHAR PRIMARY KEY,
    table_id VARCHAR NOT NULL,
    synced_at TIMESTAMP NOT NULL,
    rows BIGINT,
    duration_ms INTEGER,
    status VARCHAR,
    error TEXT
);

CREATE TABLE IF NOT EXISTS user_sync_settings (
    user_id VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    enabled BOOLEAN DEFAULT false,
    table_mode VARCHAR DEFAULT 'all',
    tables JSON,
    updated_at TIMESTAMP,
    PRIMARY KEY (user_id, dataset)
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    id VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    content TEXT,
    category VARCHAR,
    tags JSON,
    status VARCHAR DEFAULT 'pending',
    contributors JSON,
    source_user VARCHAR,
    audience VARCHAR,
    -- v15: context-engineering columns. Confidence is derived from verification
    -- evidence (see services/corporate_memory/confidence.py); valid_from/until
    -- carry the time-bounded validity for fact items; supersedes points to a
    -- prior id this row replaces; sensitivity gates which audiences can see
    -- the row; is_personal scopes the row to the contributor only (excluded
    -- from /bundle, listed only when the contributor is the caller).
    confidence DOUBLE,
    domain VARCHAR,
    entities JSON,
    source_type VARCHAR DEFAULT 'claude_local_md',
    source_ref VARCHAR,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    supersedes VARCHAR,
    sensitivity VARCHAR DEFAULT 'internal',
    is_personal BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP
);

-- v15: contradiction tracking — surfaced when two `mandatory`/`approved` items
-- assert conflicting facts on overlapping audiences. Detected by the
-- contradiction service; resolved by a curator (see app/api/memory.py).
CREATE TABLE IF NOT EXISTS knowledge_contradictions (
    id VARCHAR PRIMARY KEY,
    item_a_id VARCHAR NOT NULL,
    item_b_id VARCHAR NOT NULL,
    explanation TEXT,
    severity VARCHAR,
    suggested_resolution TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_by VARCHAR,
    resolved_at TIMESTAMP,
    resolution VARCHAR,
    detected_at TIMESTAMP DEFAULT current_timestamp
);

-- v17: duplicate-candidate hints — one row per (item_a, item_b, relation_type)
-- pair where the verification detector identified two same-domain knowledge
-- items sharing >= MIN_ENTITY_OVERLAP entities (see issue #62 + ADR Decision 1).
-- The repository canonicalizes (a, b) to (min, max) so each unordered pair maps
-- to one row regardless of insertion order. ``score`` carries the Jaccard ratio
-- (|A ∩ B| / |A ∪ B|) at detection time. ``resolved`` flips to TRUE when an
-- admin marks the pair via /api/memory/admin/duplicate-candidates/resolve.
CREATE TABLE IF NOT EXISTS knowledge_item_relations (
    item_a_id VARCHAR NOT NULL,
    item_b_id VARCHAR NOT NULL,
    relation_type VARCHAR NOT NULL,
    score DOUBLE,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_by VARCHAR,
    resolved_at TIMESTAMP,
    resolution VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (item_a_id, item_b_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_item_relations_resolved
    ON knowledge_item_relations(resolved);

-- v15→v29: state tracking for any session-pipeline processor (verification,
-- usage, future extractors). Composite PK (processor_name, session_file) so
-- each processor has its own independent processed-set keyed by jsonl path.
-- file_hash invalidates state when a session jsonl grows (live append from
-- an active Claude Code session) so processors reprocess the new content.
CREATE TABLE IF NOT EXISTS session_processor_state (
    processor_name VARCHAR NOT NULL,
    session_file VARCHAR NOT NULL,
    username VARCHAR NOT NULL,
    processed_at TIMESTAMP DEFAULT current_timestamp,
    items_extracted INTEGER DEFAULT 0,
    file_hash VARCHAR,
    PRIMARY KEY (processor_name, session_file)
);

-- v16: per-detection evidence rows — one knowledge_item can accumulate
-- multiple evidence rows over time (each new analyst confirmation adds one).
-- Persisting user_quote + detection_type per row is what enables future
-- Bayesian re-calibration and "additional verifiers" boost computation.
CREATE TABLE IF NOT EXISTS verification_evidence (
    id VARCHAR PRIMARY KEY,
    item_id VARCHAR NOT NULL,
    source_user VARCHAR,
    source_ref VARCHAR,
    detection_type VARCHAR,
    user_quote TEXT,
    created_at TIMESTAMP DEFAULT current_timestamp
);
CREATE INDEX IF NOT EXISTS idx_verification_evidence_item ON verification_evidence(item_id);

CREATE TABLE IF NOT EXISTS knowledge_votes (
    item_id VARCHAR NOT NULL,
    user_id VARCHAR NOT NULL,
    vote INTEGER,
    voted_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (item_id, user_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id VARCHAR PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL DEFAULT current_timestamp,
    user_id VARCHAR,
    action VARCHAR NOT NULL,
    resource VARCHAR,
    params JSON,
    result VARCHAR,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS telegram_links (
    user_id VARCHAR PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    linked_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS pending_codes (
    code VARCHAR PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS script_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    owner VARCHAR,
    schedule VARCHAR,
    source TEXT NOT NULL,
    deployed_at TIMESTAMP DEFAULT current_timestamp,
    last_run TIMESTAMP,
    last_status VARCHAR
);

-- v19: `is_public` column removed. The bypass shortcut had no API/UI/CLI
-- surface to set it (only direct DB UPDATE worked) so RBAC enforcement was
-- de-facto inactive. Table access is now exclusively via resource_grants
-- (ResourceType.TABLE).
CREATE TABLE IF NOT EXISTS table_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    source_type VARCHAR,
    bucket VARCHAR,
    source_table VARCHAR,
    source_query TEXT,
    sync_strategy VARCHAR DEFAULT 'full_refresh',
    query_mode VARCHAR DEFAULT 'local',
    sync_schedule VARCHAR,
    profile_after_sync BOOLEAN DEFAULT true,
    primary_key VARCHAR,
    folder VARCHAR,
    description TEXT,
    registered_by VARCHAR,
    registered_at TIMESTAMP DEFAULT current_timestamp,
    -- v26: Keboola sync-strategy support columns. NULL on existing rows;
    -- meaningful only when sync_strategy ∈ {'incremental', 'partitioned'}
    -- (or any strategy + where_filters). API-layer validators enforce the
    -- per-strategy required-field rules.
    incremental_window_days INTEGER,
    max_history_days INTEGER,
    incremental_column VARCHAR,
    where_filters VARCHAR,
    partition_by VARCHAR,
    partition_granularity VARCHAR,
    initial_load_chunk_days INTEGER
);

CREATE TABLE IF NOT EXISTS table_profiles (
    table_id VARCHAR PRIMARY KEY,
    profile JSON NOT NULL,
    profiled_at TIMESTAMP DEFAULT current_timestamp
);

-- v19: dataset_permissions and access_requests dropped. Replaced by
-- resource_grants (ResourceType.TABLE). Access requests flow removed —
-- users contact admin out-of-band; admin grants via /admin/access.

CREATE TABLE IF NOT EXISTS metric_definitions (
    id              VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    display_name    VARCHAR NOT NULL,
    category        VARCHAR NOT NULL,
    description     TEXT,
    type            VARCHAR DEFAULT 'sum',
    unit            VARCHAR,
    grain           VARCHAR DEFAULT 'monthly',
    table_name      VARCHAR,
    tables          VARCHAR[],
    expression      VARCHAR,
    time_column     VARCHAR,
    dimensions      VARCHAR[],
    filters         VARCHAR[],
    synonyms        VARCHAR[],
    notes           VARCHAR[],
    sql             TEXT NOT NULL,
    sql_variants    JSON,
    validation      JSON,
    source          VARCHAR DEFAULT 'manual',
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS column_metadata (
    table_id        VARCHAR NOT NULL,
    column_name     VARCHAR NOT NULL,
    basetype        VARCHAR,
    description     VARCHAR,
    confidence      VARCHAR DEFAULT 'manual',
    source          VARCHAR DEFAULT 'manual',
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (table_id, column_name)
);

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id           VARCHAR PRIMARY KEY,
    user_id      VARCHAR NOT NULL,
    name         VARCHAR NOT NULL,
    token_hash   VARCHAR NOT NULL,
    prefix       VARCHAR NOT NULL,
    scopes       VARCHAR,
    created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    expires_at   TIMESTAMP,
    last_used_at TIMESTAMP,
    last_used_ip VARCHAR,
    revoked_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS marketplace_registry (
    id              VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    url             VARCHAR NOT NULL,
    branch          VARCHAR,
    token_env       VARCHAR,
    description     TEXT,
    registered_by   VARCHAR,
    registered_at   TIMESTAMP DEFAULT current_timestamp,
    last_synced_at  TIMESTAMP,
    last_commit_sha VARCHAR,
    last_error      TEXT,
    -- v37: curator accountability — full name + email captured at registration
    -- and editable later. Surfaced on /marketplace cards and plugin detail in
    -- place of the historic `owner_todo` placeholder. Nullable so existing
    -- rows from pre-v37 instances survive migration; admin must fill via the
    -- /admin/marketplaces edit modal before the placeholder disappears.
    curator_name    VARCHAR,
    curator_email   VARCHAR
);

CREATE TABLE IF NOT EXISTS marketplace_plugins (
    marketplace_id  VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    description     TEXT,
    version         VARCHAR,
    author_name     VARCHAR,
    homepage        VARCHAR,
    category        VARCHAR,
    source_type     VARCHAR,
    source_spec     JSON,
    raw             JSON,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    -- v37: enrichment from upstream `.claude-plugin/marketplace-metadata.json`.
    -- `cover_photo_url` and `video_url` are stored as already-resolved served
    -- URLs (internal asset endpoint, mirrored cache endpoint, or pass-through
    -- external URL). `doc_links` is a JSON array of `{name, url, kind}` where
    -- `kind ∈ {internal, mirrored, external}` so the frontend can pick the
    -- right icon without re-resolving. NULL = upstream marketplace shipped no
    -- marketplace-metadata.json (or shipped one without an entry for this plugin).
    cover_photo_url VARCHAR,
    video_url       VARCHAR,
    doc_links       JSON,
    -- v39: admin-managed mandatory tier. When TRUE, the plugin is
    -- materialized into resource_grants (for every group) and
    -- user_plugin_optouts (for every user) by the mark_system endpoint
    -- + creation hooks; UI then locks the controls so users cannot
    -- unsubscribe and admins cannot revoke per-group grants for it. The
    -- resolver itself is unchanged — system semantics are emergent from
    -- the materialized rows, not a new filter layer.
    is_system       BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (marketplace_id, name)
);

CREATE TABLE IF NOT EXISTS user_groups (
    id          VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL UNIQUE,
    description TEXT,
    is_system   BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT current_timestamp,
    created_by  VARCHAR
);

-- v13: per-user group membership. Replaces the v12 users.groups JSON cache.
-- The `source` column tracks who created the row so each source only mutates
-- its own rows — Google sync's nightly DELETE+INSERT does NOT clobber
-- admin-added members, and admin UI deletions don't fight the sync loop.
--
-- v14: group_id now FK→user_groups(id). DuckDB FK enforcement blocks the
-- parent DELETE while children exist, so the application must delete
-- members + resource_grants BEFORE the user_groups row (see
-- app/api/access.py:delete_group). DuckDB does NOT support ON DELETE
-- CASCADE, so we rely on explicit transactional cleanup at the call site
-- and let the FK serve as a defense-in-depth invariant.
CREATE TABLE IF NOT EXISTS user_group_members (
    user_id   VARCHAR NOT NULL,
    group_id  VARCHAR NOT NULL REFERENCES user_groups(id),
    source    VARCHAR NOT NULL,  -- 'admin' | 'google_sync' | 'system_seed'
    added_at  TIMESTAMP DEFAULT current_timestamp,
    added_by  VARCHAR,
    PRIMARY KEY (user_id, group_id)
);

-- v13: unified resource grants. Replaces both group_mappings (v8/v9) and
-- plugin_access (v11). resource_type is a string identifier from
-- app.resource_types.ResourceType enum (e.g. 'marketplace_plugin').
-- resource_id is a path string whose format is owned by the module that
-- registered the resource type (e.g. '<marketplace_slug>/<plugin_name>').
--
-- v14: group_id FK→user_groups(id), same rationale as user_group_members.
CREATE TABLE IF NOT EXISTS resource_grants (
    id            VARCHAR PRIMARY KEY,
    group_id      VARCHAR NOT NULL REFERENCES user_groups(id),
    resource_type VARCHAR NOT NULL,
    resource_id   VARCHAR NOT NULL,
    assigned_at   TIMESTAMP DEFAULT current_timestamp,
    assigned_by   VARCHAR,
    UNIQUE (group_id, resource_type, resource_id)
);

-- v22: reserved (formerly setup_banner — feature dropped, table kept for
-- forward compatibility with already-migrated instances).
CREATE TABLE IF NOT EXISTS setup_banner (
    id INTEGER PRIMARY KEY DEFAULT 1,
    content TEXT,
    updated_at TIMESTAMP,
    updated_by VARCHAR,
    CONSTRAINT singleton CHECK (id = 1)
);

-- v28: generic per-key instance-template storage. Consolidates the v21
-- welcome_template and v23 claude_md_template singletons into one shape
-- so future operator-customizable surfaces ship as a row insert + admin-UI
-- section, not a fresh schema bump. Pre-seeded keys: 'welcome', 'claude_md',
-- 'home'. NULL content means "use the OSS-shipped default"; an admin override
-- replaces the OSS default at render time.
CREATE TABLE IF NOT EXISTS instance_templates (
    key VARCHAR PRIMARY KEY,
    content TEXT,
    previous_content TEXT,
    updated_at TIMESTAMP,
    updated_by VARCHAR
);

-- v29: news_template — single table holding every saved version of the
-- /home news perex + /news full body. `version` ↑ per save. `published`
-- distinguishes the active draft (FALSE) from public versions (TRUE).
-- Web reads `WHERE published = TRUE ORDER BY version DESC LIMIT 1`.
-- Admin can browse all rows. Invariant: at most one row with
-- `published = FALSE` at any time (the active draft). See
-- src/repositories/news_template.py.
CREATE TABLE IF NOT EXISTS news_template (
    id              VARCHAR PRIMARY KEY,
    version         INTEGER NOT NULL UNIQUE,
    intro           TEXT,
    content         TEXT,
    published       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
    updated_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
    created_by      VARCHAR,
    published_at    TIMESTAMP,
    published_by    VARCHAR
);
CREATE INDEX IF NOT EXISTS ix_news_template_pub_ver
    ON news_template (published, version DESC);

-- v25: per-user marketplace composition layer on top of admin grants.
--   * store_entities       — community-uploaded skills/agents/plugins
--   * user_store_installs  — which entities each user has chosen to install
--   * user_plugin_optouts  — opt-out overlay on top of admin-granted plugins
--
-- The served Claude Code marketplace for a user is computed as:
--     (admin_granted ∖ opt_outs) ∪ store_installs
--
-- See src/marketplace_filter.py:resolve_user_marketplace.
-- FK refs to users(id) intentionally omitted (matches the
-- personal_access_tokens / marketplace_registry pattern). DuckDB blocks
-- ALTER on a referenced parent — past finalize steps RENAME / DROP COLUMN
-- on `users`, which would fail if these store tables held FK refs at the
-- time the ladder reaches them. App-level deletes already cascade
-- explicitly (see app/api/store.py + the resource_grant-deletion hook).
CREATE TABLE IF NOT EXISTS store_entities (
    id                VARCHAR PRIMARY KEY,
    owner_user_id     VARCHAR NOT NULL,
    owner_username    VARCHAR NOT NULL,
    type              VARCHAR NOT NULL CHECK (type IN ('skill','agent','plugin')),
    name              VARCHAR NOT NULL,
    description       TEXT,
    category          VARCHAR,
    version           VARCHAR NOT NULL,
    photo_path        VARCHAR,
    video_url         VARCHAR,
    doc_paths         JSON,
    file_size         BIGINT,
    install_count     BIGINT NOT NULL DEFAULT 0,
    -- v29: flea-market guardrails. Non-approved entities are hidden from
    -- non-admin browse + per-user marketplace composition until the LLM
    -- review (or an admin override) flips them to 'approved'. Existing
    -- v28 rows backfill to 'approved' so current uploads stay visible
    -- through the upgrade.
    -- v35: 'archived' added — owner soft-delete state. Hidden from
    -- every browse listing (including the owner's own My AI Stack
    -- "card" filter), but still served to existing user_store_installs
    -- so previously-installed users keep getting the bundle through
    -- marketplace.zip / .git. Hard delete remains admin-only via
    -- DELETE ?hard=true.
    visibility_status VARCHAR NOT NULL DEFAULT 'pending'
                      CHECK (visibility_status IN ('pending','approved','hidden','archived')),
    archived_at       TIMESTAMP,
    archived_by       VARCHAR,
    -- v37: flea-market edit feature. version_no tracks the current
    -- version index (1-based); version_history is an append-only JSON
    -- array of past version metadata. Bundle bytes for each version
    -- live on disk under ${DATA_DIR}/store/<id>/versions/v<N>/plugin/
    -- so rollback can copy them forward; the live `plugin/` dir is
    -- always a copy of the current version. See
    -- StoreEntitiesRepository.append_version + restore endpoint.
    version_no        INTEGER NOT NULL DEFAULT 1,
    version_history   JSON DEFAULT '[]',
    created_at        TIMESTAMP DEFAULT current_timestamp,
    updated_at        TIMESTAMP DEFAULT current_timestamp,
    UNIQUE (owner_user_id, name)
);

CREATE TABLE IF NOT EXISTS user_store_installs (
    user_id      VARCHAR NOT NULL,
    entity_id    VARCHAR NOT NULL,
    installed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (user_id, entity_id)
);

CREATE TABLE IF NOT EXISTS user_plugin_optouts (
    user_id        VARCHAR NOT NULL,
    marketplace_id VARCHAR NOT NULL,
    plugin_name    VARCHAR NOT NULL,
    opted_out_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (user_id, marketplace_id, plugin_name)
);

-- v29: flea-market upload guardrails — every POST/PUT to /api/store/entities
-- writes a submissions row capturing the inline check verdicts, the async
-- LLM review outcome, and any admin override. Powers /admin/store/submissions.
--
-- Insert states (chosen by /api/store/entities POST):
--   pending_llm     → inline checks passed; LLM review enqueued; entity
--                     row created with visibility_status='pending'.
--   blocked_inline  → at least one inline check failed; entity row
--                     created with visibility_status='hidden' so admin
--                     can rescan / override / download. (Pre-v30 the
--                     entity row was rolled back; persisted now for
--                     forensics + the 30-day TTL bundle purge path.)
--
-- Background-task transitions (runner.py):
--   pending_llm → approved        — review concluded safe; entity flips
--                                   to visibility_status='approved'.
--   pending_llm → blocked_llm     — review flagged risk ≥ high; entity
--                                   stays at visibility_status='pending'.
--   pending_llm → review_error    — LLM call errored / timed out / missing
--                                   risk_level; admin Retry available.
--                                   Reaper sweeps stuck pending_llm rows
--                                   every 15 min into review_error.
--
-- Admin transitions:
--   blocked_* | review_error → overridden — force-publish; entity flipped
--                                   to visibility_status='approved'.
--
-- Lifecycle (terminal):
--   any → deleted — set by mark_deleted_for_entity after admin DELETE
--                   ?hard=true; entity row gone but tombstone entity_id
--                   preserved for activity-timeline correlation.
--
-- The legacy 'pending_inline' value exists in VALID_STATUSES for
-- forward-compat with future async-inline checks but is NOT written by
-- any current code path on insert.
CREATE TABLE IF NOT EXISTS store_submissions (
    id              VARCHAR PRIMARY KEY,
    entity_id       VARCHAR,
    submitter_id    VARCHAR NOT NULL,
    submitter_email VARCHAR,
    type            VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    version         VARCHAR,
    status          VARCHAR NOT NULL,
    inline_checks   JSON,
    llm_findings    JSON,
    reviewed_by_model VARCHAR,
    override_by     VARCHAR,
    override_reason TEXT,
    -- v30: forensic columns. file_size + bundle_sha256 are populated at
    -- upload time and survive the TTL purge so admins can correlate
    -- repeat-payload attempts after the bundle bytes are gone.
    -- bundle_purged_at lets the detail UI render "Bundle purged on …"
    -- instead of an empty Download cell.
    file_size        BIGINT,
    bundle_sha256    VARCHAR,
    bundle_purged_at TIMESTAMP,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS idx_store_submissions_status ON store_submissions(status);
CREATE INDEX IF NOT EXISTS idx_store_submissions_entity ON store_submissions(entity_id);
-- NOTE: no created_at index. DuckDB 1.x has a bug where
-- `ORDER BY <indexed col> DESC LIMIT N` short-returns on small tables
-- (reproduced with N=2 against 3 rows during /admin/store/submissions
-- paging). Submissions table is admin-only and bounded by upload
-- volume, so the index buys little; dropping it sidesteps the bug.
"""


import threading

_system_db_lock = threading.Lock()
_system_db_conn: duckdb.DuckDBPyConnection | None = None
_system_db_path: str | None = None


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


def _get_state_dir() -> Path:
    """Return path to writable state directory.

    Resolution order:
      1. STATE_DIR env var (explicit override).
      2. ${DATA_DIR}/state (default — current behavior).

    Use the explicit override when the deployer wants state on a
    separate disk mounted in parallel with /data rather than nested
    inside it. See docs/state-dir.md.
    """
    state = os.environ.get("STATE_DIR", "")
    if state:
        return Path(state)
    return _get_data_dir() / "state"


def _try_open_system_db(db_path: str) -> duckdb.DuckDBPyConnection:
    """Open ``system.duckdb``. If DuckDB's WAL replay raises an
    ``INTERNAL Error`` from ``ReplayAlter`` (a known failure mode when a
    container is killed mid-migration window with an unflushed
    ``ALTER TABLE … ADD COLUMN`` op in the WAL), fall back to the
    ``system.duckdb.pre-migrate`` snapshot taken at the start of the
    most recent migration. The migration ladder is idempotent, so the
    second start re-runs it and ends up at the same SCHEMA_VERSION
    cleanly. Without this fallback, an operator hits an unhealthy
    instance after every mid-migration crash and has to restore the
    snapshot by hand — even though the snapshot is right there.

    Only fires on the specific WAL-replay error class to avoid masking
    legitimate corruption (operator-edited DB, disk failure, etc.).
    """
    try:
        return duckdb.connect(db_path)
    except duckdb.Error as e:
        msg = str(e)
        is_wal_replay = (
            "Failure while replaying WAL" in msg
            or "ReplayAlter" in msg
            or "GetDefaultDatabase with no default database set" in msg
        )
        if not is_wal_replay:
            raise
        snapshot = Path(db_path).parent / "system.duckdb.pre-migrate"
        if not snapshot.exists():
            logger.error(
                "WAL replay failed and no pre-migrate snapshot at %s — "
                "manual recovery required.", snapshot,
            )
            raise
        wal_path = Path(db_path + ".wal")
        logger.warning(
            "WAL replay failed (%s) — auto-restoring from pre-migrate "
            "snapshot %s. The migration ladder will re-run on this start.",
            msg.split("\n", 1)[0][:200], snapshot,
        )
        # Move (not copy) the broken DB aside so an operator can post-
        # mortem if needed. The pre-migrate snapshot becomes the new
        # main DB; the WAL is dropped (its content is what failed to
        # replay).
        broken = Path(db_path + f".broken.{int(time.time())}")
        shutil.move(db_path, str(broken))
        if wal_path.exists():
            shutil.move(str(wal_path), str(broken) + ".wal")
        shutil.copy2(str(snapshot), db_path)
        # Re-open. If THIS also fails, propagate — auto-recovery has
        # exhausted its options.
        return duckdb.connect(db_path)


def get_system_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the system state database.

    Uses a single shared connection per DATA_DIR to avoid DuckDB lock
    conflicts between the main app and background tasks. Returns a cursor
    so callers can safely close() it without closing the underlying connection.
    """
    global _system_db_conn, _system_db_path
    db_path = str(_get_state_dir() / "system.duckdb")

    with _system_db_lock:
        if _system_db_conn is None or _system_db_path != db_path:
            # Close old connection if DATA_DIR changed (e.g., in tests)
            if _system_db_conn is not None:
                try:
                    _system_db_conn.close()
                except Exception:
                    pass
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _system_db_conn = _try_open_system_db(db_path)
            _system_db_path = db_path
            _ensure_schema(_system_db_conn)
        return _maybe_instrument(_system_db_conn.cursor(), "system")


def get_analytics_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the analytics database (parquet views)."""
    db_path = _get_data_dir() / "analytics" / "server.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return _maybe_instrument(duckdb.connect(str(db_path)), "analytics")


def _reattach_remote_extensions(
    conn: duckdb.DuckDBPyConnection, extracts_dir: Path
) -> None:
    """Re-LOAD DuckDB extensions listed in _remote_attach tables of each extract.duckdb.

    Called from get_analytics_db_readonly() after ATTACHing extract.duckdb files so
    that remote views (e.g. BigQuery) resolve correctly.  Uses LOAD only — no INSTALL —
    to avoid touching the network in read-only query paths.
    """
    if not extracts_dir.exists():
        return

    try:
        attached_dbs = {
            r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()
        }
    except Exception:
        return

    for ext_dir in sorted(extracts_dir.iterdir()):
        if not ext_dir.is_dir():
            continue
        if not _SAFE_IDENTIFIER.match(ext_dir.name):
            continue
        db_file = ext_dir / "extract.duckdb"
        if not db_file.exists():
            continue
        # Only process sources that were successfully attached
        if ext_dir.name not in attached_dbs:
            continue

        # Check whether this extract has a _remote_attach table
        try:
            has_table = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                f"WHERE table_catalog='{ext_dir.name}' AND table_name='_remote_attach'"
            ).fetchone()
            if not has_table:
                continue
        except Exception:
            continue

        try:
            rows = conn.execute(
                f"SELECT alias, extension, url, token_env FROM {ext_dir.name}._remote_attach"
            ).fetchall()
        except Exception as e:
            logger.debug("Could not read _remote_attach from %s: %s", ext_dir.name, e)
            continue

        # Refresh attached list before processing each source's rows
        try:
            attached_dbs = {
                r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()
            }
        except Exception:
            pass

        # Issue #81 Group A — apply the same allowlist policy on the
        # query path that the orchestrator's rebuild path uses. Without
        # this, a malicious connector's _remote_attach row exfiltrates
        # JWT_SECRET_KEY / SESSION_SECRET / OPENAI_API_KEY on every
        # query, defeating the rebuild-path hardening entirely.
        from src.orchestrator_security import (
            escape_sql_string_literal,
            is_extension_allowed,
            is_token_env_allowed,
        )

        for alias, extension, url, token_env in rows:
            if not _SAFE_IDENTIFIER.match(alias or ""):
                logger.debug("Skipping unsafe remote_attach alias: %r", alias)
                continue
            if not _SAFE_IDENTIFIER.match(extension or ""):
                logger.debug("Skipping unsafe remote_attach extension: %r", extension)
                continue
            if not is_extension_allowed(extension):
                logger.error(
                    "query-path remote_attach: extension %r not in allowlist; "
                    "refusing to LOAD/ATTACH for source %s. Override via "
                    "AGNES_REMOTE_ATTACH_EXTENSIONS if intended.",
                    extension, alias,
                )
                continue
            if token_env and not is_token_env_allowed(token_env):
                logger.error(
                    "query-path remote_attach: token_env %r not in allowlist; "
                    "refusing for source %s. Override via "
                    "AGNES_REMOTE_ATTACH_TOKEN_ENVS if intended.",
                    token_env, alias,
                )
                continue
            if alias in attached_dbs:
                logger.debug("Remote source %s already attached, skipping", alias)
                continue
            try:
                # LOAD only on the read-only query path — no INSTALL.
                # Per the function docstring, this path runs on every
                # query request and must not touch the network. The
                # rebuild path (orchestrator) is responsible for INSTALL;
                # by the time a query lands here, any community extension
                # we'll see is already on disk. If LOAD fails because the
                # extension isn't installed, log + skip (caller will see
                # missing remote views and the operator will trigger a
                # rebuild).
                conn.execute(f"LOAD {extension};")
                token = os.environ.get(token_env, "") if token_env else ""
                safe_url = escape_sql_string_literal(url)

                # BQ-specific: refresh token from GCE metadata, create session-scoped
                # secret before ATTACH. Empty token_env (set by the BQ extractor)
                # is the contract that signals "use built-in metadata path". The
                # secret is created here on every readonly-connection open because
                # secrets are session-scoped and don't persist with analytics.duckdb.
                if extension == "bigquery":
                    try:
                        bq_token = get_metadata_token()
                    except BQMetadataAuthError as e:
                        logger.error(
                            "Failed to fetch BQ metadata token for %s: %s — skipping ATTACH",
                            alias, e,
                        )
                        continue
                    escaped = escape_sql_string_literal(bq_token)
                    secret_name = f"bq_secret_{alias}"
                    conn.execute(
                        f"CREATE OR REPLACE SECRET {secret_name} "
                        f"(TYPE bigquery, ACCESS_TOKEN '{escaped}')"
                    )
                    from connectors.bigquery.access import apply_bq_session_settings
                    apply_bq_session_settings(conn)
                    conn.execute(
                        f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)"
                    )
                elif token:
                    escaped_token = escape_sql_string_literal(token)
                    conn.execute(
                        f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')"
                    )
                    # Apply BQ session settings on every BQ-extension attach,
                    # not only the metadata-token branch above. Previously the
                    # token-based branch fell through without setting
                    # bq_query_timeout_ms, leaving the 90 s extension default
                    # in place and causing "remote query timeout" surprises.
                    if extension == "bigquery":
                        from connectors.bigquery.access import apply_bq_session_settings
                        apply_bq_session_settings(conn)
                else:
                    conn.execute(
                        f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)"
                    )
                    if extension == "bigquery":
                        from connectors.bigquery.access import apply_bq_session_settings
                        apply_bq_session_settings(conn)
                attached_dbs.add(alias)
                logger.debug("Re-attached remote source %s via %s extension", alias, extension)
            except Exception as e:
                logger.debug("Could not re-attach remote source %s: %s", alias, e)


def get_analytics_db_readonly() -> duckdb.DuckDBPyConnection:
    """Read-only connection to analytics DB. Blocks writes and external access.

    ATTACHes extract.duckdb files so views that reference them work.
    """
    db_path = _get_data_dir() / "analytics" / "server.duckdb"
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(db_path), read_only=False)
        try:
            conn.execute("SET enable_external_access = false")
        except Exception:
            pass
        return _maybe_instrument(conn, "analytics_ro")
    conn = duckdb.connect(str(db_path), read_only=True)
    # ATTACH extract.duckdb files FIRST so views referencing them work
    extracts_dir = _get_data_dir() / "extracts"
    if extracts_dir.exists():
        for ext_dir in sorted(extracts_dir.iterdir()):
            db_file = ext_dir / "extract.duckdb"
            if db_file.exists() and ext_dir.is_dir():
                if not _SAFE_IDENTIFIER.match(ext_dir.name):
                    continue
                try:
                    conn.execute(f"ATTACH '{db_file}' AS {ext_dir.name} (READ_ONLY)")
                except Exception:
                    pass
    # Re-attach remote extensions so BigQuery / other remote views resolve.
    _reattach_remote_extensions(conn, extracts_dir)
    # Note: external_access stays enabled because views use read_parquet() on local files.
    # File-path-based attacks are blocked by the SQL blocklist in app/api/query.py.
    return _maybe_instrument(conn, "analytics_ro")


_V1_TO_V2_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS source_type VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS bucket VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS source_table VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS query_mode VARCHAR DEFAULT 'local'",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS sync_schedule VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS profile_after_sync BOOLEAN DEFAULT true",
]

_V2_TO_V3_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT true",
]

_V4_TO_V5_MIGRATIONS = [
    # DuckDB doesn't allow ALTER TABLE ADD COLUMN with NOT NULL constraint,
    # so we add the column with a DEFAULT, backfill, then the app-level
    # code enforces non-null semantics (never inserts NULL for `active`).
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE",
    "UPDATE users SET active = TRUE WHERE active IS NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_by VARCHAR",
]

_V5_TO_V6_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS personal_access_tokens (
        id           VARCHAR PRIMARY KEY,
        user_id      VARCHAR NOT NULL,
        name         VARCHAR NOT NULL,
        token_hash   VARCHAR NOT NULL,
        prefix       VARCHAR NOT NULL,
        scopes       VARCHAR,
        created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
        expires_at   TIMESTAMP,
        last_used_at TIMESTAMP,
        revoked_at   TIMESTAMP
    )
    """,
]

_V6_TO_V7_MIGRATIONS = [
    "ALTER TABLE personal_access_tokens ADD COLUMN IF NOT EXISTS last_used_ip VARCHAR",
]

_V7_TO_V8_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS internal_roles (
        id           VARCHAR PRIMARY KEY,
        key          VARCHAR UNIQUE NOT NULL,
        display_name VARCHAR NOT NULL,
        description  TEXT,
        owner_module VARCHAR,
        created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
        updated_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS group_mappings (
        id                VARCHAR PRIMARY KEY,
        external_group_id VARCHAR NOT NULL,
        internal_role_id  VARCHAR NOT NULL REFERENCES internal_roles(id),
        assigned_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
        assigned_by       VARCHAR,
        UNIQUE (external_group_id, internal_role_id)
    )
    """,
]

# v9 migration is multi-stage: ALTER + new table → seed core.* rows → backfill
# existing users.role values into user_role_grants → DROP users.role. The
# latter three steps run as Python helpers (_seed_core_roles +
# _backfill_users_role_to_grants) called from _ensure_schema, not raw SQL —
# they need DuckDB ConstraintException handling and per-user-role lookups
# that don't translate cleanly to a static SQL list.
_V8_TO_V9_MIGRATIONS = [
    "ALTER TABLE internal_roles ADD COLUMN IF NOT EXISTS implies VARCHAR DEFAULT '[]'",
    "ALTER TABLE internal_roles ADD COLUMN IF NOT EXISTS is_core BOOLEAN DEFAULT false",
    """
    CREATE TABLE IF NOT EXISTS user_role_grants (
        id                VARCHAR PRIMARY KEY,
        user_id           VARCHAR NOT NULL REFERENCES users(id),
        internal_role_id  VARCHAR NOT NULL REFERENCES internal_roles(id),
        granted_at        TIMESTAMP NOT NULL DEFAULT current_timestamp,
        granted_by        VARCHAR,
        source            VARCHAR DEFAULT 'direct',
        UNIQUE (user_id, internal_role_id)
    )
    """,
]

# v10: view-name collision detection across connectors (issue #81 Group C).
# The system schema above already CREATEs view_ownership; this migration is
# the ALTER path for installs predating the bump.
_V9_TO_V10_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS view_ownership (
        view_name     VARCHAR PRIMARY KEY,
        source_name   VARCHAR NOT NULL,
        registered_at TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
]

# v11: marketplace registry + plugin listing + group access mapping. Was
# plugin-mapping's v7→v8 + v8→v9 before PR #73 took the v9 slot for role
# management and #81 Group C took v10 for view_ownership; shifted up to v11.
_V10_TO_V11_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS marketplace_registry (
        id              VARCHAR PRIMARY KEY,
        name            VARCHAR NOT NULL,
        url             VARCHAR NOT NULL,
        branch          VARCHAR,
        token_env       VARCHAR,
        description     TEXT,
        registered_by   VARCHAR,
        registered_at   TIMESTAMP DEFAULT current_timestamp,
        last_synced_at  TIMESTAMP,
        last_commit_sha VARCHAR,
        last_error      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS marketplace_plugins (
        marketplace_id  VARCHAR NOT NULL,
        name            VARCHAR NOT NULL,
        description     TEXT,
        version         VARCHAR,
        author_name     VARCHAR,
        homepage        VARCHAR,
        category        VARCHAR,
        source_type     VARCHAR,
        source_spec     JSON,
        raw             JSON,
        created_at      TIMESTAMP DEFAULT current_timestamp,
        updated_at      TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (marketplace_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_groups (
        id          VARCHAR PRIMARY KEY,
        name        VARCHAR NOT NULL UNIQUE,
        description TEXT,
        created_at  TIMESTAMP DEFAULT current_timestamp,
        created_by  VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_access (
        group_id       VARCHAR NOT NULL,
        marketplace_id VARCHAR NOT NULL,
        plugin_name    VARCHAR NOT NULL,
        granted_at     TIMESTAMP DEFAULT current_timestamp,
        granted_by     VARCHAR,
        PRIMARY KEY (group_id, marketplace_id, plugin_name)
    )
    """,
]

# v12: users.groups + user_groups.is_system. Was plugin-mapping's v9→v10
# (then v10→v11); shifted up to v12 after #81 Group C took v10.
_V11_TO_V12_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS groups JSON",
    "ALTER TABLE user_groups ADD COLUMN IF NOT EXISTS is_system BOOLEAN DEFAULT FALSE",
]

# v13: replace internal_roles + group_mappings + user_role_grants + plugin_access
# with a single (group, resource_type, resource_id) grant model and add
# user_group_members to materialize membership (was users.groups JSON cache).
# Schema-only steps here; backfill + drops are in _v12_to_v13_finalize so we
# can run Python logic over the transitional state.
_V12_TO_V13_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS user_group_members (
        user_id   VARCHAR NOT NULL,
        group_id  VARCHAR NOT NULL,
        source    VARCHAR NOT NULL,
        added_at  TIMESTAMP DEFAULT current_timestamp,
        added_by  VARCHAR,
        PRIMARY KEY (user_id, group_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS resource_grants (
        id            VARCHAR PRIMARY KEY,
        group_id      VARCHAR NOT NULL,
        resource_type VARCHAR NOT NULL,
        resource_id   VARCHAR NOT NULL,
        assigned_at   TIMESTAMP DEFAULT current_timestamp,
        assigned_by   VARCHAR,
        UNIQUE (group_id, resource_type, resource_id)
    )
    """,
]


# v15: corporate-memory context-engineering columns + contradiction tracking +
# session-extraction state. The columns rename `knowledge_items.audience`'s
# original semantics into a richer model: confidence + domain + entities +
# source_type/ref + valid window + supersedes lineage + sensitivity tier +
# is_personal flag. Pavel's branch had this as v9→v10 against a v9-era main;
# the bump to v15 sequences after main's v14 (FK-on-grants).
_V14_TO_V15_MIGRATIONS = [
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS confidence DOUBLE",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS domain VARCHAR",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS entities JSON",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS source_type VARCHAR DEFAULT 'claude_local_md'",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS source_ref VARCHAR",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS valid_from TIMESTAMP",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS valid_until TIMESTAMP",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS supersedes VARCHAR",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS sensitivity VARCHAR DEFAULT 'internal'",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS is_personal BOOLEAN DEFAULT FALSE",
    "UPDATE knowledge_items SET source_type = 'claude_local_md' WHERE source_type IS NULL",
    """
    CREATE TABLE IF NOT EXISTS knowledge_contradictions (
        id VARCHAR PRIMARY KEY,
        item_a_id VARCHAR NOT NULL,
        item_b_id VARCHAR NOT NULL,
        explanation TEXT,
        severity VARCHAR,
        suggested_resolution TEXT,
        resolved BOOLEAN DEFAULT FALSE,
        resolved_by VARCHAR,
        resolved_at TIMESTAMP,
        resolution VARCHAR,
        detected_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_extraction_state (
        session_file VARCHAR PRIMARY KEY,
        username VARCHAR NOT NULL,
        processed_at TIMESTAMP DEFAULT current_timestamp,
        items_extracted INTEGER DEFAULT 0,
        file_hash VARCHAR
    )
    """,
]

# v16: per-detection evidence rows — many-to-one against knowledge_items.
# Future Bayesian re-calibration uses (detection_type, user_quote, source_user)
# triples; for now confidence.py walks them to compute "additional verifiers"
# boosts. Index on item_id keeps the per-item walk O(evidence-per-item).
_V15_TO_V16_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS verification_evidence (
        id VARCHAR PRIMARY KEY,
        item_id VARCHAR NOT NULL,
        source_user VARCHAR,
        source_ref VARCHAR,
        detection_type VARCHAR,
        user_quote TEXT,
        created_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_verification_evidence_item ON verification_evidence(item_id)",
]


# v16 -> v17: knowledge_item_relations table for duplicate-candidate hints
# (see issue #62). Same DDL as in _SYSTEM_SCHEMA so fresh installs and
# upgrades converge.
_V16_TO_V17_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS knowledge_item_relations (
        item_a_id VARCHAR NOT NULL,
        item_b_id VARCHAR NOT NULL,
        relation_type VARCHAR NOT NULL,
        score DOUBLE,
        resolved BOOLEAN DEFAULT FALSE,
        resolved_by VARCHAR,
        resolved_at TIMESTAMP,
        resolution VARCHAR,
        created_at TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (item_a_id, item_b_id, relation_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_knowledge_item_relations_resolved "
    "ON knowledge_item_relations(resolved)",
]


# v17 -> v18: see _v17_to_v18_finalize. Env-conditional, so kept as a Python
# helper rather than a flat SQL list (the migrate-ladder calls it directly).


# v19 -> v20: source_query column backs query_mode='materialized' for BigQuery.
# Admin-registered SQL stored verbatim; scheduler runs it through the DuckDB BQ
# extension (via BqAccess) and writes the result to
# /data/extracts/bigquery/data/<id>.parquet so the existing manifest + agnes pull
# flow distributes it to analysts. NULL on existing rows.
_V19_TO_V20_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS source_query TEXT",
]


# Core role seed data — single source of truth. Used by both _seed_core_roles
# (idempotent insert) and the v8→v9 backfill. Order matters: lowest privilege
# first so implies references resolve cleanly when expand_implies does BFS.
_CORE_ROLES_SEED = [
    # (key, display_name, description, implies)
    ("core.viewer", "Viewer",
     "Read-only access to permitted datasets.", []),
    ("core.analyst", "Analyst",
     "Default user role; query data, run analyses.", ["core.viewer"]),
    ("core.km_admin", "Knowledge-management admin",
     "Manages metric definitions and column metadata.", ["core.analyst"]),
    ("core.admin", "Administrator",
     "Full system access; bypasses dataset_permissions.", ["core.km_admin"]),
]

# Maps the legacy users.role string values onto core.* keys for the v8→v9
# backfill. Anything unrecognized falls back to core.viewer — safest default
# for existing rows that somehow held a value outside the documented enum.
_LEGACY_ROLE_TO_CORE_KEY = {
    "viewer": "core.viewer",
    "analyst": "core.analyst",
    "km_admin": "core.km_admin",
    "admin": "core.admin",
}


SYSTEM_ADMIN_GROUP = "Admin"
SYSTEM_EVERYONE_GROUP = "Everyone"

# Seed copy for the two hardcoded system groups. Names are referenced from
# app.auth.access (admin short-circuit) and the OAuth callback (default
# Everyone membership for new users); changing them is a breaking change.
_SYSTEM_GROUPS_SEED = [
    (SYSTEM_ADMIN_GROUP,
     "System: full access to all data and admin actions"),
    (SYSTEM_EVERYONE_GROUP,
     "System: default group every user is implicitly a member of"),
]


def _seed_system_groups(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotently insert/promote the Admin and Everyone system groups.

    Replaces the v9-era _seed_core_roles tail call. Runs on every connect
    once the DB is on a version this binary understands, so a manually-
    deleted system group reappears next start. Promotes a manually-created
    same-named group to is_system=TRUE without rewriting its description
    (admin's description wins; we only set our default when creating).
    """
    import uuid as _uuid

    for name, description in _SYSTEM_GROUPS_SEED:
        existing = conn.execute(
            "SELECT id, is_system FROM user_groups WHERE name = ?", [name]
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO user_groups (id, name, description, is_system, created_by)
                   VALUES (?, ?, ?, TRUE, 'system:seed')""",
                [str(_uuid.uuid4()), name, description],
            )
        elif not existing[1]:
            # Promote pre-existing manual group to system without touching desc.
            conn.execute(
                "UPDATE user_groups SET is_system = TRUE WHERE id = ?",
                [existing[0]],
            )


def _v12_to_v13_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    """Backfill user_group_members + resource_grants, then drop legacy tables.

    Runs after _V12_TO_V13_MIGRATIONS created the new tables. Order matters:

    1. Seed Admin/Everyone in user_groups so backfill targets exist.
    2. Backfill user_group_members from users.groups JSON via name lookup
       (source='google_sync' — Google was the v12 origin of those entries).
    3. Backfill admin membership from user_role_grants.core.admin grants.
    4. Add Everyone membership to every user (source='system_seed').
    5. Backfill resource_grants from plugin_access.
    6. DROP legacy tables in FK-correct order.
    7. ALTER users DROP COLUMN groups (DuckDB ≥ 0.8 supports it).

    Wrapped in an explicit transaction so an unhandled mid-flight failure
    rolls the DB back to a clean v12 state. Per-step soft-fails on DROP
    TABLE / ALTER (already caught and logged inline) do NOT abort the
    transaction — only an unexpected exception from a backfill SELECT or
    INSERT does. The outer caller in _ensure_schema then skips the
    schema_version bump and the next start retries the whole step.
    """
    import uuid as _uuid

    conn.execute("BEGIN TRANSACTION")
    try:
        _seed_system_groups(conn)

        admin_group_id = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchone()[0]
        everyone_group_id = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_EVERYONE_GROUP]
        ).fetchone()[0]

        # 2. users.groups JSON → user_group_members (google_sync). Tolerant of the
        # column having been physically dropped already (re-run safety) and of
        # malformed JSON (caught row-by-row, skipped silently).
        has_groups_col = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'groups'"
        ).fetchone()
        if has_groups_col:
            rows = conn.execute(
                "SELECT id, groups FROM users WHERE groups IS NOT NULL"
            ).fetchall()
            for user_id, groups_json in rows:
                try:
                    import json as _json
                    names = _json.loads(groups_json) if isinstance(groups_json, str) else (groups_json or [])
                except (ValueError, TypeError):
                    names = []
                if not isinstance(names, list):
                    continue
                for name in names:
                    if not isinstance(name, str) or not name.strip():
                        continue
                    group_row = conn.execute(
                        "SELECT id FROM user_groups WHERE name = ?", [name],
                    ).fetchone()
                    if not group_row:
                        continue
                    try:
                        conn.execute(
                            """INSERT INTO user_group_members
                               (user_id, group_id, source, added_by)
                               VALUES (?, ?, 'google_sync', 'system:v13-backfill')""",
                            [user_id, group_row[0]],
                        )
                    except duckdb.ConstraintException:
                        logger.debug(
                            "v13 backfill step 2 (google_sync): skipped "
                            "insert for user=%s group=%s — already present",
                            user_id, name,
                        )

        # 3. core.admin grants → Admin membership. Tolerant of either table being
        # absent (e.g. fresh install path that skipped v8→v9).
        has_internal_roles = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'internal_roles'"
        ).fetchone()
        has_user_role_grants = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'user_role_grants'"
        ).fetchone()
        if has_internal_roles and has_user_role_grants:
            admin_users = conn.execute(
                """SELECT DISTINCT g.user_id
                   FROM user_role_grants g
                   JOIN internal_roles r ON r.id = g.internal_role_id
                   WHERE r.key = 'core.admin'"""
            ).fetchall()
            for (user_id,) in admin_users:
                try:
                    conn.execute(
                        """INSERT INTO user_group_members
                           (user_id, group_id, source, added_by)
                           VALUES (?, ?, 'system_seed', 'system:v13-backfill')""",
                        [user_id, admin_group_id],
                    )
                except duckdb.ConstraintException:
                    logger.debug(
                        "v13 backfill step 3 (admin system_seed): skipped "
                        "insert for user=%s — already in Admin group "
                        "(possibly from step 2 google_sync of 'Admin' "
                        "Workspace group; system_seed intent is dropped)",
                        user_id,
                    )

        # 4. Everyone for every user (idempotent via UNIQUE PK).
        user_rows = conn.execute("SELECT id FROM users").fetchall()
        for (user_id,) in user_rows:
            try:
                conn.execute(
                    """INSERT INTO user_group_members
                       (user_id, group_id, source, added_by)
                       VALUES (?, ?, 'system_seed', 'system:v13-backfill')""",
                    [user_id, everyone_group_id],
                )
            except duckdb.ConstraintException:
                logger.debug(
                    "v13 backfill step 4 (everyone system_seed): skipped "
                    "insert for user=%s — already in Everyone group "
                    "(possibly from step 2 google_sync of 'Everyone' "
                    "Workspace group; system_seed intent is dropped)",
                    user_id,
                )

        # 5. plugin_access → resource_grants
        has_plugin_access = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'plugin_access'"
        ).fetchone()
        if has_plugin_access:
            pa_rows = conn.execute(
                """SELECT group_id, marketplace_id, plugin_name, granted_at, granted_by
                   FROM plugin_access"""
            ).fetchall()
            for group_id, marketplace_id, plugin_name, granted_at, granted_by in pa_rows:
                resource_id = f"{marketplace_id}/{plugin_name}"
                try:
                    conn.execute(
                        """INSERT INTO resource_grants
                           (id, group_id, resource_type, resource_id, assigned_at, assigned_by)
                           VALUES (?, ?, 'marketplace_plugin', ?, ?, ?)""",
                        [str(_uuid.uuid4()), group_id, resource_id, granted_at, granted_by],
                    )
                except duckdb.ConstraintException:
                    logger.debug(
                        "v13 backfill step 5 (resource_grants): skipped "
                        "insert for group=%s resource=%s — already migrated",
                        group_id, resource_id,
                    )

        # Audit: log any non-core capability grants before dropping the
        # legacy tables. No production caller in this repo ever registered
        # non-core roles via register_internal_role (verified across git
        # history) — this is a safety net for forked installs that may
        # have added custom rows. Operators see a warning naming each
        # affected role + count, so they can re-issue the equivalent
        # grants in the v13 group-based model.
        if has_internal_roles and has_user_role_grants:
            non_core_rows = conn.execute(
                """SELECT r.key, COUNT(*) AS cnt
                   FROM user_role_grants g
                   JOIN internal_roles r ON r.id = g.internal_role_id
                   WHERE r.key NOT LIKE 'core.%'
                   GROUP BY r.key"""
            ).fetchall()
            for role_key, cnt in non_core_rows:
                logger.warning(
                    "v13 migration: dropping %d grant(s) for non-core role "
                    "'%s' (no equivalent in the v13 group-based model). "
                    "If this role was registered via register_internal_role(), "
                    "the affected users need to be re-added to an "
                    "appropriate user_group post-upgrade.",
                    cnt, role_key,
                )

        # 6. Drop legacy tables in FK-correct order: dependent tables first.
        for stmt in [
            "DROP TABLE IF EXISTS plugin_access",
            "DROP TABLE IF EXISTS user_role_grants",
            "DROP TABLE IF EXISTS group_mappings",
            "DROP TABLE IF EXISTS internal_roles",
        ]:
            try:
                conn.execute(stmt)
            except Exception as e:
                logger.warning("v13 drop failed (%s): %s", stmt, e)

        # 7. Drop users.groups column. DuckDB supports DROP COLUMN; silently no-op
        # if it's already gone (fresh-install path or partial re-run).
        if has_groups_col:
            try:
                conn.execute("ALTER TABLE users DROP COLUMN groups")
            except Exception as e:
                logger.warning("v13 ALTER users DROP COLUMN groups failed: %s", e)

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _v13_to_v14_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    """Add FOREIGN KEY (group_id) → user_groups(id) on user_group_members
    and resource_grants.

    DuckDB does not support ALTER TABLE ADD CONSTRAINT for foreign keys, so
    the migration recreates each table:

        1. Pre-clean orphan rows (group_id no longer in user_groups).
           These should not exist on a clean v13 DB but the app-layer
           cascade was best-effort before this PR (see #3).
        2. RENAME old table to *_v13_pre.
        3. CREATE TABLE with the FK (matches the v14 _SYSTEM_SCHEMA).
        4. INSERT … SELECT from *_v13_pre.
        5. DROP *_v13_pre.

    Wrapped in BEGIN TRANSACTION so a mid-flight failure rolls back to
    a clean v13 state and the outer caller skips the schema_version bump.
    DuckDB does NOT support ON DELETE CASCADE — see _SYSTEM_SCHEMA above
    and app/api/access.py:delete_group for the explicit cascade.
    """
    orphan_members = conn.execute(
        """SELECT COUNT(*) FROM user_group_members
           WHERE group_id NOT IN (SELECT id FROM user_groups)"""
    ).fetchone()[0]
    orphan_grants = conn.execute(
        """SELECT COUNT(*) FROM resource_grants
           WHERE group_id NOT IN (SELECT id FROM user_groups)"""
    ).fetchone()[0]
    if orphan_members:
        logger.warning(
            "v14 migration: dropping %d orphan user_group_members rows "
            "(group_id pointed at a deleted user_groups.id)",
            orphan_members,
        )
    if orphan_grants:
        logger.warning(
            "v14 migration: dropping %d orphan resource_grants rows",
            orphan_grants,
        )

    conn.execute("BEGIN TRANSACTION")
    try:
        # Orphan cleanup must happen inside the transaction so it rolls
        # back together with the table swap on any failure.
        conn.execute(
            """DELETE FROM user_group_members
               WHERE group_id NOT IN (SELECT id FROM user_groups)"""
        )
        conn.execute(
            """DELETE FROM resource_grants
               WHERE group_id NOT IN (SELECT id FROM user_groups)"""
        )

        # user_group_members rebuild
        conn.execute(
            "ALTER TABLE user_group_members RENAME TO user_group_members_v13_pre"
        )
        conn.execute(
            """CREATE TABLE user_group_members (
                user_id   VARCHAR NOT NULL,
                group_id  VARCHAR NOT NULL REFERENCES user_groups(id),
                source    VARCHAR NOT NULL,
                added_at  TIMESTAMP DEFAULT current_timestamp,
                added_by  VARCHAR,
                PRIMARY KEY (user_id, group_id)
            )"""
        )
        conn.execute(
            """INSERT INTO user_group_members
               (user_id, group_id, source, added_at, added_by)
               SELECT user_id, group_id, source, added_at, added_by
               FROM user_group_members_v13_pre"""
        )
        conn.execute("DROP TABLE user_group_members_v13_pre")

        # resource_grants rebuild
        conn.execute(
            "ALTER TABLE resource_grants RENAME TO resource_grants_v13_pre"
        )
        conn.execute(
            """CREATE TABLE resource_grants (
                id            VARCHAR PRIMARY KEY,
                group_id      VARCHAR NOT NULL REFERENCES user_groups(id),
                resource_type VARCHAR NOT NULL,
                resource_id   VARCHAR NOT NULL,
                assigned_at   TIMESTAMP DEFAULT current_timestamp,
                assigned_by   VARCHAR,
                UNIQUE (group_id, resource_type, resource_id)
            )"""
        )
        conn.execute(
            """INSERT INTO resource_grants
               (id, group_id, resource_type, resource_id, assigned_at, assigned_by)
               SELECT id, group_id, resource_type, resource_id, assigned_at, assigned_by
               FROM resource_grants_v13_pre"""
        )
        conn.execute("DROP TABLE resource_grants_v13_pre")

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _v17_to_v18_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop stranded non-google memberships from google-managed groups.

    Two classes of cruft:

    1. Auto-created google_sync groups (``created_by='system:google-sync'``)
       only exist because Google sync materialized them on a Workspace claim.
       Anyone in such a group whose membership is NOT ``source='google_sync'``
       got there by an obsolete code path; drop them unconditionally — the
       Workspace state is the source of truth for these rows.

    2. Seeded ``Admin`` / ``Everyone`` rows are env-conditional. When
       ``AGNES_GROUP_ADMIN_EMAIL`` / ``AGNES_GROUP_EVERYONE_EMAIL`` is set
       the row mirrors a Workspace group exclusively, and v13's
       ``system:v13-backfill`` writes (one row per existing user into
       Everyone, one per ``core.admin``-grantee into Admin) are stranded
       cruft that ``_is_sso_user`` mis-classifies as SSO membership. Drop
       those ``system_seed`` rows. The bootstrap admin's Admin membership
       is preserved by the ``added_by`` allow-list — it must survive so
       the operator never loses console access.

       When the env mapping is absent, those system rows are LOCAL groups,
       and ``system:v13-backfill`` rows are legitimate (the user's
       core.admin grant was migrated into Admin-group membership, and
       every user is auto-broadcast into Everyone). Touching them would
       remove admin privileges or empty Everyone — so the env-conditional
       branches are skipped.

    Env vars are read at migration time via os.environ — operators
    flipping the mapping later don't need a fresh migration.
    """
    # Non-google memberships in auto-created google_sync groups: always cruft.
    conn.execute(
        """DELETE FROM user_group_members
           WHERE source != 'google_sync'
             AND group_id IN (
                 SELECT id FROM user_groups
                 WHERE created_by = 'system:google-sync'
             )"""
    )

    if os.environ.get("AGNES_GROUP_EVERYONE_EMAIL", "").strip():
        conn.execute(
            """DELETE FROM user_group_members
               WHERE source = 'system_seed'
                 AND group_id IN (
                     SELECT id FROM user_groups
                     WHERE name = 'Everyone' AND is_system
                 )"""
        )

    if os.environ.get("AGNES_GROUP_ADMIN_EMAIL", "").strip():
        conn.execute(
            """DELETE FROM user_group_members
               WHERE source = 'system_seed'
                 AND added_by NOT IN ('app.main:seed_admin', 'auth.bootstrap')
                 AND group_id IN (
                     SELECT id FROM user_groups
                     WHERE name = 'Admin' AND is_system
                 )"""
        )


def _v18_to_v19_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop legacy data-RBAC tables + dead columns.

    Removes:
      - ``dataset_permissions`` table (per-user grants — replaced by per-group
        ``resource_grants(resource_type='table')``)
      - ``access_requests`` table (self-service request/approve flow — removed,
        users contact admin out-of-band)
      - ``users.role`` column (NULL artifact since v13 — auth derives from
        ``user_group_members`` via ``is_user_admin``)
      - ``table_registry.is_public`` column (bypass shortcut with no
        API/UI/CLI surface — every table now requires explicit
        ``resource_grants`` row, admin override aside)

    DuckDB ALTER TABLE DROP COLUMN can be blocked by historic FK
    constraints, so the column drops use a table-rebuild idiom (rename →
    create new → INSERT … SELECT → drop old). The INSERT picks the
    intersection of the legacy and v19 column sets so test fixtures that
    hand-craft minimal pre-v19 schemas (e.g. without `sync_strategy` /
    `primary_key`) still migrate cleanly. Wrapped in BEGIN/COMMIT;
    on error ROLLBACK and the outer caller skips the schema_version bump.
    """
    def _existing_cols(table: str) -> set[str]:
        return {
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ?", [table],
            ).fetchall()
        }

    conn.execute("BEGIN TRANSACTION")
    try:
        # 1 + 2: legacy table drops. IF EXISTS guards against fresh installs
        # where _SYSTEM_SCHEMA never created them (v19+ shape).
        conn.execute("DROP TABLE IF EXISTS dataset_permissions")
        conn.execute("DROP TABLE IF EXISTS access_requests")

        # 3: rebuild users without `role` column. Skip when the column
        # never existed (fresh install on v19+ schema or test fixtures
        # that hand-crafted a minimal users table without it).
        if "role" in _existing_cols("users"):
            conn.execute("ALTER TABLE users RENAME TO users_v18_pre")
            conn.execute(
                """CREATE TABLE users (
                    id VARCHAR PRIMARY KEY,
                    email VARCHAR UNIQUE NOT NULL,
                    name VARCHAR,
                    password_hash VARCHAR,
                    setup_token VARCHAR,
                    setup_token_created TIMESTAMP,
                    reset_token VARCHAR,
                    reset_token_created TIMESTAMP,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    deactivated_at TIMESTAMP,
                    deactivated_by VARCHAR,
                    created_at TIMESTAMP DEFAULT current_timestamp,
                    updated_at TIMESTAMP
                )"""
            )
            users_target_cols = [
                "id", "email", "name", "password_hash",
                "setup_token", "setup_token_created",
                "reset_token", "reset_token_created",
                "active", "deactivated_at", "deactivated_by",
                "created_at", "updated_at",
            ]
            old_users_cols = _existing_cols("users_v18_pre")
            common = [c for c in users_target_cols if c in old_users_cols]
            col_list = ", ".join(common)
            conn.execute(
                f"INSERT INTO users ({col_list}) "
                f"SELECT {col_list} FROM users_v18_pre"
            )
            conn.execute("DROP TABLE users_v18_pre")

        # 4: rebuild table_registry without `is_public` column.
        if "is_public" in _existing_cols("table_registry"):
            conn.execute(
                "ALTER TABLE table_registry RENAME TO table_registry_v18_pre"
            )
            conn.execute(
                """CREATE TABLE table_registry (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    source_type VARCHAR,
                    bucket VARCHAR,
                    source_table VARCHAR,
                    sync_strategy VARCHAR DEFAULT 'full_refresh',
                    query_mode VARCHAR DEFAULT 'local',
                    sync_schedule VARCHAR,
                    profile_after_sync BOOLEAN DEFAULT true,
                    primary_key VARCHAR,
                    folder VARCHAR,
                    description TEXT,
                    registered_by VARCHAR,
                    registered_at TIMESTAMP DEFAULT current_timestamp
                )"""
            )
            registry_target_cols = [
                "id", "name", "source_type", "bucket", "source_table",
                "sync_strategy", "query_mode", "sync_schedule",
                "profile_after_sync", "primary_key", "folder",
                "description", "registered_by", "registered_at",
            ]
            old_registry_cols = _existing_cols("table_registry_v18_pre")
            common = [c for c in registry_target_cols if c in old_registry_cols]
            col_list = ", ".join(common)
            conn.execute(
                f"INSERT INTO table_registry ({col_list}) "
                f"SELECT {col_list} FROM table_registry_v18_pre"
            )
            conn.execute("DROP TABLE table_registry_v18_pre")

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _seed_core_roles(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotently insert/refresh the four core.* hierarchy roles.

    Called from _ensure_schema on every system-DB connect (the unconditional
    tail call below the migration guard) — fresh installs need the rows to
    exist before any user_role_grants can reference them, and existing DBs
    benefit from the safety net if a deployment somehow loses a row
    (e.g. accidental admin DELETE). Implies field is rewritten on every call
    to keep the hierarchy in sync with code; display_name + description are
    rewritten too so a doc tweak deploys without manual SQL.
    """
    import json as _json
    import uuid as _uuid

    for key, display_name, description, implies in _CORE_ROLES_SEED:
        existing = conn.execute(
            "SELECT id FROM internal_roles WHERE key = ?", [key]
        ).fetchone()
        implies_json = _json.dumps(implies)
        if existing:
            conn.execute(
                """UPDATE internal_roles
                   SET display_name = ?, description = ?, implies = ?,
                       is_core = true, owner_module = 'core',
                       updated_at = current_timestamp
                   WHERE id = ?""",
                [display_name, description, implies_json, existing[0]],
            )
        else:
            conn.execute(
                """INSERT INTO internal_roles
                   (id, key, display_name, description, owner_module, implies, is_core)
                   VALUES (?, ?, ?, ?, 'core', ?, true)""",
                [str(_uuid.uuid4()), key, display_name, description, implies_json],
            )


def _backfill_users_role_to_grants(conn: duckdb.DuckDBPyConnection) -> None:
    """One-shot: convert legacy users.role values into user_role_grants rows.

    Runs as part of the v8→v9 migration, after _seed_core_roles populated the
    target internal_roles rows and before users.role is dropped. Idempotent
    via the (user_id, internal_role_id) UNIQUE constraint — re-run is safe.
    """
    import uuid as _uuid

    # Verify users.role column still exists (we may be re-running after a
    # half-applied migration); skip silently if it's already gone.
    has_role_col = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'role'"
    ).fetchone()
    if not has_role_col:
        return

    rows = conn.execute(
        "SELECT id, role FROM users WHERE role IS NOT NULL"
    ).fetchall()
    backfilled = 0
    for user_id, role_str in rows:
        role_key = _LEGACY_ROLE_TO_CORE_KEY.get(role_str, "core.viewer")
        role_row = conn.execute(
            "SELECT id FROM internal_roles WHERE key = ?", [role_key]
        ).fetchone()
        if not role_row:
            logger.warning(
                "v9 backfill: core role %s missing — skipping user %s",
                role_key, user_id,
            )
            continue
        try:
            conn.execute(
                """INSERT INTO user_role_grants
                   (id, user_id, internal_role_id, granted_by, source)
                   VALUES (?, ?, ?, 'system:v9-backfill', 'auto-seed')""",
                [str(_uuid.uuid4()), user_id, role_row[0]],
            )
            backfilled += 1
        except duckdb.ConstraintException:
            pass  # already granted (idempotent re-run)
    if backfilled:
        logger.info(
            "v9 backfill: seeded user_role_grants for %d existing user(s)",
            backfilled,
        )


_V3_TO_V4_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS metric_definitions (
        id              VARCHAR PRIMARY KEY,
        name            VARCHAR NOT NULL,
        display_name    VARCHAR NOT NULL,
        category        VARCHAR NOT NULL,
        description     TEXT,
        type            VARCHAR DEFAULT 'sum',
        unit            VARCHAR,
        grain           VARCHAR DEFAULT 'monthly',
        table_name      VARCHAR,
        tables          VARCHAR[],
        expression      VARCHAR,
        time_column     VARCHAR,
        dimensions      VARCHAR[],
        filters         VARCHAR[],
        synonyms        VARCHAR[],
        notes           VARCHAR[],
        sql             TEXT NOT NULL,
        sql_variants    JSON,
        validation      JSON,
        source          VARCHAR DEFAULT 'manual',
        created_at      TIMESTAMP DEFAULT current_timestamp,
        updated_at      TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS column_metadata (
        table_id        VARCHAR NOT NULL,
        column_name     VARCHAR NOT NULL,
        basetype        VARCHAR,
        description     VARCHAR,
        confidence      VARCHAR DEFAULT 'manual',
        source          VARCHAR DEFAULT 'manual',
        updated_at      TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (table_id, column_name)
    )
    """,
]

_V20_TO_V21_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS welcome_template (
        id INTEGER PRIMARY KEY DEFAULT 1,
        content TEXT,
        updated_at TIMESTAMP,
        updated_by VARCHAR,
        CONSTRAINT singleton CHECK (id = 1)
    )""",
    "INSERT INTO welcome_template (id, content) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING",
]

_V21_TO_V22_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS setup_banner (
        id INTEGER PRIMARY KEY DEFAULT 1,
        content TEXT,
        updated_at TIMESTAMP,
        updated_by VARCHAR,
        CONSTRAINT singleton CHECK (id = 1)
    )""",
    "INSERT INTO setup_banner (id, content) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING",
]

_V22_TO_V23_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS claude_md_template (
        id INTEGER PRIMARY KEY DEFAULT 1,
        content TEXT,
        updated_at TIMESTAMP,
        updated_by VARCHAR,
        CONSTRAINT singleton CHECK (id = 1)
    )""",
    "INSERT INTO claude_md_template (id, content) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING",
]

# v25: store + opt-out tables backing the flea-market and my-stack views
# (now served at /marketplace?tab=flea + /marketplace?tab=my; the v25-era
# standalone /store and /my-ai-stack page routes were dropped post-v25).
_V24_TO_V25_MIGRATIONS = [
    # FK refs deliberately omitted — see the matching note in _SYSTEM_SCHEMA.
    """
    CREATE TABLE IF NOT EXISTS store_entities (
        id              VARCHAR PRIMARY KEY,
        owner_user_id   VARCHAR NOT NULL,
        owner_username  VARCHAR NOT NULL,
        type            VARCHAR NOT NULL CHECK (type IN ('skill','agent','plugin')),
        name            VARCHAR NOT NULL,
        description     TEXT,
        category        VARCHAR,
        version         VARCHAR NOT NULL,
        photo_path      VARCHAR,
        video_url       VARCHAR,
        doc_paths       JSON,
        file_size       BIGINT,
        install_count   BIGINT NOT NULL DEFAULT 0,
        created_at      TIMESTAMP DEFAULT current_timestamp,
        updated_at      TIMESTAMP DEFAULT current_timestamp,
        UNIQUE (owner_user_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_store_installs (
        user_id      VARCHAR NOT NULL,
        entity_id    VARCHAR NOT NULL,
        installed_at TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (user_id, entity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_plugin_optouts (
        user_id        VARCHAR NOT NULL,
        marketplace_id VARCHAR NOT NULL,
        plugin_name    VARCHAR NOT NULL,
        opted_out_at   TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (user_id, marketplace_id, plugin_name)
    )
    """,
]


# v26: unify Keboola query_mode='local' rows into 'materialized'.
#
# The old `local` flow ran the DuckDB Keboola extension's COPY through
# QueryService — which is unreliable on linked-bucket projects (and was
# wholly broken pre-v0.1.6 of the extension). The new `materialized`
# flow uses the Storage API export-async path directly:
#   POST /v2/storage/tables/<id>/export-async
#   GET  /v2/storage/jobs/<id>  (poll)
#   GET  /v2/storage/files/<id>?federationToken=1  (signed URL)
#   download → CSV → parquet
# That works regardless of project flags, and a NULL `source_query`
# means "full table export" — same effective behavior the `local` mode
# previously gave.
#
# Existing Keboola rows registered as `query_mode='local'` are flipped
# to 'materialized'; their source_query stays NULL (full table). Jira
# and BigQuery 'local' rows are untouched (this connector still uses
# its own path).
_V25_TO_V26_MIGRATIONS = [
    """
    UPDATE table_registry
    SET query_mode = 'materialized'
    WHERE source_type = 'keboola' AND query_mode = 'local'
    """,
]


# v27: Keboola sync-strategy support columns on table_registry.
#
# Layered on top of v26's local→materialized unification. Admins can opt
# specific Keboola tables back to `query_mode='local'` (via the Direct
# extract Edit-modal radio) to enable the new sync_strategy dispatcher.
# The existing `sync_strategy` column (default 'full_refresh') drives one
# of {'full_refresh', 'incremental', 'partitioned'} from v27 onward. The
# seven columns added here are the per-strategy knobs:
#   - incremental_window_days: backtrack window applied to last_sync (default 7)
#   - max_history_days: cap on first-sync history depth
#   - incremental_column: reserved for future use when changedSince's
#     lastChangeDate isn't the right mutation column for a table
#   - where_filters: JSON array of {column, operator, values} filter entries
#     resolved at sync time (date placeholders like {{last_3_months}})
#   - partition_by: column whose value drives the partition key
#   - partition_granularity: 'day' | 'month' | 'year'
#   - initial_load_chunk_days: chunked initial-load step size (default 30)
#
# All NULL on existing rows → no behavior change for tables that don't
# opt in. v26's local→materialized flip preserves the migration-correct
# behavior for the default case; new-or-edited rows that pick Direct
# extract land at `query_mode='local'` again with sync_strategy in play.
# API-layer validators enforce per-strategy required-field combinations
# (e.g. partitioned ⇒ partition_by required) and reject conflicting combos
# (e.g. incremental + where_filters → 422).
_V26_TO_V27_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS incremental_window_days INTEGER",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS max_history_days INTEGER",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS incremental_column VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS where_filters VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS partition_by VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS partition_granularity VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS initial_load_chunk_days INTEGER",
]


# v28 (upstream): explicit-install (Model B) for curated marketplace plugins.
#
# Pre-v28 the served set was (rbac ∖ user_plugin_optouts) — a curated plugin
# the admin granted appeared in the user's marketplace until the user opted
# out via the my-stack view. From v28 the served set is (rbac ∩ subscriptions)
# — users explicitly install each curated plugin from /marketplace.
#
# We keep the table+column names (`user_plugin_optouts.opted_out_at`) to
# avoid DDL churn on running operator instances. Row PRESENCE flips meaning
# from "excluded" to "subscribed", so we wipe rows so the inverted reading
# starts from a clean baseline. Users will re-install via /marketplace.
#
# Also adds marketplace_plugins.created_at (per-plugin "newest first" sort
# on /marketplace). Backfilled from parent marketplace_registry.registered_at
# so existing plugins get a sensible date until the next sync overwrites
# with CURRENT_TIMESTAMP.
_V27_TO_V28_MIGRATIONS = [
    "DELETE FROM user_plugin_optouts",
    # IF NOT EXISTS guard: `_SYSTEM_SCHEMA` runs before the migration ladder
    # and creates `marketplace_plugins` with the full current-version
    # column set (including `created_at`) on fresh installs that come up
    # at any pre-v28 version via test fixtures. The ALTER would then trip
    # on an existing column. Same idiom as upstream `_V26_TO_V27_MIGRATIONS`.
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS created_at TIMESTAMP",
    """
    UPDATE marketplace_plugins
       SET created_at = (
           SELECT registered_at FROM marketplace_registry
            WHERE marketplace_registry.id = marketplace_plugins.marketplace_id
       )
     WHERE created_at IS NULL
    """,
]


# v29: /home page rollout. Two changes bundled because they ship together.
# (Originally drafted as v26 / v28 across rebases; landed at v29 after
# upstream's marketplace v28.)
#
#   1. instance_templates(key, content, ...) consolidates the v21
#      welcome_template + v23 claude_md_template singletons into one shape so
#      future operator-customizable surfaces ship as a row insert + admin-UI
#      section, not a fresh schema bump.
#
#      Migration semantics: CREATE the new table, INSERT existing rows from the
#      legacy tables (preserving content + updated_at + updated_by), DROP the
#      legacy tables. The CREATE+seed pure-SQL portion lives in this list;
#      the conditional INSERT-from-legacy + DROP lives in
#      _v28_to_v29_finalize() below because it needs information_schema
#      lookups to handle both fresh-install (no legacy tables) and existing
#      paths cleanly.
#
#   2. users.onboarded BOOLEAN NOT NULL DEFAULT FALSE — feeds the /home
#      state-aware landing. Default FALSE for everyone on migration; explicit
#      signal (`POST /api/me/onboarded` from `agnes init` success or the
#      self-mark button on the not-onboarded view) flips it to TRUE.
_V28_TO_V29_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS instance_templates (
        key VARCHAR PRIMARY KEY,
        content TEXT,
        previous_content TEXT,
        updated_at TIMESTAMP,
        updated_by VARCHAR
    )
    """,
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarded BOOLEAN DEFAULT FALSE",
    # Backfill any pre-existing NULL onboarded values to FALSE so the column
    # carries the documented "default FALSE" semantics for legacy users
    # (DuckDB ADD COLUMN with DEFAULT applies to new INSERTs but leaves
    # existing rows NULL — UPDATE here closes that gap).
    "UPDATE users SET onboarded = FALSE WHERE onboarded IS NULL",
]


def _v28_to_v29_finalize(conn) -> None:
    """Migrate legacy welcome_template + claude_md_template rows into
    instance_templates, then drop the legacy tables.

    Runs after _V28_TO_V29_MIGRATIONS creates the new table. Idempotent:
    re-running on an already-v29 DB is a no-op because the legacy tables
    are gone after the first run and the seed INSERTs use ON CONFLICT.
    """
    has_welcome = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = 'welcome_template'"
    ).fetchone()
    if has_welcome:
        conn.execute(
            "INSERT INTO instance_templates (key, content, updated_at, updated_by) "
            "SELECT 'welcome', content, updated_at, updated_by FROM welcome_template "
            "ON CONFLICT (key) DO NOTHING"
        )
        conn.execute("DROP TABLE welcome_template")

    has_claude_md = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = 'claude_md_template'"
    ).fetchone()
    if has_claude_md:
        conn.execute(
            "INSERT INTO instance_templates (key, content, updated_at, updated_by) "
            "SELECT 'claude_md', content, updated_at, updated_by FROM claude_md_template "
            "ON CONFLICT (key) DO NOTHING"
        )
        conn.execute("DROP TABLE claude_md_template")

    # Seed the canonical key set with NULL content. The INSERTs are no-ops if
    # the keys already landed via the legacy migration above (existing
    # operators) or via a prior migration run (idempotent re-execution).
    for key in ("welcome", "claude_md", "home"):
        conn.execute(
            "INSERT INTO instance_templates (key, content) VALUES (?, NULL) "
            "ON CONFLICT (key) DO NOTHING",
            [key],
        )


_V29_TO_V30_MIGRATIONS = [
    # news_template: single table holding every saved version of the /home
    # news perex + /news full body. `version` monotonically increases per
    # save. `published` distinguishes the active draft (FALSE) from public
    # versions (TRUE). Web reads `WHERE published = TRUE ORDER BY version
    # DESC LIMIT 1`. Admin can browse all rows.
    """
    CREATE TABLE IF NOT EXISTS news_template (
        id              VARCHAR PRIMARY KEY,
        version         INTEGER NOT NULL UNIQUE,
        intro           TEXT,
        content         TEXT,
        published       BOOLEAN NOT NULL DEFAULT FALSE,
        created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
        updated_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
        created_by      VARCHAR,
        published_at    TIMESTAMP,
        published_by    VARCHAR
    )
    """,
    # Composite index supports both `WHERE published = TRUE ORDER BY version
    # DESC LIMIT 1` (the hot read path on every /home + /news request) and
    # full-table version listing in the admin UI.
    """
    CREATE INDEX IF NOT EXISTS ix_news_template_pub_ver
        ON news_template (published, version DESC)
    """,
]


# v32: flea-market upload guardrails — create store_submissions table +
# add visibility_status to store_entities. (Originally drafted as v29
# but renumbered to v32 after rebase onto upstream's v29/v30/v31.)
#
#   * `store_entities.visibility_status` (default 'pending'). Existing
#     rows backfill to 'approved' so live uploads survive the upgrade —
#     the guardrail pipeline only gates NEW submissions.
#   * `store_submissions` table holds the per-upload audit trail
#     powering /admin/store/submissions. The CREATE lives in
#     _SYSTEM_SCHEMA; this migration only adds the column + backfill.
#
# IF NOT EXISTS guard on the ALTER mirrors v27/v28 — fresh installs at
# pre-v32 (test fixtures) come up with the column already present via
# _SYSTEM_SCHEMA.
_V31_TO_V32_MIGRATIONS = [
    "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS visibility_status VARCHAR",
    "UPDATE store_entities SET visibility_status = 'approved' WHERE visibility_status IS NULL",
]


# v33: forensic columns on store_submissions — file_size, bundle_sha256,
# bundle_purged_at. Underpins persist-blocked-bundle behavior: blocked
# uploads keep the bundle on disk so admins can Rescan / Override /
# Download. The 30-day TTL purge then clears bytes while leaving the
# row + sha intact for forensic correlation. file_size on existing rows
# is backfilled from the linked entity row (when present);
# bundle_sha256 stays NULL on legacy rows since we no longer have the
# bytes to hash. Renumbered from v30 → v33 after rebase onto upstream's
# v29/v30/v31 sequence.
_V32_TO_V33_MIGRATIONS = [
    "ALTER TABLE store_submissions ADD COLUMN IF NOT EXISTS file_size BIGINT",
    "ALTER TABLE store_submissions ADD COLUMN IF NOT EXISTS bundle_sha256 VARCHAR",
    "ALTER TABLE store_submissions ADD COLUMN IF NOT EXISTS bundle_purged_at TIMESTAMP",
    """
    UPDATE store_submissions
       SET file_size = (
           SELECT file_size FROM store_entities
            WHERE store_entities.id = store_submissions.entity_id
       )
     WHERE file_size IS NULL AND entity_id IS NOT NULL
    """,
]


# v34: drop store_submissions.retry_count. Counter mixed two unrelated
# things (LLM error count + admin rescan count), was asymmetric (Retry
# LLM didn't bump but Rescan did), and is fully redundant with the
# audit_log timeline now rendered on the detail page — every rescan /
# retry / review_error is a row there with timestamp + actor. SELECT
# COUNT(*) FROM audit_log WHERE resource = 'store_submission:<id>' AND
# action IN (…) gives the same number when an admin actually wants it.
# v35: store_entities gains 'archived' as a fourth visibility state +
# audit columns (archived_at, archived_by). Owner soft-delete writes
# this state instead of dropping the row; existing user_store_installs
# keep serving the bundle through marketplace.zip / .git so already-
# installed users don't lose the plugin. Hard delete (admin only via
# DELETE ?hard=true) remains the path for legal / privacy removals.
#
# DuckDB doesn't support ALTER COLUMN ADD CHECK in-place; the existing
# CHECK constraint allows {pending, approved, hidden}. Workaround:
# rebuild via column-rebuild — but DuckDB DROP COLUMN can fail on
# indexed tables (we hit this in v34). Easier: drop the CHECK constraint
# implicitly by not relying on it (the application validates via
# StoreEntitiesRepository.set_visibility), and just add the new
# columns. The CHECK still rejects 'archived' on inserts via DuckDB
# but DuckDB's CHECK constraint is informational on existing tables
# under ALTER — verify in migration testing.
#
# Concretely: drop and re-add the visibility_status column rebuilt
# without the CHECK, OR use ALTER TABLE … DROP CONSTRAINT. DuckDB
# supports neither cleanly on the indexed `store_entities`. Workaround:
# rename to a temp column, copy values, drop original, rename back.
# Two-step: first add the new audit columns (always safe); then
# rebuild visibility_status without the CHECK so 'archived' becomes a
# valid value.
def _v34_to_v35_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """Add the ``archived`` visibility state + audit columns to ``store_entities``.

    Replaces the old list-form ``_V34_TO_V35_MIGRATIONS`` so the migration is
    safe to re-run after a partial failure. The original sequence was

        ADD _vis_v35 → UPDATE _vis_v35 = visibility_status →
        DROP visibility_status → RENAME _vis_v35 TO visibility_status

    which left a half-rebuilt DB stranded if step 4 (RENAME) failed after
    step 3 (DROP) succeeded: ``visibility_status`` was gone, ``_vis_v35``
    held the values, and ``schema_version`` never got bumped because the
    UPDATE at the bottom of the migration ladder never ran. Restarting
    the binary then hit step 3 again with no IF EXISTS guard and looped
    on the same DROP error.

    The new implementation inspects ``store_entities``'s columns up front
    and picks the right recovery path:

    * **clean v34 shape** (``visibility_status`` present, ``_vis_v35``
      absent) — full rebuild via copy → drop → rename, as before
    * **partial v35** (``_vis_v35`` present, ``visibility_status`` absent)
      — rebuild aborted mid-way; finish the RENAME only
    * **both columns present** (rare; aborted rebuild that didn't reach
      the DROP) — drop the temp ``_vis_v35`` and keep ``visibility_status``

    The audit columns (``archived_at``, ``archived_by``) ship first
    behind ``IF NOT EXISTS`` so they're safe in all three states.
    """
    conn.execute(
        "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP"
    )
    conn.execute(
        "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS archived_by VARCHAR"
    )

    cols = {
        r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'store_entities' "
            "  AND column_name IN ('visibility_status', '_vis_v35')"
        ).fetchall()
    }
    has_vis = "visibility_status" in cols
    has_temp = "_vis_v35" in cols

    if has_vis and not has_temp:
        # Clean v34 shape — full rebuild. NOT NULL + DEFAULT are re-applied
        # in v36 (DuckDB ALTER COLUMN supports SET NOT NULL / SET DEFAULT
        # but not ADD CHECK on an existing column). Value-list enforcement
        # is application-side via VALID_VISIBILITY in StoreEntitiesRepository.
        conn.execute(
            "ALTER TABLE store_entities ADD COLUMN _vis_v35 VARCHAR"
        )
        conn.execute(
            "UPDATE store_entities SET _vis_v35 = visibility_status"
        )
        conn.execute(
            "ALTER TABLE store_entities DROP COLUMN visibility_status"
        )
        conn.execute(
            "ALTER TABLE store_entities RENAME COLUMN _vis_v35 TO visibility_status"
        )
    elif has_temp and not has_vis:
        # Partial-rebuild recovery — prior attempt dropped visibility_status
        # but the RENAME never landed. Data is already in _vis_v35 from
        # the prior UPDATE; finish the rename.
        logger.warning(
            "v34→v35 detected partial-rebuild state (visibility_status "
            "missing, _vis_v35 present); recovering via RENAME"
        )
        conn.execute(
            "ALTER TABLE store_entities RENAME COLUMN _vis_v35 TO visibility_status"
        )
    elif has_vis and has_temp:
        # Both present — earlier rebuild aborted before the DROP.
        # visibility_status holds the canonical values; drop the temp.
        logger.warning(
            "v34→v35 detected partial-rebuild state (both visibility_status "
            "and _vis_v35 present); dropping the temp"
        )
        conn.execute(
            "ALTER TABLE store_entities DROP COLUMN _vis_v35"
        )
    # else: neither column is present, which means store_entities itself
    # is at a shape ahead of v34. _SYSTEM_SCHEMA above already created
    # the post-v35 shape; nothing to do here.


# v35→v36: re-apply NOT NULL + DEFAULT 'pending' on
# store_entities.visibility_status. Lost in v34→v35 because the column
# rebuild via ADD/UPDATE/DROP/RENAME stripped both invariants. Without
# them an INSERT that omits visibility_status lands NULL → repo
# subsequently reads None → undefined behavior in the visibility gates.
# Idempotent: SET NOT NULL is a no-op when already NOT NULL; SET DEFAULT
# replaces whatever default was set. The defensive UPDATE handles the
# theoretical case where a row got NULL between v35 and v36.
#
# Also: defensively re-applies the v28→v29 users.onboarded ADD COLUMN
# for DBs where that step was silently skipped. We've observed DBs
# whose schema_version row says 36 but whose users table is missing
# `onboarded` — the only consequence-free recovery is an idempotent
# ADD IF NOT EXISTS at the v36 step.
_V35_TO_V36_MIGRATIONS = [
    "UPDATE store_entities SET visibility_status = 'pending' WHERE visibility_status IS NULL",
    "ALTER TABLE store_entities ALTER COLUMN visibility_status SET NOT NULL",
    "ALTER TABLE store_entities ALTER COLUMN visibility_status SET DEFAULT 'pending'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarded BOOLEAN DEFAULT FALSE",
    "UPDATE users SET onboarded = FALSE WHERE onboarded IS NULL",
]


# v37→v38: flea-market entity edit feature with version history.
#
# (Originally drafted as v37; renumbered after rebase onto main where
# v37 is taken by the curated marketplace enrichment migration.)
#
# Adds two columns to store_entities so an owner editing their plugin
# accumulates an append-only history rather than overwriting the prior
# bundle:
#
#   * version_no INTEGER  — current version index (1-based). Bumps on
#     every approved bundle update; metadata-only edits don't bump.
#   * version_history JSON — array of past version metadata entries:
#       [{"n", "hash", "sha256", "size", "submission_id",
#         "created_at", "created_by"}, …]
#     Each row's bundle bytes live on disk under
#     ``${DATA_DIR}/store/<eid>/versions/v<N>/plugin/`` so rollback can
#     copy them forward.
#
# Backfill: existing rows get version_no=1 and a single-entry
# version_history populated from the row's current ``version`` (hash)
# + ``file_size`` so post-migration entities surface as v1 in the UI.
# created_at backfilled from the entity row; submission_id is best-
# effort (we look up the most recent submission_id for the entity_id
# if any exists, else NULL).
_V37_TO_V38_MIGRATIONS = [
    # Defensive: minimal partial-state DBs from earlier migrations may
    # be missing columns the backfill UPDATE below references. Add
    # them idempotently first. Real post-v29 DBs already have these;
    # this is a no-op there. Keeps the recovery path through
    # `tests/test_db_schema_version.py::test_v32_db_with_partial_v35_recovers_through_full_ladder`
    # intact when walking from v32 fixture forward.
    "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS version VARCHAR",
    "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS file_size BIGINT",
    "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS created_at TIMESTAMP",
    # DuckDB ALTER doesn't accept "NOT NULL DEFAULT" together — split:
    # ADD nullable + DEFAULT, backfill nulls, then SET NOT NULL.
    "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS version_no INTEGER DEFAULT 1",
    "UPDATE store_entities SET version_no = 1 WHERE version_no IS NULL",
    "ALTER TABLE store_entities ALTER COLUMN version_no SET NOT NULL",
    "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS version_history JSON DEFAULT '[]'",
    # Backfill: synthesize a v1 entry from existing columns when the
    # history is empty. Idempotent — re-running on a populated row
    # is a no-op because the WHERE filters on empty/NULL history.
    """
    UPDATE store_entities SET version_history = json_array(
        json_object(
            'n',  1,
            'hash', version,
            'sha256', NULL,
            'size', file_size,
            'submission_id', (
                SELECT id FROM store_submissions
                 WHERE entity_id = store_entities.id
                 ORDER BY created_at DESC
                 LIMIT 1
            ),
            'created_at', CAST(created_at AS VARCHAR),
            'created_by', owner_user_id
        )
    )
    WHERE version_history IS NULL
       OR version_history = '[]'
       OR json_array_length(version_history) = 0
    """,
]


# v39: marketplace_plugins.is_system flag backing the "system plugin"
# admin tier. Plugins flipped TRUE are materialized into resource_grants
# (per group) and user_plugin_optouts (per user) by the mark_system
# endpoint; UI then locks the corresponding controls. NULL backfill
# kept defensive — DEFAULT FALSE on the column already covers fresh rows
# but the explicit UPDATE catches any pre-existing nullable column from
# partial-state DBs.
_V38_TO_V39_MIGRATIONS = [
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS is_system BOOLEAN DEFAULT FALSE",
    "UPDATE marketplace_plugins SET is_system = FALSE WHERE is_system IS NULL",
]


_V33_TO_V34_MIGRATIONS = [
    # DuckDB blocks DROP COLUMN while indexes reference the table
    # ("Dependency Error: Cannot alter entry … because there are entries
    # that depend on it"), even when the index doesn't reference the
    # dropped column. Drop both indexes, drop the column, then re-create
    # the indexes from _SYSTEM_SCHEMA's CREATE INDEX IF NOT EXISTS
    # statements (which already ran above this block — but DROP+CREATE
    # is idempotent here too).
    "DROP INDEX IF EXISTS idx_store_submissions_status",
    "DROP INDEX IF EXISTS idx_store_submissions_entity",
    "ALTER TABLE store_submissions DROP COLUMN IF EXISTS retry_count",
    "CREATE INDEX IF NOT EXISTS idx_store_submissions_status ON store_submissions(status)",
    "CREATE INDEX IF NOT EXISTS idx_store_submissions_entity ON store_submissions(entity_id)",
]


# v31: rename session_extraction_state → session_processor_state with composite
# PK (processor_name, session_file). The session pipeline framework
# (services/session_pipeline/) lets multiple processors track their own
# processed-set independently; each gets its own row keyed by name. Existing
# rows belong to the verification detector, so they're copied across with
# processor_name='verification'. The old single-PK table is dropped — its only
# caller (services/verification_detector/detector.py) is rewritten in the same
# PR to use the new repository.
#
# (Originally drafted as v29 but renumbered to v31 after rebase onto upstream's
# v29 instance_templates + v30 news_template work.)
#
# Implemented as a function rather than a SQL list because the INSERT-from-old
# step depends on whether `session_extraction_state` actually exists. Fresh
# installs at a pre-v31 schema_version (test fixtures hand-rolling a v19/v20
# DB) come through `_SYSTEM_SCHEMA` which already creates
# `session_processor_state` at the new shape — but does NOT create the old
# `session_extraction_state` (we removed that). So the migration must skip
# the copy + drop when the old table is missing rather than 500 on
# CatalogException.
_V30_TO_V31_CREATE_NEW_TABLE = """
    CREATE TABLE IF NOT EXISTS session_processor_state (
        processor_name VARCHAR NOT NULL,
        session_file VARCHAR NOT NULL,
        username VARCHAR NOT NULL,
        processed_at TIMESTAMP DEFAULT current_timestamp,
        items_extracted INTEGER DEFAULT 0,
        file_hash VARCHAR,
        PRIMARY KEY (processor_name, session_file)
    )
"""


def _v30_to_v31_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """Run the v31 migration steps with conditional copy from the legacy table."""
    conn.execute(_V30_TO_V31_CREATE_NEW_TABLE)

    # Skip the copy + drop when the legacy table doesn't exist (fresh
    # install or upgrade path that started at >= v31). Otherwise migrate
    # rows over with processor_name='verification' (the only writer of the
    # legacy table).
    has_legacy = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = 'session_extraction_state'"
    ).fetchone()
    if not has_legacy:
        return

    # INSERT OR IGNORE on the (processor_name, session_file) PK so a
    # re-run idempotently no-ops if a verification row was already
    # written at the new shape.
    conn.execute(
        """
        INSERT OR IGNORE INTO session_processor_state
            (processor_name, session_file, username, processed_at, items_extracted, file_hash)
        SELECT 'verification', session_file, username, processed_at, items_extracted, file_hash
          FROM session_extraction_state
        """
    )
    conn.execute("DROP TABLE session_extraction_state")


# v37: curated marketplace enrichment from `.claude-plugin/marketplace-metadata.json`
# plus mandatory curator identity on `marketplace_registry`. See the file-level
# `_SYSTEM_SCHEMA` block for the column-level commentary; the migration is
# pure ADD COLUMN IF NOT EXISTS so it is idempotent against a fresh install
# whose schema_version row was hand-rolled below 37 by test fixtures (the
# IF NOT EXISTS guard then no-ops because `_SYSTEM_SCHEMA` already created
# the columns at the new shape). Same idiom as `_V27_TO_V28_MIGRATIONS`'s
# `marketplace_plugins.created_at` ALTER.
#
# Originally drafted as v32 but renumbered after rebase onto upstream's
# v32→v36 sequence (flea-market upload guardrails + soft delete).
_V36_TO_V37_MIGRATIONS = [
    "ALTER TABLE marketplace_registry ADD COLUMN IF NOT EXISTS curator_name VARCHAR",
    "ALTER TABLE marketplace_registry ADD COLUMN IF NOT EXISTS curator_email VARCHAR",
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS cover_photo_url VARCHAR",
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS video_url VARCHAR",
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS doc_links JSON",
]


# v24: rewrite materialized BQ source_query from DuckDB-flavor
# (bq."<dataset>"."<table>") to BigQuery-native (`<project>.<dataset>.<table>`)
# so the new connectors.bigquery.extractor.materialize_query wrapping
# path (which routes through bigquery_query() / BQ jobs API) accepts
# them. Pre-v24, materialize used Storage Read API for the bq.<ds>.<tbl>
# form, which fails for views — see PR for full motivation.
#
# This migration is implemented in Python (not pure SQL) because the
# rewrite is a regex-and-replace per row: the project_id comes from
# instance_config (file/env), not the DB. SQL alone can't pull the
# project_id and substitute it. If the project isn't configured at
# migration time, log a warning per affected row and leave them — the
# operator must configure data_source.bigquery.project, restart, and
# the migration will fire on next start (idempotent).
def _replace_for_v24(project_id: str):
    """Build a re.sub replacement function (not a string) so backslash
    sequences in `project_id` aren't interpreted as group references.
    GCP project IDs can't actually contain backslashes, but using a
    function-form replacement is the defensive idiom — it makes the
    intent explicit and removes the dependency on re.sub's replacement-
    string escaping rules."""
    def _repl(m):
        return f"`{project_id}.{m.group(1)}.{m.group(2)}`"
    return _repl


def _v23_to_v24_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    import re as _re

    try:
        from app.instance_config import get_value
        project_id = get_value("data_source", "bigquery", "project", default="") or ""
    except Exception:
        project_id = ""

    pattern = _re.compile(r'bq\."([^"]+)"\."([^"]+)"')

    rows = conn.execute(
        "SELECT id, source_query FROM table_registry "
        "WHERE query_mode = 'materialized' "
        "AND source_query LIKE '%bq.\"%' "
        "AND source_type = 'bigquery'"
    ).fetchall()

    if not rows:
        return  # Nothing to migrate; skip the transaction.

    # If we have rows to migrate AND project_id isn't configured, we cannot
    # rewrite their source_query. Raise BEFORE the schema_version bump so
    # the migration re-runs on the NEXT startup (after the operator
    # configures the project). Pre-fix the function logged a warning per
    # row and returned normally — the schema_version then bumped to 24
    # unconditionally, the `if current < 24:` gate skipped this function
    # forever after, and rows stayed in DuckDB-flavor SQL. The new
    # `_wrap_admin_sql_for_jobs_api` wrapping path then rejected those
    # rows at materialize time as unparseable BQ SQL with no automatic
    # recovery (Devin Review on db.py:1757). Side effect: a BQ-using
    # deployment that hasn't set the project blocks startup until they
    # do — that's the right call for a config error that would otherwise
    # silently break materialized tables.
    if not project_id:
        raise RuntimeError(
            f"v24 migration cannot complete: {len(rows)} materialized "
            f"BigQuery row(s) need their source_query rewritten from "
            f"DuckDB-flavor `bq.\"ds\".\"tbl\"` to BQ-native "
            f"`<project>.ds.tbl`, but `data_source.bigquery.project` is "
            f"not configured. Set it via /admin/server-config (or "
            f"`instance.yaml: data_source.bigquery.project`) and restart "
            f"the app to retry the migration. The schema version is NOT "
            f"bumped to 24 until this completes; pre-migration DB "
            f"snapshot is at `{_get_state_dir()}/system.duckdb.pre-migrate`."
        )

    conn.execute("BEGIN TRANSACTION")
    try:
        for row_id, sq in rows:
            if sq is None:
                continue
            new_sq = pattern.sub(_replace_for_v24(project_id), sq)
            if new_sq != sq:
                conn.execute(
                    "UPDATE table_registry SET source_query = ? WHERE id = ?",
                    [new_sq, row_id],
                )
                logger.info(
                    "v24 migration: rewrote source_query for row %r", row_id,
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they don't exist. Apply migrations if schema version changed.

    Self-heal pass for split-brain DBs runs only when ``current >=
    SCHEMA_VERSION``. Scenario: a contributor's DB landed at
    ``schema_version=N`` from a partial migration (crash mid-DDL,
    parallel WIP branch with a different table set, etc.), but the
    on-disk file is missing tables this binary expects. Without this
    pass, the migration block below skips because we don't downgrade,
    and every runtime query against the missing table crashes.

    Because ``_SYSTEM_SCHEMA`` is all ``CREATE TABLE IF NOT EXISTS``,
    running it is idempotent: existing tables stay untouched (columns +
    data preserved), missing tables get created. Cost: dozens of no-op
    DDLs per process start.

    The self-heal explicitly does NOT run on the ``current <
    SCHEMA_VERSION`` path so the pre-migration snapshot taken inside
    that branch captures a true point-in-time state of the on-disk DB
    *before* any DDL runs — operators reading the snapshot for rollback
    debugging see exactly the tables the old schema had, not the
    binary's full table set with extras tacked on.
    """
    current = get_schema_version(conn)
    if current >= SCHEMA_VERSION:
        # Split-brain or same-version safety net: heal any tables this
        # binary expects that aren't on disk. Migration block skipped
        # because we don't downgrade — the version row is left at
        # ``current`` so a later binary that understands ``current``
        # picks up where the split-brain left off.
        conn.execute(_SYSTEM_SCHEMA)
    if current < SCHEMA_VERSION:
        # Snapshot before migration for rollback support
        if current > 0:
            try:
                db_path = _get_state_dir() / "system.duckdb"
                if db_path.exists():
                    # Flush WAL to main DB file before copying
                    try:
                        conn.execute("CHECKPOINT")
                    except Exception:
                        pass  # CHECKPOINT may fail on read-only or in-memory DBs
                    snapshot = db_path.parent / "system.duckdb.pre-migrate"
                    shutil.copy2(str(db_path), str(snapshot))
                    # Also copy WAL if it still exists (belt and suspenders)
                    wal_path = Path(str(db_path) + ".wal")
                    if wal_path.exists():
                        shutil.copy2(str(wal_path), str(snapshot) + ".wal")
                    logger.info("Pre-migration snapshot saved: %s", snapshot)
            except Exception as e:
                logger.warning("Could not create pre-migration snapshot: %s", e)
        conn.execute(_SYSTEM_SCHEMA)
        if current == 0:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                [SCHEMA_VERSION],
            )
            # v22 setup_banner row (kept as compat per CLAUDE.md schema notes).
            conn.execute(
                "INSERT INTO setup_banner (id, content) VALUES (1, NULL) "
                "ON CONFLICT (id) DO NOTHING"
            )
            # v26 instance_templates seed — three canonical keys with NULL
            # content (operator override absent → render OSS default).
            for key in ("welcome", "claude_md", "home"):
                conn.execute(
                    "INSERT INTO instance_templates (key, content) VALUES (?, NULL) "
                    "ON CONFLICT (key) DO NOTHING",
                    [key],
                )
            # Fresh-install seed is handled by the unconditional
            # _seed_core_roles call at the bottom of _ensure_schema —
            # left as a no-op branch here so the migration ladder still
            # reads chronologically.
        else:
            if current < 2:
                for sql in _V1_TO_V2_MIGRATIONS:
                    conn.execute(sql)
            if current < 3:
                for sql in _V2_TO_V3_MIGRATIONS:
                    conn.execute(sql)
            if current < 4:
                for sql in _V3_TO_V4_MIGRATIONS:
                    conn.execute(sql)
            if current < 5:
                for sql in _V4_TO_V5_MIGRATIONS:
                    conn.execute(sql)
            if current < 6:
                for sql in _V5_TO_V6_MIGRATIONS:
                    conn.execute(sql)
            if current < 7:
                for sql in _V6_TO_V7_MIGRATIONS:
                    conn.execute(sql)
            if current < 8:
                for sql in _V7_TO_V8_MIGRATIONS:
                    conn.execute(sql)
            if current < 9:
                for sql in _V8_TO_V9_MIGRATIONS:
                    conn.execute(sql)
                # v9 finalize: seed core.* roles, backfill grants from
                # legacy users.role, then drop the column. Order matters —
                # backfill needs the seed rows to exist; drop must be last.
                _seed_core_roles(conn)
                _backfill_users_role_to_grants(conn)
                # DuckDB rejects DROP COLUMN while user_role_grants FK
                # references users(id), so we NULL the legacy values instead
                # — UserRepository ignores the column going forward. Physical
                # drop is deferred to a future schema-rebuild migration.
                # Skip UPDATE if the column never existed (e.g. test fixtures
                # starting from v2/v3 with a hand-crafted minimal users table).
                has_role_col = conn.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = 'role'"
                ).fetchone()
                if has_role_col:
                    conn.execute("UPDATE users SET role = NULL")
            if current < 10:
                for sql in _V9_TO_V10_MIGRATIONS:
                    conn.execute(sql)
            if current < 11:
                for sql in _V10_TO_V11_MIGRATIONS:
                    conn.execute(sql)
            if current < 12:
                for sql in _V11_TO_V12_MIGRATIONS:
                    conn.execute(sql)
            if current < 13:
                for sql in _V12_TO_V13_MIGRATIONS:
                    conn.execute(sql)
                _v12_to_v13_finalize(conn)
            if current < 14:
                _v13_to_v14_finalize(conn)
            if current < 15:
                for sql in _V14_TO_V15_MIGRATIONS:
                    conn.execute(sql)
            if current < 16:
                for sql in _V15_TO_V16_MIGRATIONS:
                    conn.execute(sql)
            if current < 17:
                for sql in _V16_TO_V17_MIGRATIONS:
                    conn.execute(sql)
            if current < 18:
                _v17_to_v18_finalize(conn)
            if current < 19:
                _v18_to_v19_finalize(conn)
            if current < 20:
                for sql in _V19_TO_V20_MIGRATIONS:
                    conn.execute(sql)
            if current < 21:
                for sql in _V20_TO_V21_MIGRATIONS:
                    conn.execute(sql)
            if current < 22:
                for sql in _V21_TO_V22_MIGRATIONS:
                    conn.execute(sql)
            if current < 23:
                for sql in _V22_TO_V23_MIGRATIONS:
                    conn.execute(sql)
            if current < 24:
                _v23_to_v24_finalize(conn)
            if current < 25:
                for sql in _V24_TO_V25_MIGRATIONS:
                    conn.execute(sql)
            if current < 26:
                for sql in _V25_TO_V26_MIGRATIONS:
                    conn.execute(sql)
            if current < 27:
                for sql in _V26_TO_V27_MIGRATIONS:
                    conn.execute(sql)
            if current < 28:
                for sql in _V27_TO_V28_MIGRATIONS:
                    conn.execute(sql)
            if current < 29:
                for sql in _V28_TO_V29_MIGRATIONS:
                    conn.execute(sql)
                _v28_to_v29_finalize(conn)
            if current < 30:
                for sql in _V29_TO_V30_MIGRATIONS:
                    conn.execute(sql)
            if current < 31:
                _v30_to_v31_migrate(conn)
            if current < 32:
                for sql in _V31_TO_V32_MIGRATIONS:
                    conn.execute(sql)
            if current < 33:
                for sql in _V32_TO_V33_MIGRATIONS:
                    conn.execute(sql)
            if current < 34:
                for sql in _V33_TO_V34_MIGRATIONS:
                    conn.execute(sql)
            if current < 35:
                _v34_to_v35_migrate(conn)
            if current < 36:
                for sql in _V35_TO_V36_MIGRATIONS:
                    conn.execute(sql)
            if current < 37:
                for sql in _V36_TO_V37_MIGRATIONS:
                    conn.execute(sql)
            if current < 38:
                for sql in _V37_TO_V38_MIGRATIONS:
                    conn.execute(sql)
            if current < 39:
                for sql in _V38_TO_V39_MIGRATIONS:
                    conn.execute(sql)
            conn.execute(
                "UPDATE schema_version SET version = ?, applied_at = current_timestamp",
                [SCHEMA_VERSION],
            )
            # Force WAL → main DB consolidation immediately after the
            # migration ladder. Without this, the v27 `ALTER TABLE
            # table_registry ADD COLUMN` statements sit in
            # `system.duckdb.wal` until DuckDB's next implicit checkpoint;
            # if the container is killed in that window (e.g. by the
            # auto-upgrade cron's `docker compose up -d` mid-deploy),
            # the next start's WAL replay hits an `INTERNAL Error:
            # Calling DatabaseManager::GetDefaultDatabase with no default
            # database set` on the `ReplayAlter` path and the system
            # database becomes unrecoverable from the running binary —
            # the operator has to restore from the pre-migrate snapshot
            # by hand. This was reproduced on agnes-dev during PR #217
            # rollout: container restart 5s after the v27 migration
            # window left the DB in an unhealthy=db_schema=unreachable
            # state.
            #
            # CHECKPOINT flushes the WAL to the main DB file
            # synchronously. Best-effort: if it fails (read-only handle,
            # in-memory DB, or transient lock), log and continue —
            # exactly the same exposure as before this fix.
            try:
                conn.execute("CHECKPOINT")
            except Exception as e:
                logger.warning(
                    "Post-migration CHECKPOINT failed (%s); WAL may "
                    "contain unflushed ALTER ops. A clean shutdown of "
                    "this process before any container restart is the "
                    "safe path; otherwise, the next start may need to "
                    "restore from %s.",
                    e, _get_state_dir() / "system.duckdb.pre-migrate",
                )

    # Always run the system-groups seed when the DB is on a version this binary
    # understands — per-connect safety net so a manually-deleted Admin/Everyone
    # row reappears next start. Two UPSERTs; near-zero cost. Lives outside the
    # migration guard so:
    #   1. recovery: deleted system group reappears on next start;
    #   2. fresh installs: the (current == 0) branch above doesn't need its own
    #      seed — _SYSTEM_SCHEMA created the user_groups table empty.
    # Skip when current > SCHEMA_VERSION (future-version-noop rollback contract).
    if get_schema_version(conn) <= SCHEMA_VERSION:
        _seed_system_groups(conn)


def get_schema_version(conn: duckdb.DuckDBPyConnection) -> int:
    """Get current schema version. Returns 0 if no schema exists."""
    try:
        result = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return result[0] if result and result[0] else 0
    except duckdb.CatalogException:
        return 0


def close_system_db() -> None:
    """Close the shared system DB connection. Called on app shutdown.

    CHECKPOINT before close so the WAL flushes into ``system.duckdb`` and
    the file is left in a clean state. If we skip this and the process
    later gets SIGKILL'd (e.g. Docker's default 10s stop_grace_period
    expires during ``docker compose up -d`` recreate), DuckDB leaves a
    populated ``.wal`` that the next process must replay on open. When
    the next process is a different DuckDB version (image upgrade
    window), replay can hit internal assertions like
    ``Failure while replaying WAL ... GetDefaultDatabase with no default
    database set`` and the app 500s on every authed request.

    CHECKPOINT is best-effort: if it raises (locked, disk full, etc.)
    we still proceed to close — the recovery path in ``_try_open_system_db``
    plus the longer ``stop_grace_period`` in compose are the safety nets.
    """
    global _system_db_conn, _system_db_path
    if _system_db_conn:
        try:
            _system_db_conn.execute("CHECKPOINT")
            logger.debug("close_system_db: CHECKPOINT ok")
        except Exception as exc:
            # Log + proceed — CHECKPOINT failure is not fatal (recovery path
            # in _try_open_system_db handles a dirty WAL on next open), but
            # we want operators to see WHY the safety net was needed if a
            # WAL-replay failure does surface later.
            logger.warning("close_system_db: CHECKPOINT failed (%s); proceeding to close", exc)
        try:
            _system_db_conn.close()
        except Exception as exc:
            logger.debug("close_system_db: close raised (%s); ignoring", exc)
        _system_db_conn = None
        _system_db_path = None
