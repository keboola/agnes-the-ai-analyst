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


# Re-export the lightweight helper. The implementation lives in
# `src.duckdb_conn` so connectors / CLI / scripts can import it without
# pulling the heavy `connectors.bigquery.auth` dep that this module
# imports above.
from src.duckdb_conn import _open_duckdb  # noqa: F401, E402  (re-export)


_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

SCHEMA_VERSION = 65

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
    onboarded BOOLEAN NOT NULL DEFAULT FALSE,
    -- v44: per-user pull timestamp. Bumped on every GET /api/sync/manifest
    -- so `agnes pull` (and the SessionStart hook that wraps it) imprints
    -- the user's last sync time. Powers the /home status frame's "Last
    -- sync" card.
    last_pull_at TIMESTAMP
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
    -- v49: the scalar ``domain`` column was replaced by the
    -- ``knowledge_item_domains`` M:N junction (see ``memory_domains``
    -- and the v49 backfill in ``_v51_to_v52``).
    entities JSON,
    source_type VARCHAR DEFAULT 'claude_local_md',
    source_ref VARCHAR,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    supersedes VARCHAR,
    sensitivity VARCHAR DEFAULT 'internal',
    is_personal BOOLEAN DEFAULT FALSE,
    -- v49: governance Required tier, split out of the v15-era
    -- status='mandatory' overload. status now tracks lifecycle only
    -- (pending/approved/rejected); is_required is the orthogonal
    -- "must appear in the bundle, cannot be dismissed" flag.
    is_required BOOLEAN DEFAULT FALSE,
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

-- v46: per-user opt-out for knowledge items. A row here means the user has
-- dismissed an item from their personal AI bundle and (optionally) their
-- listing — but mandatory items can never be dismissed; the governance
-- hard rule is enforced API-side and reinforced by the SQL filter via
-- ``status != 'mandatory'`` in the EXISTS subquery in list_items/search/
-- count_items/bundle. Idempotent inserts (ON CONFLICT do nothing).
CREATE TABLE IF NOT EXISTS knowledge_item_user_dismissed (
    user_id VARCHAR NOT NULL,
    item_id VARCHAR NOT NULL,
    dismissed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (user_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_item_user_dismissed_user
    ON knowledge_item_user_dismissed(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id VARCHAR PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL DEFAULT current_timestamp,
    user_id VARCHAR,
    action VARCHAR NOT NULL,
    resource VARCHAR,
    params JSON,
    result VARCHAR,
    duration_ms INTEGER,
    params_before JSON,
    client_ip VARCHAR,
    client_kind VARCHAR,
    correlation_id VARCHAR
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
    initial_load_chunk_days INTEGER,
    -- v51: fully-qualified BigQuery path (`project.dataset.table`) for
    -- BigQuery rows. When set, decouples the UX/RBAC `bucket` label from
    -- the physical BQ dataset name; rows without it fall back to the
    -- legacy `<remote_attach.project>.<bucket>.<source_table>` path.
    -- Issue #343 (released on main as 0.54.29).
    bq_fqn VARCHAR,
    -- v55: per-table docs surface used by /catalog/t/<id>. All
    -- admin-authored, optional. sample_questions + pairs_well_with are
    -- JSON arrays so admins can edit lists without us cascading a new
    -- junction table; things_to_know is freeform notes (markdown-ish
    -- treated as plain text on render).
    sample_questions JSON,
    things_to_know   TEXT,
    pairs_well_with  JSON,
    -- v59: structured per-table documentation for the package-detail
    -- rewrite. ``grain`` (e.g. "1 row per session × event_date"),
    -- ``platforms`` (JSON list of platform names), ``partition_col``
    -- (single column name — distinct from the v33-era ``partition_by``
    -- which carries BigQuery partition-key SQL), ``history`` ("Full",
    -- "Rolling 15 months", "Nov 2025+"), ``gotchas`` (JSON list of
    -- ``{key: bool, body: str}`` — first ``key=true`` is rendered
    -- distinctly as the "Key gotcha"). All additive + NULLABLE.
    grain         VARCHAR,
    platforms     VARCHAR,
    partition_col VARCHAR,
    history       VARCHAR,
    gotchas       VARCHAR
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

-- v60: short-lived setup tokens for the Agnes Cowork one-click setup flow.
-- Generated by POST /api/user/cowork-bundle, consumed once by
-- POST /api/auth/exchange-setup-token which mints a regular PAT.
-- token_hash = SHA-256(raw "st_..." value) — plaintext never stored.
-- used_at NULL = token still valid; non-NULL = already consumed.
CREATE TABLE IF NOT EXISTS setup_tokens (
    id          VARCHAR PRIMARY KEY,
    user_id     VARCHAR NOT NULL,
    token_hash  VARCHAR NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    used_at     TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
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
-- v49: ``requirement`` enum splits Required-tier semantics out of the
-- grant identity. ``available`` (default) — grantee can opt in via
-- ``user_stack_subscriptions``. ``required`` — auto-included in the
-- effective stack, opt-out blocked at the API. Applies to
-- ``data_package`` / ``memory_domain`` / ``memory_item`` grants;
-- ``marketplace_plugin`` Required-tier stays on
-- ``marketplace_plugins.is_system`` per D1.
CREATE TABLE IF NOT EXISTS resource_grants (
    id            VARCHAR PRIMARY KEY,
    group_id      VARCHAR NOT NULL REFERENCES user_groups(id),
    resource_type VARCHAR NOT NULL,
    resource_id   VARCHAR NOT NULL,
    requirement   VARCHAR DEFAULT 'available',
    assigned_at   TIMESTAMP DEFAULT current_timestamp,
    assigned_by   VARCHAR,
    UNIQUE (group_id, resource_type, resource_id)
);

-- v49: Data Packages — admin-curated bundles of tables. A package is a
-- single Browse / Add-to-stack unit; effective TABLE set for RBAC =
-- direct TABLE grants ∪ tables in DATA_PACKAGE grants. See
-- ``docs/brainstorms/2026-05-15-unified-stack-design.md`` section 3.3.
CREATE TABLE IF NOT EXISTS data_packages (
    id              VARCHAR PRIMARY KEY,
    slug            VARCHAR UNIQUE NOT NULL,
    name            VARCHAR NOT NULL,
    description     TEXT,
    icon            VARCHAR,
    color           VARCHAR,
    -- v50: admin-uploaded cover image (served from /uploads/covers/<sha>.<ext>).
    -- Closes the visual gap with /marketplace cards which render real
    -- JPGs/PNGs; cards fall back to 2-letter initials when this is NULL.
    cover_image_url VARCHAR,
    -- v51: lifecycle + classification surface for /catalog cards. The
    -- card eyebrow renders ``category``; the cover-corner status pill
    -- renders ``status``. Hero filter checkboxes filter by status.
    -- ``status`` is a soft enum ('prod' default; 'poc'; 'coming-soon';
    -- 'draft' admin-only). ``category`` is free-form text for the
    -- eyebrow line — admins should keep it short and consistent
    -- (e.g. "Sessions & Traffic", "Customer Insights").
    status          VARCHAR DEFAULT 'prod',
    category        VARCHAR,
    -- v54: soft-delete column. DELETE handlers set this instead of
    -- removing the row, so junction tables + resource_grants survive
    -- for the undo flow. list/get filter ``deleted_at IS NULL``.
    deleted_at      TIMESTAMP,
    -- v56: extended content for the /catalog/p/<slug> detail-page
    -- rewrite (extended-descriptions admin spec). All additive + NULLABLE.
    --   owner_name / owner_team — render "Owned by X · Team" line
    --   tags                    — JSON list of category strings
    --   long_description        — markdown body for "What it is"
    --   when_to_use / when_not_to_use
    --                           — JSON bullet lists
    --   example_questions       — JSON list of analyst questions
    --                             surfaced as a package-level prompt
    --                             panel.
    -- Badges (`curated` / `new`) are NOT persisted columns — they're
    -- derived at render time from creator group + created_at age, so
    -- backdating or admin-status changes pick up automatically.
    owner_name      VARCHAR,
    owner_team      VARCHAR,
    tags            VARCHAR,
    long_description TEXT,
    when_to_use     VARCHAR,
    when_not_to_use VARCHAR,
    example_questions VARCHAR,
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS data_package_tables (
    package_id  VARCHAR NOT NULL REFERENCES data_packages(id),
    table_id    VARCHAR NOT NULL REFERENCES table_registry(id),
    added_at    TIMESTAMP DEFAULT current_timestamp,
    added_by    VARCHAR,
    PRIMARY KEY (package_id, table_id)
);
CREATE INDEX IF NOT EXISTS idx_data_package_tables_table
    ON data_package_tables(table_id);

-- v49: Memory Domains — first-class entities replacing the v15 scalar
-- ``knowledge_items.domain`` string. Junction allows an item to belong
-- to multiple domains; admin can create non-canonical domains beyond the
-- legacy ``VALID_DOMAINS`` six. See spec section 3.4.
CREATE TABLE IF NOT EXISTS memory_domains (
    id              VARCHAR PRIMARY KEY,
    slug            VARCHAR UNIQUE NOT NULL,
    name            VARCHAR NOT NULL,
    description     TEXT,
    icon            VARCHAR,
    color           VARCHAR,
    -- v50: admin-uploaded cover image — same path / fallback contract as
    -- data_packages.cover_image_url above.
    cover_image_url VARCHAR,
    -- v51: lifecycle ``status`` only ('prod' / 'poc' / 'coming-soon' /
    -- 'draft'). Memory Domains don't carry ``category`` because the
    -- domain itself IS the classification — adding a second-level
    -- category would be redundant.
    status          VARCHAR DEFAULT 'prod',
    -- v54: soft-delete (see data_packages.deleted_at).
    deleted_at      TIMESTAMP,
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS knowledge_item_domains (
    item_id   VARCHAR NOT NULL REFERENCES knowledge_items(id),
    domain_id VARCHAR NOT NULL REFERENCES memory_domains(id),
    added_at  TIMESTAMP DEFAULT current_timestamp,
    added_by  VARCHAR,
    PRIMARY KEY (item_id, domain_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_item_domains_domain
    ON knowledge_item_domains(domain_id);

-- v55: ``memory_domain_suggestions`` — non-admin users can suggest a new
-- domain from /corporate-memory empty state. Admin queue surfaces them
-- with one-click approve (creates the real ``memory_domains`` row +
-- marks the suggestion ``status='approved'``) or reject. Open suggestions
-- have ``status='pending'``; resolved ones keep the row for audit so the
-- requester sees the disposition. No FK on ``created_by`` so a deleted
-- user doesn't cascade-nuke their suggestion history.
CREATE TABLE IF NOT EXISTS memory_domain_suggestions (
    id              VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    description     TEXT,
    rationale       TEXT,
    status          VARCHAR DEFAULT 'pending',  -- 'pending' / 'approved' / 'rejected'
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    resolved_at     TIMESTAMP,
    resolved_by     VARCHAR,
    resolution_note TEXT,
    -- When approved, the resulting memory_domains.id so the admin queue
    -- can deep-link to the created domain.
    created_domain_id VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_memory_domain_suggestions_status
    ON memory_domain_suggestions(status);

-- v61: ``cli_auth_codes`` — short-lived, single-use exchange codes for the
-- browser-loopback `agnes auth login` flow (gh-style). The browser, holding
-- an authenticated session, confirms CLI authorization; the server mints a
-- code (hash stored here, bound to the user) and redirects it to the CLI's
-- localhost loopback. The CLI then POSTs the code to /cli/auth/exchange over
-- HTTPS and receives a real PAT — so the durable credential never travels
-- through the browser address bar / history. Codes expire in ~2 min and are
-- consumed exactly once (compare-and-swap on ``consumed_at``). Rows are left
-- after expiry/consumption for a short audit window; a cheap opportunistic
-- delete of expired rows runs on each create.
CREATE TABLE IF NOT EXISTS cli_auth_codes (
    code_hash   VARCHAR PRIMARY KEY,   -- sha256(raw code); raw code never stored
    user_id     VARCHAR NOT NULL,
    email       VARCHAR NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
    expires_at  TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP
);

-- v53: Recipes are admin-curated, multi-table query templates analysts
-- copy + adapt. Sibling concept to Data Packages on /catalog (separate
-- "Recipes" tab). Not stack subscribable — analysts use a recipe, they
-- don't opt in to it. ``related_table_ids`` is a JSON array of
-- ``table_registry.id`` values so the recipe drilldown can render
-- per-table links without us cascading another junction table.
CREATE TABLE IF NOT EXISTS recipes (
    id              VARCHAR PRIMARY KEY,
    slug            VARCHAR UNIQUE NOT NULL,
    title           VARCHAR NOT NULL,
    description     TEXT,
    icon            VARCHAR,
    color           VARCHAR,
    sql_template    TEXT,
    related_table_ids JSON,
    status          VARCHAR DEFAULT 'prod',
    -- v54: soft-delete (see data_packages.deleted_at).
    deleted_at      TIMESTAMP,
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

-- v49: generic per-user opt-in for resource_grants flagged
-- ``requirement='available'``. Currently scoped to ``data_package`` /
-- ``memory_domain`` resource types — Marketplace pluginy stay on the
-- existing ``user_plugin_optouts`` opt-out shape per D1.
CREATE TABLE IF NOT EXISTS user_stack_subscriptions (
    user_id       VARCHAR NOT NULL,
    resource_type VARCHAR NOT NULL,
    resource_id   VARCHAR NOT NULL,
    subscribed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (user_id, resource_type, resource_id)
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
    -- v49: phase-1 Flea refactor adds three user-facing metadata columns.
    -- `title` is a humanized display name (acronym-aware), shown on web
    -- surfaces instead of the kebab-case `name`. `tagline` is an optional
    -- 200-char short description for card UI (long-form lives in
    -- `description`). `synthetic_name` is the deterministic
    -- `<name>-by-<owner_username>` value baked into served bundles —
    -- stored as a column so attribution + uniqueness checks can target a
    -- single source of truth instead of recomputing the concat on every
    -- query. Phase 1 only populates these; downstream surfaces (cards,
    -- detail pages, Claude Code propagation) consume them in later phases.
    title             VARCHAR NOT NULL,
    tagline           VARCHAR,
    synthetic_name    VARCHAR NOT NULL,
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
-- NOTE: the v50 UNIQUE INDEX on store_entities.synthetic_name is created
-- by ``_v49_to_v50_migrate``, not here. Reason: ``_v48_to_v49_migrate``
-- runs ``ALTER TABLE store_entities ALTER COLUMN … SET NOT NULL`` which
-- DuckDB blocks when an index already references the table. Fresh-install
-- ordering is therefore: CREATE TABLE (no index) → v49 migrate (no-op
-- ALTERs on empty table) → v50 migrate (CREATE UNIQUE INDEX).
-- NOTE: no created_at index. DuckDB 1.x has a bug where
-- `ORDER BY <indexed col> DESC LIMIT N` short-returns on small tables
-- (reproduced with N=2 against 3 rows during /admin/store/submissions
-- paging). Submissions table is admin-only and bounded by upload
-- volume, so the index buys little; dropping it sidesteps the bug.

-- v40: persistent metadata cache for remote sources (BigQuery initially).
-- Replaces the per-request, in-memory `_metadata_cache` in v2_catalog.py
-- that turned every cold-cache /api/v2/catalog into a sequence of N×3 BQ
-- jobs API calls (one TABLE_STORAGE + COLUMNS pair per remote row) — long
-- enough on view-backed or partitioned tables (>>30 s) to blow the CLI's
-- httpx 30 s read timeout. Now refresh is driven exclusively by the
-- scheduler (default every 4 h, `SCHEDULER_BQ_METADATA_REFRESH_INTERVAL`),
-- and the catalog endpoint just reads this table — no BQ at request time.
--
-- Columns:
--   table_id          — registry.id; PK and join key with table_registry.
--   rows / size_bytes / partition_by / clustered_by — last successful
--                       provider result. NULL when the table has never
--                       been fetched, or fetch failed before any success.
--                       clustered_by stored as JSON array of column names.
--   refreshed_at      — wall-clock of the last successful fetch. Used by
--                       the catalog response to compute metadata_freshness
--                       (`fresh` if < 2× scheduler interval old, `stale`
--                       otherwise, `never_fetched` if NULL).
--   error_at / error_msg — last failure timestamp + redacted message.
--                       NULL after the next successful refresh.
CREATE TABLE IF NOT EXISTS bq_metadata_cache (
    table_id        VARCHAR PRIMARY KEY,
    rows            BIGINT,
    size_bytes      BIGINT,
    partition_by    VARCHAR,
    clustered_by    JSON,
    -- BigQuery entity classification, surfaced in catalog so analyst Claude
    -- can decide query strategy. Values mirror INFORMATION_SCHEMA.TABLES.
    -- table_type: `BASE TABLE`, `VIEW`, `MATERIALIZED VIEW`, `EXTERNAL`,
    -- `SNAPSHOT`, `CLONE`. NULL until first successful refresh.
    entity_type     VARCHAR,
    -- Cache of known column names from the most recent successful refresh,
    -- as JSON array of strings. Used by /api/v2/catalog to filter generic
    -- where_examples against the table's actual schema — drops example
    -- predicates that reference columns the table doesn't have. Populated
    -- by bq_metadata_refresh.refresh_one from fetch_bq_columns_full, so
    -- there is no extra BQ roundtrip just for this.
    known_columns   JSON,
    refreshed_at    TIMESTAMP,
    error_at        TIMESTAMP,
    error_msg       VARCHAR
);
-- Self-heal for instances that already ran an earlier v40 incarnation
-- that lacked entity_type / known_columns. The CREATE TABLE above is
-- IF NOT EXISTS so it skips on already-existing tables; these ALTERs
-- close the column-set gap. Idempotent on fresh installs (no-op).
ALTER TABLE bq_metadata_cache ADD COLUMN IF NOT EXISTS entity_type VARCHAR;
ALTER TABLE bq_metadata_cache ADD COLUMN IF NOT EXISTS known_columns JSON;

-- v42 (was v41 pre-rebase): usage telemetry tables — per-event log,
-- per-session aggregate, daily rollups, and attribution tables for
-- skills/agents/commands.
CREATE TABLE IF NOT EXISTS usage_events (
    id                  VARCHAR PRIMARY KEY,
    session_id          VARCHAR NOT NULL,
    session_file        VARCHAR NOT NULL,
    username            VARCHAR NOT NULL,
    event_uuid          VARCHAR,
    parent_uuid         VARCHAR,
    event_type          VARCHAR NOT NULL,
    tool_name           VARCHAR,
    skill_name          VARCHAR,
    subagent_type       VARCHAR,
    command_name        VARCHAR,
    is_error            BOOLEAN DEFAULT FALSE,
    source              VARCHAR NOT NULL,
    ref_id              VARCHAR,
    model               VARCHAR,
    cwd                 VARCHAR,
    occurred_at         TIMESTAMP NOT NULL,
    processor_version   INTEGER NOT NULL,
    extracted_at        TIMESTAMP DEFAULT current_timestamp,
    friction_tags       JSON,
    user_id             VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_user_time ON usage_events(username, occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_tool ON usage_events(tool_name);
CREATE INDEX IF NOT EXISTS idx_usage_events_skill ON usage_events(skill_name);
CREATE INDEX IF NOT EXISTS idx_usage_events_ref ON usage_events(source, ref_id);
-- idx_usage_events_user_id is created by _v44_to_v45, not here: _SYSTEM_SCHEMA
-- runs before the migration ladder, and CREATE TABLE IF NOT EXISTS won't add
-- user_id to a pre-v45 usage_events, so an index on it would fail to bind.
-- Same pattern as the v41 audit_log indices below.

CREATE TABLE IF NOT EXISTS usage_session_summary (
    session_file        VARCHAR PRIMARY KEY,
    session_id          VARCHAR NOT NULL,
    username            VARCHAR NOT NULL,
    started_at          TIMESTAMP,
    ended_at            TIMESTAMP,
    active_seconds      INTEGER,
    wall_seconds        INTEGER,
    user_messages       INTEGER DEFAULT 0,
    assistant_messages  INTEGER DEFAULT 0,
    tool_calls          INTEGER DEFAULT 0,
    tool_errors         INTEGER DEFAULT 0,
    skill_invocations   INTEGER DEFAULT 0,
    subagent_dispatches INTEGER DEFAULT 0,
    mcp_calls           INTEGER DEFAULT 0,
    slash_commands      INTEGER DEFAULT 0,
    distinct_tools      INTEGER DEFAULT 0,
    distinct_skills     INTEGER DEFAULT 0,
    primary_model       VARCHAR,
    processor_version   INTEGER NOT NULL,
    extracted_at        TIMESTAMP DEFAULT current_timestamp,
    -- v44: per-session token counters summed from JSONL message.usage.*.
    -- BIGINT because cache tokens routinely exceed INT range over long
    -- sessions. Default 0 so existing rows backfill cleanly; the
    -- processor's reprocess loop (driven by USAGE_PROCESSOR_VERSION
    -- bump) overwrites with real values on next tick.
    input_tokens          BIGINT DEFAULT 0,
    output_tokens         BIGINT DEFAULT 0,
    cache_read_tokens     BIGINT DEFAULT 0,
    cache_creation_tokens BIGINT DEFAULT 0,
    user_id               VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_usage_session_user ON usage_session_summary(username);
CREATE INDEX IF NOT EXISTS idx_usage_session_started ON usage_session_summary(started_at);
-- idx_usage_session_user_id is created by _v44_to_v45, not here — see the
-- note on idx_usage_events_user_id above.

-- usage_tool_daily: legacy rollup of tool invocations by day/source. Currently
-- only consumed by `src/usage_ask.py` SCHEMA_DIGEST + admin reprocess endpoint;
-- has no product-UI consumer. Marked as candidate for removal in v46; will be
-- evaluated for full drop in next telemetry refactor iteration.
CREATE TABLE IF NOT EXISTS usage_tool_daily (
    day                 DATE NOT NULL,
    tool_name           VARCHAR NOT NULL,
    source              VARCHAR NOT NULL,
    invocations         INTEGER DEFAULT 0,
    error_count         INTEGER DEFAULT 0,
    distinct_users      INTEGER DEFAULT 0,
    distinct_sessions   INTEGER DEFAULT 0,
    PRIMARY KEY (day, tool_name, source)
);

-- v46: marketplace item telemetry rollup. Per-day fact table; window snapshot
-- is sibling `usage_marketplace_item_window`.
-- `parent_plugin` is '' (empty string, not NULL) for type='plugin' rows and
-- standalone flea entities — keeps composite PK well-defined without NULL gymnastics.
CREATE TABLE IF NOT EXISTS usage_marketplace_item_daily (
    day            DATE    NOT NULL,
    source         VARCHAR NOT NULL,            -- 'curated' | 'flea' | 'builtin'
    type           VARCHAR NOT NULL,            -- 'plugin' | 'skill' | 'agent'
    parent_plugin  VARCHAR NOT NULL DEFAULT '', -- '' = no parent
    name           VARCHAR NOT NULL,
    count          INTEGER NOT NULL DEFAULT 0,
    distinct_users INTEGER NOT NULL DEFAULT 0, -- per-day COUNT(DISTINCT user_id)
    error_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source, type, parent_plugin, name)
);
CREATE INDEX IF NOT EXISTS idx_mid_lookup ON usage_marketplace_item_daily(source, type, parent_plugin, name);

-- v46: sliding-window snapshot for marketplace items. Refreshed by
-- `rebuild_rollups` — last_7d every UsageProcessor tick (~10 min),
-- last_30d hourly. `distinct_users` here is the TRUE distinct count
-- across the window (recomputed from usage_events at rebuild time),
-- not a sum of per-day distincts.
CREATE TABLE IF NOT EXISTS usage_marketplace_item_window (
    period_label   VARCHAR NOT NULL,            -- 'last_7d' | 'last_30d' (extensible)
    source         VARCHAR NOT NULL,
    type           VARCHAR NOT NULL,
    parent_plugin  VARCHAR NOT NULL DEFAULT '',
    name           VARCHAR NOT NULL,
    invocations    INTEGER NOT NULL DEFAULT 0,
    distinct_users INTEGER NOT NULL DEFAULT 0,  -- true sliding-window distinct
    refreshed_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (period_label, source, type, parent_plugin, name)
);
CREATE INDEX IF NOT EXISTS idx_miw_lookup ON usage_marketplace_item_window(period_label, source, type);
"""


import threading

_system_db_lock = threading.Lock()
_system_db_conn: duckdb.DuckDBPyConnection | None = None
_system_db_path: str | None = None

# Mirror the system-DB singleton pattern for the analytics DB. Pre-#163,
# `get_analytics_db()` opened a fresh `duckdb.connect()` on every call —
# most callers don't `.close()` the returned handle, so each leaked
# connection held a WAL ref + FD until GC kicked in. Under load this
# manifested as "too many open files" or DuckDB lock contention on the
# analytics DB. Singleton + cursor-per-call (mirrors `get_system_db()`
# above) means callers that close the cursor only close the cursor —
# the underlying connection stays.
_analytics_db_lock = threading.Lock()
_analytics_db_conn: duckdb.DuckDBPyConnection | None = None
_analytics_db_path: str | None = None

# DuckDB per-connection memory budgets.
#
# DuckDB enforces ``memory_limit`` PER CONNECTION, not per process. In a
# memory-constrained container (e.g. a 4 GiB cgroup) the live connections
# must sum under the cap or the kernel OOM-kills the whole process. DuckDB
# 1.5 is cgroup-aware — a fresh connection defaults to ~80% of the cgroup
# limit — so a single *uncapped* connection can exceed the cap on its own.
# We give each connection an explicit conservative budget:
#
#   system (singleton)             1 GiB    metadata + telemetry aggregations
#   analytics (singleton)          1.5 GiB  working set over parquet views
#   analytics readonly (per req)   1 GiB    one analyst's heavy query
#
# Steady state — the two singletons plus one in-flight readonly query —
# is 3.5 GiB, under a 4 GiB cap with host headroom. The readonly path is
# per-request and unbounded in count (FastAPI threadpool), so a burst of
# concurrent analyst queries can momentarily exceed the cap; the
# ``temp_directory`` disk spill below is the backstop — an over-budget
# query spills to disk (or raises a clean DuckDB OOM) instead of growing
# process RSS. For very memory-constrained, high-concurrency deployments,
# tune AGNES_THREADPOOL_SIZE down too. (Bounding readonly connection
# concurrency directly is a possible follow-up — out of scope here.)
#
# See docs/superpowers/specs/2026-06-01-system-duckdb-resilience-design.md.
_SYSTEM_DB_MEMORY_LIMIT = "1GB"
_ANALYTICS_DB_MEMORY_LIMIT = "1500MB"
_ANALYTICS_RO_MEMORY_LIMIT = "1GB"
_DUCKDB_THREADS = 2
_DUCKDB_MAX_TEMP_DIR_SIZE = "10GB"


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


def _peek_schema_version(snapshot_path: Path) -> int:
    """Open a DuckDB snapshot read-only and return its
    ``MAX(schema_version.version)``.

    Read-only mode bypasses WAL replay entirely — even if the snapshot
    has its own stale WAL, the read-only handle ignores it. Any
    ``duckdb.Error`` (table missing, file corrupt, permission denied)
    is treated conservatively as version 0, so an unreadable snapshot
    fails the freshness check in :func:`_try_open_system_db` and ends
    in the refusal path. Defensive: never returns -1 / None / raises.
    """
    try:
        conn = _open_duckdb(str(snapshot_path), read_only=True)
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except duckdb.Error:
        return 0


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
        return _open_duckdb(db_path)
    except duckdb.Error as e:
        msg = str(e)
        is_wal_replay = (
            "Failure while replaying WAL" in msg
            or "ReplayAlter" in msg
            or "GetDefaultDatabase with no default database set" in msg
        )
        if not is_wal_replay:
            raise
        wal_path = Path(db_path + ".wal")

        # STEP A — salvage the live file. Its last checkpoint is almost
        # always newer than any pre-migrate snapshot (checkpoints run
        # continuously; the snapshot is captured only at migrations).
        # Discard ONLY the unreplayable WAL — DuckDB couldn't apply it
        # anyway — and reopen the file at its last checkpoint. This loses
        # at most the transactions written since that checkpoint, never
        # the days of admin state a stale-snapshot rollback would drop.
        # It also subsumes the mid-migration case: an uncommitted ALTER
        # lives in the discarded WAL, so the file is at the pre-migration
        # version and the idempotent ladder re-runs forward on this start.
        salvaged = _salvage_discard_wal(db_path, wal_path, original_error=e)
        if salvaged is not None:
            return salvaged

        # STEP B — the live file itself won't open. Fall back to the
        # pre-migrate snapshot (with the #379 version guard below). The
        # WAL was already moved aside by Step A, so _move_to_broken here
        # just relocates the unreadable DB file.
        snapshot = Path(db_path).parent / "system.duckdb.pre-migrate"
        if not snapshot.exists():
            logger.error(
                "WAL replay failed, live file unreadable, and no pre-migrate "
                "snapshot at %s — manual recovery required.",
                snapshot,
            )
            raise

        # #379: refuse auto-recovery if the snapshot version doesn't
        # match SCHEMA_VERSION exactly. The migration ladder is
        # idempotent for schema but not for data; re-running it against
        # a stale snapshot (peek < SCHEMA_VERSION) silently drops every
        # row added since the snapshot was captured. The mirror case
        # (peek > SCHEMA_VERSION) is just as bad: an operator rolled
        # the code back, but the snapshot was captured at a later
        # migration transition — auto-recovery would copy the future
        # snapshot in and the next start's _ensure_schema would land
        # in the split-brain "current > target" branch. Both directions
        # mean data corruption; the only safe move is to refuse and
        # surface the version mismatch loudly. The broken DB + WAL are
        # preserved either way for forensics.
        snapshot_version = _peek_schema_version(snapshot)
        if snapshot_version != SCHEMA_VERSION:
            broken = _move_to_broken(db_path, wal_path)
            direction = (
                "stale" if snapshot_version < SCHEMA_VERSION else "future"
            )
            risk = (
                "would re-run the migration ladder and silently drop all "
                f"rows added since v{snapshot_version}"
                if snapshot_version < SCHEMA_VERSION
                else (
                    f"would land the DB at v{snapshot_version} under a "
                    f"v{SCHEMA_VERSION} binary (split-brain)"
                )
            )
            logger.critical(
                "REFUSING auto-recovery: pre-migrate snapshot %s "
                "(snapshot v%d, target v%d). Auto-recovery %s. Broken "
                "DB preserved at %s; broken WAL at %s.wal if it existed. "
                "To accept the snapshot anyway and discard the broken "
                "files, manually run: cp %s %s",
                direction, snapshot_version, SCHEMA_VERSION, risk,
                broken, broken,
                snapshot, db_path,
                exc_info=e,
            )
            raise RuntimeError(
                f"pre-migrate snapshot {direction} "
                f"(v{snapshot_version} vs target v{SCHEMA_VERSION}); "
                f"auto-recovery refused. Broken DB at {broken}. "
                f"Manual recovery: cp {snapshot} {db_path}"
            )

        logger.warning(
            "WAL replay failed (%s) — auto-restoring from pre-migrate "
            "snapshot %s. The migration ladder will re-run on this start.",
            msg.split("\n", 1)[0][:200],
            snapshot,
        )
        # Move (not copy) the broken DB aside so an operator can post-
        # mortem if needed. The pre-migrate snapshot becomes the new
        # main DB; the WAL is dropped (its content is what failed to
        # replay).
        _move_to_broken(db_path, wal_path)
        shutil.copy2(str(snapshot), db_path)
        # Re-open. If THIS also fails, propagate — auto-recovery has
        # exhausted its options.
        return _open_duckdb(db_path)


def _salvage_discard_wal(
    db_path: str, wal_path: Path, *, original_error: Exception
) -> duckdb.DuckDBPyConnection | None:
    """Discard an unreplayable WAL and reopen the DB at its last checkpoint.

    Returns the open connection on success, or ``None`` if the database
    file itself won't open (the caller then falls back to the pre-migrate
    snapshot). The discarded WAL is moved to ``<db>.wal.discarded.<ts>``
    (chmod ``0o600`` — it can hold uncommitted password/PAT writes) and
    preserved for forensics: its content is exactly what DuckDB failed to
    replay.
    """
    if wal_path.exists():
        discarded = Path(str(db_path) + f".wal.discarded.{int(time.time())}")
        try:
            shutil.move(str(wal_path), str(discarded))
            try:
                os.chmod(discarded, 0o600)
            except OSError:
                pass  # best-effort; preservation matters more than mode
        except OSError as move_err:
            logger.error(
                "WAL salvage: could not move WAL aside (%s); cannot reopen",
                move_err,
            )
            return None
    try:
        # Route through `_open_duckdb` so the salvage reopen inherits the
        # same `SET GLOBAL TimeZone='UTC'` pin every other connection gets
        # (frontend timezone fix, #473) — otherwise the WAL-salvage path
        # would silently drop back to the host's local zone.
        conn = _open_duckdb(db_path)
    except duckdb.Error as reopen_err:
        logger.warning(
            "WAL salvage reopen failed (%s); falling back to pre-migrate snapshot",
            reopen_err,
        )
        return None
    logger.warning(
        "WAL replay failed (%s) — discarded the unreplayable WAL and reopened "
        "system.duckdb at its last checkpoint. Transactions written since that "
        "checkpoint are lost; admin state up to the checkpoint is intact.",
        str(original_error).split("\n", 1)[0][:200],
    )
    return conn


def _move_to_broken(db_path: str, wal_path: Path) -> Path:
    """Move the broken DB (+ WAL if present) aside to ``.broken.<ts>``.

    Shared by both branches of :func:`_try_open_system_db` (refusal and
    happy-path recovery). The preserved files are chmod'd to ``0o600``
    because ``system.duckdb`` holds argon2 password hashes, personal-
    access-token rows, and the audit log — ``shutil.move`` inherits the
    source mode (typically ``0o644`` under default umask), so a stale
    ``.broken.*`` would be world-readable on its way out. The
    containing ``state/`` directory is usually ``0o700``, but defense
    in depth matters: backups, container volumes, and tab-completion
    mistakes can all surface the file. Returns the chosen broken path.
    """
    broken = Path(db_path + f".broken.{int(time.time())}")
    shutil.move(db_path, str(broken))
    try:
        os.chmod(broken, 0o600)
    except OSError:
        pass  # best-effort; preservation is more important than mode
    if wal_path.exists():
        broken_wal = str(broken) + ".wal"
        shutil.move(str(wal_path), broken_wal)
        try:
            os.chmod(broken_wal, 0o600)
        except OSError:
            pass
    return broken


def _apply_memory_caps(
    conn: duckdb.DuckDBPyConnection, memory_limit: str, *, label: str
) -> None:
    """Apply defensive memory caps + disk-spill settings to *conn*.

    DuckDB ``memory_limit`` is per-connection; this keeps any single
    connection from growing the process past the container cgroup cap
    (see the ``_*_MEMORY_LIMIT`` constants). Best-effort: a failing PRAGMA
    (read-only DB, in-memory DB, older DuckDB) is logged and skipped so
    the connection stays usable on defaults. The essential caps
    (memory_limit/threads) are applied in their own try so an optional
    temp-spill failure can't undo them.
    """
    try:
        conn.execute(f"SET memory_limit='{memory_limit}'")
        conn.execute(f"SET threads={_DUCKDB_THREADS}")
        conn.execute("SET preserve_insertion_order=false")
    except Exception as e:
        logger.warning(
            "%s: SET memory/threads failed (%s); defaults remain", label, e
        )
    # Disk spill: a query that exceeds its memory budget spills to disk
    # (or raises a clean DuckDB error) instead of OOM-killing the process.
    try:
        tmp = _get_state_dir() / "duckdb-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        conn.execute(f"SET temp_directory='{tmp}'")
        conn.execute(f"SET max_temp_directory_size='{_DUCKDB_MAX_TEMP_DIR_SIZE}'")
    except Exception as e:
        logger.debug("%s: temp_directory spill setup failed (%s)", label, e)


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
            # Cap BEFORE _ensure_schema so migrations + the on-demand FTS
            # index rebuild (src/fts.py) run under the budget too. The
            # system DB was missed by the analytics-only cap in PR #434 —
            # an uncapped singleton was the dominant allocator behind the
            # 4 GiB-cgroup OOM loop.
            _apply_memory_caps(
                _system_db_conn, _SYSTEM_DB_MEMORY_LIMIT, label="get_system_db"
            )
            _system_db_path = db_path
            _ensure_schema(_system_db_conn)
        return _maybe_instrument(_system_db_conn.cursor(), "system")


def get_analytics_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the analytics database (parquet views).

    Singleton — mirrors `get_system_db()` above. Returns a cursor on the
    shared connection so callers can `.close()` the handle without
    closing the underlying connection. Re-opens transparently when
    `DATA_DIR` changes (test fixtures that swap data dirs across cases).

    Pre-#163 this opened a fresh connection on every call and most
    callers leaked it; see the rationale block at the module-level
    `_analytics_db_*` globals. `get_analytics_db_readonly()` deliberately
    stays per-call because each invocation re-ATTACHes extract.duckdb
    files into a fresh read-only context.
    """
    global _analytics_db_conn, _analytics_db_path
    db_path = str(_get_data_dir() / "analytics" / "server.duckdb")

    with _analytics_db_lock:
        if _analytics_db_conn is None or _analytics_db_path != db_path:
            # Close stale connection if DATA_DIR changed (test fixtures)
            if _analytics_db_conn is not None:
                try:
                    _analytics_db_conn.close()
                except Exception:
                    pass
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            # Route through ``_open_duckdb`` so the connection inherits the
            # ``SET GLOBAL TimeZone='UTC'`` pin (frontend timezone fix from
            # #473), then apply the memory cap (OOM resilience from #479).
            _analytics_db_conn = _open_duckdb(db_path)
            # Defensive memory cap (budgeted with the system + readonly
            # connections to sum under a 4 GiB cgroup — see the
            # ``_*_MEMORY_LIMIT`` constants). Analyst-facing queries that
            # hit the cap spill to disk or surface a clear DuckDB OOM
            # exception, rather than a silent process-wide OOM-kill.
            _apply_memory_caps(
                _analytics_db_conn,
                _ANALYTICS_DB_MEMORY_LIMIT,
                label="get_analytics_db",
            )
            _analytics_db_path = db_path
        return _maybe_instrument(_analytics_db_conn.cursor(), "analytics")


def _reattach_remote_extensions(conn: duckdb.DuckDBPyConnection, extracts_dir: Path) -> None:
    """Re-LOAD DuckDB extensions listed in _remote_attach tables of each extract.duckdb.

    Called from get_analytics_db_readonly() after ATTACHing extract.duckdb files so
    that remote views (e.g. BigQuery) resolve correctly.  Uses LOAD only — no INSTALL —
    to avoid touching the network in read-only query paths.
    """
    if not extracts_dir.exists():
        return

    try:
        attached_dbs = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
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
            attached_dbs = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
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
                    extension,
                    alias,
                )
                continue
            if token_env and not is_token_env_allowed(token_env):
                logger.error(
                    "query-path remote_attach: token_env %r not in allowlist; "
                    "refusing for source %s. Override via "
                    "AGNES_REMOTE_ATTACH_TOKEN_ENVS if intended.",
                    token_env,
                    alias,
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
                            alias,
                            e,
                        )
                        continue
                    escaped = escape_sql_string_literal(bq_token)
                    secret_name = f"bq_secret_{alias}"
                    conn.execute(f"CREATE OR REPLACE SECRET {secret_name} (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
                    from connectors.bigquery.access import apply_bq_session_settings

                    apply_bq_session_settings(conn)
                    conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)")
                elif token:
                    escaped_token = escape_sql_string_literal(token)
                    conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')")
                    # Apply BQ session settings on every BQ-extension attach,
                    # not only the metadata-token branch above. Previously the
                    # token-based branch fell through without setting
                    # bq_query_timeout_ms, leaving the 90 s extension default
                    # in place and causing "remote query timeout" surprises.
                    if extension == "bigquery":
                        from connectors.bigquery.access import apply_bq_session_settings

                        apply_bq_session_settings(conn)
                else:
                    conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)")
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
        conn = _open_duckdb(str(db_path), read_only=False)
        try:
            conn.execute("SET enable_external_access = false")
        except Exception:
            pass
        # Memory cap — see get_analytics_db / the _*_MEMORY_LIMIT constants.
        _apply_memory_caps(conn, _ANALYTICS_RO_MEMORY_LIMIT, label="analytics_ro")
        return _maybe_instrument(conn, "analytics_ro")
    conn = _open_duckdb(str(db_path), read_only=True)
    # Memory cap (see get_analytics_db rationale). Read-only conns can
    # still buffer significant memory for analyst queries that hit
    # ``CREATE TEMP TABLE`` over read_parquet — capping keeps a single
    # analyst's heavy query from process-wide OOM-killing all other
    # in-flight requests.
    _apply_memory_caps(conn, _ANALYTICS_RO_MEMORY_LIMIT, label="analytics_ro")
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
    "CREATE INDEX IF NOT EXISTS idx_knowledge_item_relations_resolved ON knowledge_item_relations(resolved)",
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
    ("core.viewer", "Viewer", "Read-only access to permitted datasets.", []),
    ("core.analyst", "Analyst", "Default user role; query data, run analyses.", ["core.viewer"]),
    (
        "core.km_admin",
        "Knowledge-management admin",
        "Manages metric definitions and column metadata.",
        ["core.analyst"],
    ),
    ("core.admin", "Administrator", "Full system access; bypasses dataset_permissions.", ["core.km_admin"]),
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
    (SYSTEM_ADMIN_GROUP, "System: full access to all data and admin actions"),
    (SYSTEM_EVERYONE_GROUP, "System: default group every user is implicitly a member of"),
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
        existing = conn.execute("SELECT id, is_system FROM user_groups WHERE name = ?", [name]).fetchone()
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

        admin_group_id = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        everyone_group_id = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_EVERYONE_GROUP]
        ).fetchone()[0]

        # 2. users.groups JSON → user_group_members (google_sync). Tolerant of the
        # column having been physically dropped already (re-run safety) and of
        # malformed JSON (caught row-by-row, skipped silently).
        has_groups_col = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'groups'"
        ).fetchone()
        if has_groups_col:
            rows = conn.execute("SELECT id, groups FROM users WHERE groups IS NOT NULL").fetchall()
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
                        "SELECT id FROM user_groups WHERE name = ?",
                        [name],
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
                            "v13 backfill step 2 (google_sync): skipped insert for user=%s group=%s — already present",
                            user_id,
                            name,
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
                        group_id,
                        resource_id,
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
                    cnt,
                    role_key,
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
            "v14 migration: dropping %d orphan user_group_members rows (group_id pointed at a deleted user_groups.id)",
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
        conn.execute("ALTER TABLE user_group_members RENAME TO user_group_members_v13_pre")
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
        conn.execute("ALTER TABLE resource_grants RENAME TO resource_grants_v13_pre")
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
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [table],
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
                "id",
                "email",
                "name",
                "password_hash",
                "setup_token",
                "setup_token_created",
                "reset_token",
                "reset_token_created",
                "active",
                "deactivated_at",
                "deactivated_by",
                "created_at",
                "updated_at",
            ]
            old_users_cols = _existing_cols("users_v18_pre")
            common = [c for c in users_target_cols if c in old_users_cols]
            col_list = ", ".join(common)
            conn.execute(f"INSERT INTO users ({col_list}) SELECT {col_list} FROM users_v18_pre")
            conn.execute("DROP TABLE users_v18_pre")

        # 4: rebuild table_registry without `is_public` column.
        if "is_public" in _existing_cols("table_registry"):
            # v49: _SYSTEM_SCHEMA runs before the migration ladder and
            # creates ``data_package_tables`` with a FK pointing at
            # table_registry(id). DuckDB blocks the RENAME until the
            # dependent is dropped — drop it and recreate after the swap.
            # The v49 finalize re-establishes data_package_tables, and the
            # body of this migration's INSERT … SELECT preserves all
            # registry rows so the recreated FK won't dangle. Saved-rows
            # in data_package_tables also stay valid (they reference
            # table_registry.id which is preserved verbatim).
            data_pkg_existed = False
            pkg_rows = []
            try:
                pkg_rows = conn.execute(
                    "SELECT package_id, table_id, added_at, added_by "
                    "FROM data_package_tables"
                ).fetchall()
                data_pkg_existed = True
                conn.execute("DROP TABLE data_package_tables")
            except duckdb.Error:
                # Table doesn't exist (pre-v49 DB or hand-crafted fixture);
                # the v49 migration body will create it later.
                pass

            conn.execute("ALTER TABLE table_registry RENAME TO table_registry_v18_pre")
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
                "id",
                "name",
                "source_type",
                "bucket",
                "source_table",
                "sync_strategy",
                "query_mode",
                "sync_schedule",
                "profile_after_sync",
                "primary_key",
                "folder",
                "description",
                "registered_by",
                "registered_at",
            ]
            old_registry_cols = _existing_cols("table_registry_v18_pre")
            common = [c for c in registry_target_cols if c in old_registry_cols]
            col_list = ", ".join(common)
            conn.execute(f"INSERT INTO table_registry ({col_list}) SELECT {col_list} FROM table_registry_v18_pre")
            conn.execute("DROP TABLE table_registry_v18_pre")

            # Recreate the v49 junction + restore any rows we captured.
            if data_pkg_existed:
                conn.execute(
                    """CREATE TABLE data_package_tables (
                        package_id  VARCHAR NOT NULL REFERENCES data_packages(id),
                        table_id    VARCHAR NOT NULL REFERENCES table_registry(id),
                        added_at    TIMESTAMP DEFAULT current_timestamp,
                        added_by    VARCHAR,
                        PRIMARY KEY (package_id, table_id)
                    )"""
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_data_package_tables_table "
                    "ON data_package_tables(table_id)"
                )
                for row in pkg_rows:
                    conn.execute(
                        "INSERT INTO data_package_tables"
                        "(package_id, table_id, added_at, added_by) "
                        "VALUES (?, ?, ?, ?)",
                        list(row),
                    )

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
        existing = conn.execute("SELECT id FROM internal_roles WHERE key = ?", [key]).fetchone()
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
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'role'"
    ).fetchone()
    if not has_role_col:
        return

    rows = conn.execute("SELECT id, role FROM users WHERE role IS NOT NULL").fetchall()
    backfilled = 0
    for user_id, role_str in rows:
        role_key = _LEGACY_ROLE_TO_CORE_KEY.get(role_str, "core.viewer")
        role_row = conn.execute("SELECT id FROM internal_roles WHERE key = ?", [role_key]).fetchone()
        if not role_row:
            logger.warning(
                "v9 backfill: core role %s missing — skipping user %s",
                role_key,
                user_id,
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
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' AND table_name = 'welcome_template'"
    ).fetchone()
    if has_welcome:
        conn.execute(
            "INSERT INTO instance_templates (key, content, updated_at, updated_by) "
            "SELECT 'welcome', content, updated_at, updated_by FROM welcome_template "
            "ON CONFLICT (key) DO NOTHING"
        )
        conn.execute("DROP TABLE welcome_template")

    has_claude_md = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' AND table_name = 'claude_md_template'"
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
            "INSERT INTO instance_templates (key, content) VALUES (?, NULL) ON CONFLICT (key) DO NOTHING",
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
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS archived_by VARCHAR")

    # If the table is already at a post-v49 shape (synthetic_name column
    # present from phase-1 Flea refactor), the v35 visibility_status rebuild
    # has effectively been done long ago AND the v50 UNIQUE INDEX on
    # synthetic_name now blocks `DROP COLUMN visibility_status` (DuckDB
    # forbids dropping a column when an index references a column after it
    # positionally). Short-circuit so re-runs of the ladder on a fully
    # migrated DB (e.g. a test that resets schema_version backwards) stay
    # idempotent.
    post_v49 = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'store_entities' AND column_name = 'synthetic_name'"
    ).fetchone()
    if post_v49:
        return

    cols = {
        r[0]
        for r in conn.execute(
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
        conn.execute("ALTER TABLE store_entities ADD COLUMN _vis_v35 VARCHAR")
        conn.execute("UPDATE store_entities SET _vis_v35 = visibility_status")
        conn.execute("ALTER TABLE store_entities DROP COLUMN visibility_status")
        conn.execute("ALTER TABLE store_entities RENAME COLUMN _vis_v35 TO visibility_status")
    elif has_temp and not has_vis:
        # Partial-rebuild recovery — prior attempt dropped visibility_status
        # but the RENAME never landed. Data is already in _vis_v35 from
        # the prior UPDATE; finish the rename.
        logger.warning(
            "v34→v35 detected partial-rebuild state (visibility_status "
            "missing, _vis_v35 present); recovering via RENAME"
        )
        conn.execute("ALTER TABLE store_entities RENAME COLUMN _vis_v35 TO visibility_status")
    elif has_vis and has_temp:
        # Both present — earlier rebuild aborted before the DROP.
        # visibility_status holds the canonical values; drop the temp.
        logger.warning(
            "v34→v35 detected partial-rebuild state (both visibility_status and _vis_v35 present); dropping the temp"
        )
        conn.execute("ALTER TABLE store_entities DROP COLUMN _vis_v35")
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
def _v35_to_v36_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent v35→v36. Gates the ALTER COLUMN steps on current
    nullability — once v50 creates the UNIQUE INDEX on store_entities,
    DuckDB blocks ALTER COLUMN against the table (the index references
    a column "after" visibility_status positionally), so a redundant
    SET NOT NULL on an already-NOT-NULL column would explode."""
    conn.execute(
        "UPDATE store_entities SET visibility_status = 'pending' "
        "WHERE visibility_status IS NULL"
    )
    nullable = conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'store_entities' AND column_name = 'visibility_status'"
    ).fetchone()
    if nullable and nullable[0] == "YES":
        conn.execute(
            "ALTER TABLE store_entities ALTER COLUMN visibility_status SET NOT NULL"
        )
        conn.execute(
            "ALTER TABLE store_entities ALTER COLUMN visibility_status "
            "SET DEFAULT 'pending'"
        )
    conn.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarded BOOLEAN DEFAULT FALSE"
    )
    conn.execute("UPDATE users SET onboarded = FALSE WHERE onboarded IS NULL")


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
def _v37_to_v38_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent v37→v38. Gates the ``version_no SET NOT NULL`` step on
    current nullability — see ``_v35_to_v36_migrate`` for why."""
    # Defensive: minimal partial-state DBs from earlier migrations may
    # be missing columns the backfill UPDATE below references. Add
    # them idempotently first. Real post-v29 DBs already have these;
    # this is a no-op there. Keeps the recovery path through
    # `tests/test_db_schema_version.py::test_v32_db_with_partial_v35_recovers_through_full_ladder`
    # intact when walking from v32 fixture forward.
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS version VARCHAR")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS file_size BIGINT")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS created_at TIMESTAMP")
    # DuckDB ALTER doesn't accept "NOT NULL DEFAULT" together — split:
    # ADD nullable + DEFAULT, backfill nulls, then SET NOT NULL.
    conn.execute(
        "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS version_no INTEGER DEFAULT 1"
    )
    conn.execute("UPDATE store_entities SET version_no = 1 WHERE version_no IS NULL")
    nullable = conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'store_entities' AND column_name = 'version_no'"
    ).fetchone()
    if nullable and nullable[0] == "YES":
        conn.execute("ALTER TABLE store_entities ALTER COLUMN version_no SET NOT NULL")
    conn.execute(
        "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS version_history JSON DEFAULT '[]'"
    )
    # Backfill: synthesize a v1 entry from existing columns when the
    # history is empty. Idempotent — re-running on a populated row is
    # a no-op because the WHERE filters on empty/NULL history.
    conn.execute(
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
        """
    )


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


# v40: bq_metadata_cache table. Existing DBs get an empty table; the next
# scheduler tick (or app startup warmup) populates it. The catalog endpoint
# treats absence-of-row as `metadata_freshness: never_fetched` and returns
# NULL for the optional fields rather than failing — analyst tooling already
# tolerates NULL rows / size_bytes from the pre-0.47 contract.
_V39_TO_V40_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS bq_metadata_cache (
        table_id      VARCHAR PRIMARY KEY,
        rows          BIGINT,
        size_bytes    BIGINT,
        partition_by  VARCHAR,
        clustered_by  JSON,
        entity_type   VARCHAR,
        known_columns JSON,
        refreshed_at  TIMESTAMP,
        error_at      TIMESTAMP,
        error_msg     VARCHAR
    )
    """,
    # entity_type + known_columns may be absent on instances that picked
    # up the early v40 (`bq_metadata_cache` without these columns) before
    # the field was added. IF NOT EXISTS makes the ALTERs idempotent for
    # the fresh-create path above and additive for the upgrade path.
    "ALTER TABLE bq_metadata_cache ADD COLUMN IF NOT EXISTS entity_type VARCHAR",
    "ALTER TABLE bq_metadata_cache ADD COLUMN IF NOT EXISTS known_columns JSON",
]


def _v40_to_v41(conn: duckdb.DuckDBPyConnection) -> None:
    """v41 (was v40 pre-rebase): audit_log gains params_before (JSON), client_ip
    (VARCHAR), client_kind (VARCHAR, 'cli'|'web'|'agent'|'scheduler'|'external'),
    and correlation_id (VARCHAR, groups multi-step operations).

    Three indices added on (timestamp), (user_id, timestamp), (action, timestamp)
    to keep Activity Center timeline queries under 100ms even at 100k+ rows.

    NOTE: DuckDB does not honor DESC in CREATE INDEX; the planner is free to
    scan either direction. On a populated audit_log (~100k+ rows), each
    CREATE INDEX is single-threaded and may take 10–30s.
    """
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS timestamp TIMESTAMP DEFAULT current_timestamp")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS user_id VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS action VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS resource VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS params JSON")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS result VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS duration_ms INTEGER")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS params_before JSON")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS client_ip VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS client_kind VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS correlation_id VARCHAR")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_timestamp_desc ON audit_log(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_log(user_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action_time ON audit_log(action, timestamp)")


def _v41_to_v42(conn: duckdb.DuckDBPyConnection) -> None:
    """v42 (was v41 pre-rebase): 7 new usage_* tables for platform telemetry.

    - usage_events: per-event log (tool_use, slash_command, subagent, mcp_call)
      extracted from session JSONLs.
    - usage_session_summary: per-session aggregate keyed by session_file.
      session_id is NOT NULL — the processor always extracts a session_id from
      JSONL; orphan sessions are skipped before this row is written.
    - usage_tool_daily / usage_plugin_daily: daily rollups for fast marketplace
      queries.
    - usage_attribution_skills / _agents / _commands: skill/agent/command
      attribution exploded from plugin manifests; composite PKs allow the same
      name to appear in two different plugins.

    All CREATE TABLE/INDEX statements are IF NOT EXISTS — safe to re-run.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id                  VARCHAR PRIMARY KEY,
            session_id          VARCHAR NOT NULL,
            session_file        VARCHAR NOT NULL,
            username            VARCHAR NOT NULL,
            event_uuid          VARCHAR,
            parent_uuid         VARCHAR,
            event_type          VARCHAR NOT NULL,
            tool_name           VARCHAR,
            skill_name          VARCHAR,
            subagent_type       VARCHAR,
            command_name        VARCHAR,
            is_error            BOOLEAN DEFAULT FALSE,
            source              VARCHAR NOT NULL,
            ref_id              VARCHAR,
            model               VARCHAR,
            cwd                 VARCHAR,
            occurred_at         TIMESTAMP NOT NULL,
            processor_version   INTEGER NOT NULL,
            extracted_at        TIMESTAMP DEFAULT current_timestamp,
            friction_tags       JSON
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_session_summary (
            session_file        VARCHAR PRIMARY KEY,
            session_id          VARCHAR NOT NULL,
            username            VARCHAR NOT NULL,
            started_at          TIMESTAMP,
            ended_at            TIMESTAMP,
            active_seconds      INTEGER,
            wall_seconds        INTEGER,
            user_messages       INTEGER DEFAULT 0,
            assistant_messages  INTEGER DEFAULT 0,
            tool_calls          INTEGER DEFAULT 0,
            tool_errors         INTEGER DEFAULT 0,
            skill_invocations   INTEGER DEFAULT 0,
            subagent_dispatches INTEGER DEFAULT 0,
            mcp_calls           INTEGER DEFAULT 0,
            slash_commands      INTEGER DEFAULT 0,
            distinct_tools      INTEGER DEFAULT 0,
            distinct_skills     INTEGER DEFAULT 0,
            primary_model       VARCHAR,
            processor_version   INTEGER NOT NULL,
            extracted_at        TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_tool_daily (
            day                 DATE NOT NULL,
            tool_name           VARCHAR NOT NULL,
            source              VARCHAR NOT NULL,
            invocations         INTEGER DEFAULT 0,
            error_count         INTEGER DEFAULT 0,
            distinct_users      INTEGER DEFAULT 0,
            distinct_sessions   INTEGER DEFAULT 0,
            PRIMARY KEY (day, tool_name, source)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_plugin_daily (
            day                 DATE NOT NULL,
            source              VARCHAR NOT NULL,
            ref_id              VARCHAR NOT NULL,
            invocations         INTEGER DEFAULT 0,
            distinct_users      INTEGER DEFAULT 0,
            distinct_sessions   INTEGER DEFAULT 0,
            PRIMARY KEY (day, source, ref_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_attribution_skills (
            source       VARCHAR NOT NULL,
            ref_id       VARCHAR NOT NULL,
            skill_name   VARCHAR NOT NULL,
            PRIMARY KEY (source, ref_id, skill_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_attribution_agents (
            source       VARCHAR NOT NULL,
            ref_id       VARCHAR NOT NULL,
            agent_name   VARCHAR NOT NULL,
            PRIMARY KEY (source, ref_id, agent_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_attribution_commands (
            source       VARCHAR NOT NULL,
            ref_id       VARCHAR NOT NULL,
            command_name VARCHAR NOT NULL,
            PRIMARY KEY (source, ref_id, command_name)
        )
    """)
    # Indices — created after all tables so the batch can be re-run safely.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_user_time ON usage_events(username, occurred_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_tool ON usage_events(tool_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_skill ON usage_events(skill_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_ref ON usage_events(source, ref_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session_user ON usage_session_summary(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session_started ON usage_session_summary(started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_attr_skill_lookup ON usage_attribution_skills(skill_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_attr_agent_lookup ON usage_attribution_agents(agent_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_attr_command_lookup ON usage_attribution_commands(command_name)")


def _v42_to_v43(conn: duckdb.DuckDBPyConnection) -> None:
    """v43: user_observability_views — per-user saved filter combinations for
    the new unified /admin/activity page.

    Saved view payload (`query_json`) is the full UI state needed to reproduce
    a page render: `{window, lens, filters: {user_id, action_prefix, source,
    result_pattern}, search, sort}`. The schema is intentionally JSON not
    columns — the UI evolves faster than DB migrations.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_observability_views (
            id          VARCHAR PRIMARY KEY,
            user_id     VARCHAR NOT NULL,
            name        VARCHAR NOT NULL,
            query_json  JSON NOT NULL,
            created_at  TIMESTAMP DEFAULT current_timestamp,
            UNIQUE (user_id, name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_views_user ON user_observability_views(user_id, created_at)")


def _v43_to_v44(conn: duckdb.DuckDBPyConnection) -> None:
    """v44: homepage status frame backing columns.

    Adds ``users.last_pull_at`` (per-user manifest fetch timestamp) and
    four BIGINT token counters on ``usage_session_summary``
    (``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
    ``cache_creation_tokens``). All idempotent ALTERs — fresh installs
    receive the columns from ``_SYSTEM_SCHEMA`` and this is a no-op for
    them; upgrade path picks them up.

    Token columns default to 0; existing summary rows backfill on the
    next UsageProcessor tick because ``USAGE_PROCESSOR_VERSION`` bumps
    from 1 → 2 in the same release, which the session-pipeline
    reprocess loop uses to invalidate stale summaries.
    """
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_pull_at TIMESTAMP")
    for col in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        conn.execute(f"ALTER TABLE usage_session_summary ADD COLUMN IF NOT EXISTS {col} BIGINT DEFAULT 0")


def _v44_to_v45(conn: duckdb.DuckDBPyConnection) -> None:
    """v45: add user_id column to usage tables for stable RBAC filtering.

    The ``username`` column in ``usage_session_summary`` / ``usage_events``
    stores the directory name from the session-data path, which is either
    an email local-part (session collector) or a UUID (upload API). Email
    local-parts are unstable — they change when users rename. ``user_id``
    is the stable identity and becomes the authoritative RBAC filter
    column for the ``agnes_sessions`` / ``agnes_telemetry`` aliases.

    Backfill: the UsageProcessor populates ``user_id`` on every
    (re)process run. Existing rows get backfilled when
    ``USAGE_PROCESSOR_VERSION`` bumps, which triggers the session-pipeline
    reprocess loop.
    """
    conn.execute("ALTER TABLE usage_session_summary ADD COLUMN IF NOT EXISTS user_id VARCHAR")
    conn.execute("ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS user_id VARCHAR")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session_user_id ON usage_session_summary(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_user_id ON usage_events(user_id)")


def _v46_to_v47(conn: duckdb.DuckDBPyConnection) -> None:
    """v47: DuckDB FTS BM25 index over knowledge_items(title, content).

    Replaces ``ILIKE '%q%'`` ranking-by-insertion-order in
    ``KnowledgeRepository.search`` with BM25 relevance scoring (#121).

    The migration is *soft* — and soft against *any* exception, not just
    the ``duckdb.Error`` that ``ensure_knowledge_fts_index`` already
    handles. A non-DuckDB exception escaping from the inner helper (for
    example an ``OSError`` from an extension fetch that bypasses
    DuckDB's wrapping in a sandboxed environment, or a future DuckDB
    version surfacing a non-``Error`` subclass) would otherwise leave
    the DB stuck at v46 forever. ``KnowledgeRepository.search`` falls
    back to ILIKE on a missing index, so a soft-fail here is always
    recoverable later (boot-time lifespan rebuild + per-mutation
    ``create_fts_index(overwrite=1)`` both retry on every restart and
    every write).

    DuckDB FTS indexes are static snapshots — they don't track
    base-table mutations automatically. The lifespan in ``app/main.py``
    rebuilds once at boot as a safety net; ``create`` and title-or-
    content ``update`` in the repo rebuild on every relevant mutation
    via the same ``overwrite=1`` PRAGMA. At corpus sizes <few-thousand
    rows this is sub-100ms.
    """
    try:
        from src.fts import ensure_knowledge_fts_index
        ensure_knowledge_fts_index(conn)
    except Exception:  # noqa: BLE001 — best-effort migration, see docstring
        # Logged at the call site (``ensure_knowledge_fts_index`` already
        # WARNs on duckdb.Error); only surfaces here for non-DuckDB
        # escapes. Schema bump must proceed regardless.
        logger = logging.getLogger(__name__)
        logger.warning(
            "v47 FTS index creation raised non-duckdb exception during migration; "
            "schema bumped to 47 anyway, search will fall back to ILIKE until "
            "the next boot-time / per-mutation rebuild succeeds",
            exc_info=True,
        )


def _v45_to_v46(conn: duckdb.DuckDBPyConnection) -> None:
    """v46: per-user opt-out (dismiss) for knowledge items.

    Adds ``knowledge_item_user_dismissed`` (user_id, item_id, dismissed_at)
    with composite PK and an index on ``user_id`` to support the EXISTS
    subquery used by list_items / search / count_items / bundle to filter
    out items the caller has dismissed. Mandatory items are excluded from
    that filter at the SQL layer (``status != 'mandatory'``); the API
    further refuses POSTs against mandatory items so the row is never
    written in the first place.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT
    EXISTS`` are safe to re-run. Fresh installs receive the table via
    ``_SYSTEM_SCHEMA``; the upgrade path picks it up here.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS knowledge_item_user_dismissed (
            user_id VARCHAR NOT NULL,
            item_id VARCHAR NOT NULL,
            dismissed_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (user_id, item_id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_item_user_dismissed_user "
        "ON knowledge_item_user_dismissed(user_id)"
    )


def _v47_to_v48(conn: duckdb.DuckDBPyConnection) -> None:
    """v48: marketplace telemetry refactor.

    The v42 attribution layer (``usage_attribution_skills``, ``_agents``,
    ``_commands``) lookups on ``skill_name`` *without* the plugin prefix,
    while Claude Code writes identifiers as ``<plugin_name>:<local_name>``
    in JSONL — so the lookup never matched and every event was attributed
    to ``('builtin', None)``. The downstream ``usage_plugin_daily`` rollup
    was filtered ``WHERE source IN ('curated','flea')`` and therefore
    always empty.

    The fix: prefix-split + live lookup against ``marketplace_plugins`` /
    ``store_entities`` makes the attribution layer redundant. The new
    schema replaces all four tables with two purpose-built rollups:

    - ``usage_marketplace_item_daily``: per-day fact with count +
      per-day distinct_users + error_count, primary granularity for
      sparkline charts and incremental refresh.
    - ``usage_marketplace_item_window``: sliding-window snapshot with
      true distinct_users per window (recomputed from usage_events at
      rebuild time, can't be summed from daily distincts). Two labels
      shipped: ``last_7d`` (refreshed every tick), ``last_30d``
      (refreshed hourly).

    Drop targets verified empty / derivable on production-shape data:
    - ``usage_plugin_daily``: 0 rows (always — the attribution bug
      meant the WHERE clause never matched).
    - ``usage_attribution_*``: mapping tables, derivable from plugin
      tree on disk if ever needed again.
    """
    conn.execute("DROP TABLE IF EXISTS usage_attribution_skills")
    conn.execute("DROP TABLE IF EXISTS usage_attribution_agents")
    conn.execute("DROP TABLE IF EXISTS usage_attribution_commands")
    conn.execute("DROP TABLE IF EXISTS usage_plugin_daily")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_marketplace_item_daily (
            day            DATE    NOT NULL,
            source         VARCHAR NOT NULL,
            type           VARCHAR NOT NULL,
            parent_plugin  VARCHAR NOT NULL DEFAULT '',
            name           VARCHAR NOT NULL,
            count          INTEGER NOT NULL DEFAULT 0,
            distinct_users INTEGER NOT NULL DEFAULT 0,
            error_count    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, source, type, parent_plugin, name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_marketplace_item_window (
            period_label   VARCHAR NOT NULL,
            source         VARCHAR NOT NULL,
            type           VARCHAR NOT NULL,
            parent_plugin  VARCHAR NOT NULL DEFAULT '',
            name           VARCHAR NOT NULL,
            invocations    INTEGER NOT NULL DEFAULT 0,
            distinct_users INTEGER NOT NULL DEFAULT 0,
            refreshed_at   TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (period_label, source, type, parent_plugin, name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mid_lookup ON usage_marketplace_item_daily(source, type, parent_plugin, name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_miw_lookup ON usage_marketplace_item_window(period_label, source, type)")


def _v48_to_v49_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """v49: phase-1 Flea refactor — add ``title``, ``tagline``, ``synthetic_name``.

    Python function (not a SQL list) because the backfill needs Python-side
    humanize logic (acronym dict + Title Case) which has no clean SQL
    equivalent. Pattern mirrors ``_v34_to_v35_migrate``.

    Steps:
      1. Add columns nullable + default NULL so the ALTER works on a populated
         table.
      2. Iterate rows: compute ``title = humanize_name(strip_archive_suffix(name))``
         and ``synthetic_name = f"{name}-by-{owner_username}"``. ``tagline``
         stays NULL.
      3. SET NOT NULL on ``title`` and ``synthetic_name``. ``tagline`` stays
         nullable by design (optional short description).

    Idempotent: re-runs are safe — ADD COLUMN IF NOT EXISTS is a no-op;
    UPDATEs overwrite with the same values; ALTER ... SET NOT NULL is a
    no-op when already NOT NULL.
    """
    from src.store_naming import humanize_name, strip_archive_suffix

    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS title VARCHAR")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS tagline VARCHAR")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS synthetic_name VARCHAR")

    rows = conn.execute(
        "SELECT id, name, owner_username FROM store_entities"
    ).fetchall()
    for row_id, name, owner_username in rows:
        display_base = strip_archive_suffix(name or "")
        title = humanize_name(display_base) or display_base or "Untitled"
        synthetic = f"{name}-by-{owner_username}"
        conn.execute(
            "UPDATE store_entities SET title = ?, synthetic_name = ? WHERE id = ?",
            [title, synthetic, row_id],
        )

    # Gate ALTER … SET NOT NULL on current nullability. DuckDB blocks
    # ALTER COLUMN once an index references the table (which happens after
    # v50 creates the UNIQUE INDEX on synthetic_name), so an unconditional
    # re-run on a fully-migrated DB would explode. Idempotent path: skip
    # the ALTER when the column is already NOT NULL.
    nullable = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'store_entities' "
            "AND column_name IN ('title', 'synthetic_name')"
        ).fetchall()
    }
    if nullable.get("title") == "YES":
        conn.execute("ALTER TABLE store_entities ALTER COLUMN title SET NOT NULL")
    if nullable.get("synthetic_name") == "YES":
        conn.execute("ALTER TABLE store_entities ALTER COLUMN synthetic_name SET NOT NULL")


def _v49_to_v50_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """v50: enforce DB-level uniqueness on ``store_entities.synthetic_name``.

    v49 introduced the column as NOT NULL but without uniqueness. App-level
    ``_suffixed_already_taken`` only fires at upload/rename; any other write
    path (admin DB hand-fix, future migration drift) could silently insert a
    duplicate, and ``WHERE synthetic_name = ?`` would then non-deterministically
    return one of the matching rows. With ``synthetic_name`` now the canonical
    attribution key (rollup tables, marketplace bundle naming, JSONL invocation
    prefix), uniqueness must be enforced at the DB level.

    DuckDB has no ``ALTER TABLE ADD CONSTRAINT UNIQUE`` for existing tables,
    but ``CREATE UNIQUE INDEX`` is functionally equivalent (rejects duplicate
    inserts). The archive rewrite path
    (``StoreEntitiesRepository.archive``) renames synthetic_name alongside
    name, so archived rows cannot collide with live ones — a full-table
    UNIQUE index is correct.

    Steps:
      1. Pre-flight: scan for existing duplicates. If any are found, abort
         with ``RuntimeError`` listing them — the index creation would fail
         anyway, but a structured error gives the operator a clear diagnostic
         instead of a raw DuckDB constraint-violation message.
      2. Create the UNIQUE index (idempotent via IF NOT EXISTS).

    Idempotent: a re-run finds the index already present and skips both
    the duplicate scan (which would still pass) and the CREATE.
    """
    # Pre-flight duplicate detection. List the actual conflicting slugs +
    # row counts so the operator can resolve manually (typically by
    # archiving one of the colliding rows, which rewrites its
    # synthetic_name to the __archived__<epoch>-suffixed form).
    dupes = conn.execute(
        """SELECT synthetic_name, COUNT(*) AS n
             FROM store_entities
            GROUP BY synthetic_name
           HAVING COUNT(*) > 1
            ORDER BY n DESC, synthetic_name"""
    ).fetchall()
    if dupes:
        summary = ", ".join(f"{name!r} x{n}" for name, n in dupes)
        raise RuntimeError(
            "v49→v50 migration blocked: duplicate synthetic_name values "
            f"present in store_entities ({summary}). Resolve manually "
            "(archive or rename the colliding rows) and re-run."
        )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_store_entities_synthetic_name "
        "ON store_entities(synthetic_name)"
    )
def _v51_to_v52(conn: duckdb.DuckDBPyConnection) -> None:
    """v49: unified stack for Data Packages + Memory.

    Single migration entry point for the v49 cutover. See
    ``docs/brainstorms/2026-05-15-unified-stack-design.md`` section 8.1
    for the full step list. Idempotent (``ALTER ... ADD COLUMN IF NOT
    EXISTS``, ``CREATE TABLE IF NOT EXISTS``) so re-running is safe.

    Steps 6 + 9b (junction populate + recreate) are conditional on the
    legacy ``knowledge_items.domain`` column actually existing — fresh
    installs come through ``_SYSTEM_SCHEMA`` which already creates the
    post-v49 table shape (no ``domain`` column, ``knowledge_item_domains``
    junction in place), so those steps no-op.
    """
    has_legacy_domain_col = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'knowledge_items' AND column_name = 'domain'"
    ).fetchone() is not None

    # 1) resource_grants.requirement — per-group 'available' | 'required'
    # enum. Default 'available' preserves pre-v49 semantics. Required-tier
    # applies to data_package / memory_domain / memory_item grants;
    # marketplace_plugin Required-tier stays on
    # marketplace_plugins.is_system per D1.
    conn.execute(
        "ALTER TABLE resource_grants "
        "ADD COLUMN IF NOT EXISTS requirement VARCHAR DEFAULT 'available'"
    )

    # 2) knowledge_items.is_required — splits the v15-era status='mandatory'
    # overload into an orthogonal boolean. Items can now be 'approved' and
    # also Required (governance tier), or 'pending' without affecting the
    # Required state. Existing 'mandatory' rows migrate to
    # is_required=TRUE, status='approved'.
    conn.execute(
        "ALTER TABLE knowledge_items "
        "ADD COLUMN IF NOT EXISTS is_required BOOLEAN DEFAULT FALSE"
    )
    # Skip the backfill UPDATE on hand-crafted v1 fixtures where
    # ``status`` was never added — the ladder upgrade from v1 reaches v15
    # before v49 anyway, but the migration body sees the pre-v15 shape
    # only on those test paths. Guard so the migration stays no-op-safe.
    has_status = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'knowledge_items' AND column_name = 'status'"
    ).fetchone()
    if has_status:
        conn.execute(
            "UPDATE knowledge_items "
            "   SET is_required = TRUE, status = 'approved' "
            " WHERE status = 'mandatory'"
        )

    # 3) Data Packages — admin-curated bundles of tables. A package is a
    # browse / add-to-stack unit; the tables it contains flow into the
    # caller's effective table set via DATA_PACKAGE grants. See spec
    # section 3.3.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS data_packages (
            id              VARCHAR PRIMARY KEY,
            slug            VARCHAR UNIQUE NOT NULL,
            name            VARCHAR NOT NULL,
            description     TEXT,
            icon            VARCHAR,
            color           VARCHAR,
            cover_image_url VARCHAR,
            created_by      VARCHAR,
            created_at      TIMESTAMP DEFAULT current_timestamp,
            updated_at      TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS data_package_tables (
            package_id  VARCHAR NOT NULL REFERENCES data_packages(id),
            table_id    VARCHAR NOT NULL REFERENCES table_registry(id),
            added_at    TIMESTAMP DEFAULT current_timestamp,
            added_by    VARCHAR,
            PRIMARY KEY (package_id, table_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_data_package_tables_table "
        "ON data_package_tables(table_id)"
    )

    # 4) Memory Domains — first-class entities replacing the scalar
    # ``knowledge_items.domain`` string. The ``memory_domains`` parent
    # table is created here; ``knowledge_item_domains`` is deferred to
    # after the legacy column is dropped (step 9) because DuckDB blocks
    # ``ALTER TABLE knowledge_items DROP COLUMN`` while a child table
    # holds an FK reference. See spec section 3.4.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_domains (
            id              VARCHAR PRIMARY KEY,
            slug            VARCHAR UNIQUE NOT NULL,
            name            VARCHAR NOT NULL,
            description     TEXT,
            icon            VARCHAR,
            color           VARCHAR,
            cover_image_url VARCHAR,
            created_by      VARCHAR,
            created_at      TIMESTAMP DEFAULT current_timestamp,
            updated_at      TIMESTAMP DEFAULT current_timestamp
        )
        """
    )

    # 5) Seed canonical memory_domains from the legacy ``VALID_DOMAINS``
    # constant in ``app/api/memory.py`` (the v15 hardcoded six). Deterministic
    # ``md_<slug>`` ids so downstream Phase-2+ refactors can rely on the
    # naming convention without re-querying. Icons / colors are part of the
    # canonical seed so the Browse UI renders consistently across instances.
    canonical_domains = [
        ("md_finance",        "finance",        "Finance",        "💰", "#dcfce7"),
        ("md_engineering",    "engineering",    "Engineering",    "⚙️", "#dbeafe"),
        ("md_product",        "product",        "Product",        "📦", "#fef3c7"),
        ("md_data",           "data",           "Data",           "📊", "#f3e8ff"),
        ("md_operations",     "operations",     "Operations",     "🔧", "#fff7ed"),
        ("md_infrastructure", "infrastructure", "Infrastructure", "🏗️", "#fef2f2"),
    ]
    for did, slug, name, icon, color in canonical_domains:
        conn.execute(
            "INSERT INTO memory_domains (id, slug, name, icon, color, created_at) "
            "VALUES (?, ?, ?, ?, ?, current_timestamp) "
            "ON CONFLICT (slug) DO NOTHING",
            [did, slug, name, icon, color],
        )

    # Plus one row per non-canonical ``knowledge_items.domain`` value found
    # in the existing data (defensive — instances may have hand-set domains
    # outside the six). Slug normalization mirrors the junction populate
    # query below so the join in task 1.6 matches deterministically. Only
    # runs on an upgrade path where the legacy column still exists; fresh
    # installs skip this since ``_SYSTEM_SCHEMA`` ships the post-v49 shape.
    if has_legacy_domain_col:
        conn.execute(
            """
            INSERT INTO memory_domains(id, slug, name, created_at)
            SELECT
                'md_' || lower(regexp_replace(domain, '[^a-z0-9]+', '_', 'g')),
                lower(regexp_replace(domain, '[^a-z0-9]+', '-', 'g')),
                domain,
                current_timestamp
              FROM (SELECT DISTINCT domain FROM knowledge_items
                     WHERE domain IS NOT NULL AND domain <> ''
                       AND domain NOT IN ('finance','engineering','product','data','operations','infrastructure'))
            ON CONFLICT (slug) DO NOTHING
            """
        )

    # 6) Stash the legacy (item_id, domain_id) pairs in a temporary table
    # so we can recreate the relation after dropping the scalar column.
    # DuckDB blocks the DROP COLUMN as long as a child table FK-references
    # ``knowledge_items``, so the junction itself is created in step 9b
    # below — after the column is gone. Skipped on fresh installs where
    # the legacy column has never existed.
    if has_legacy_domain_col:
        conn.execute("DROP TABLE IF EXISTS _v49_item_domain_pairs")
        conn.execute(
            """
            CREATE TEMP TABLE _v49_item_domain_pairs AS
            SELECT ki.id AS item_id, md.id AS domain_id
              FROM knowledge_items ki
              JOIN memory_domains  md
                ON md.slug = lower(regexp_replace(ki.domain, '[^a-z0-9]+', '-', 'g'))
             WHERE ki.domain IS NOT NULL AND ki.domain <> ''
            """
        )

    # 7) Re-point ``MEMORY_DOMAIN`` grants — pre-v49 stored the domain slug
    # directly in ``resource_grants.resource_id``; v49+ stores
    # ``memory_domains.id``. Orphan grants (resource_id is a slug with no
    # matching ``memory_domains`` row) are left untouched per spec D14 so
    # an admin can decide whether to delete or re-create the domain.
    conn.execute(
        """
        UPDATE resource_grants
           SET resource_id = (
               SELECT id FROM memory_domains
                WHERE memory_domains.slug = resource_grants.resource_id
           )
         WHERE resource_type = 'memory_domain'
           AND EXISTS (
               SELECT 1 FROM memory_domains
                WHERE memory_domains.slug = resource_grants.resource_id
           )
        """
    )

    # 8) ``user_stack_subscriptions`` — generic per-user opt-in for
    # ``data_package`` and ``memory_domain`` grants flagged
    # ``requirement='available'``. Composite PK (user_id, resource_type,
    # resource_id) makes the insert idempotent. Marketplace pluginy stay
    # on the existing ``user_plugin_optouts`` opt-out shape per D1.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_stack_subscriptions (
            user_id       VARCHAR NOT NULL,
            resource_type VARCHAR NOT NULL,
            resource_id   VARCHAR NOT NULL,
            subscribed_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (user_id, resource_type, resource_id)
        )
        """
    )

    # 9a) Drop the legacy ``knowledge_items.domain`` scalar. Single-PR
    # cutover per D14 — the temp-stashed pairs from step 6 + memory_domains
    # seeded in step 5 are the new sources of truth.
    #
    # DuckDB blocks ``ALTER TABLE … DROP COLUMN`` while any FK references
    # the same table (DependencyException). On the fresh-install +
    # upgrade-from-v1 paths, ``_SYSTEM_SCHEMA`` runs BEFORE the migration
    # ladder and creates ``knowledge_item_domains`` with the FK already
    # in place. We DROP that dependent (stashing its rows), perform the
    # column drop, and recreate the junction immediately after.
    junction_existed = False
    stashed_rows: list = []
    try:
        stashed_rows = conn.execute(
            "SELECT item_id, domain_id, added_at, added_by "
            "FROM knowledge_item_domains"
        ).fetchall()
        junction_existed = True
        conn.execute("DROP TABLE knowledge_item_domains")
    except duckdb.Error:
        # Junction didn't exist (genuine v48 upgrade path); fall through.
        pass

    conn.execute("ALTER TABLE knowledge_items DROP COLUMN IF EXISTS domain")

    # 9b) (Re)create the M:N junction and replay any rows we stashed.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_item_domains (
            item_id   VARCHAR NOT NULL REFERENCES knowledge_items(id),
            domain_id VARCHAR NOT NULL REFERENCES memory_domains(id),
            added_at  TIMESTAMP DEFAULT current_timestamp,
            added_by  VARCHAR,
            PRIMARY KEY (item_id, domain_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_item_domains_domain "
        "ON knowledge_item_domains(domain_id)"
    )
    if junction_existed and stashed_rows:
        for row in stashed_rows:
            conn.execute(
                "INSERT INTO knowledge_item_domains"
                "(item_id, domain_id, added_at, added_by) "
                "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                list(row),
            )
    # Replay the stashed pairs. The temp table exists only when the
    # upgrade path ran step 6 — fresh installs skip both step 6 and
    # this replay since ``_v49_item_domain_pairs`` was never created.
    has_pairs = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = '_v49_item_domain_pairs'"
    ).fetchone()
    if has_pairs:
        conn.execute(
            """
            INSERT INTO knowledge_item_domains(item_id, domain_id, added_at)
            SELECT item_id, domain_id, current_timestamp
              FROM _v49_item_domain_pairs
            ON CONFLICT DO NOTHING
            """
        )
        conn.execute("DROP TABLE _v49_item_domain_pairs")

    # 10) bump schema_version row. Matches the pattern used by every
    # prior in-function migration (e.g. _v30_to_v31_migrate, _v34_to_v35_migrate)
    # — the per-step migrations declared as SQL lists rely on the
    # outer ``UPDATE schema_version`` at the end of ``_ensure_schema``,
    # but the ladder-internal function pattern keeps the bump local so a
    # mid-ladder failure doesn't leave the version stale.
    conn.execute("UPDATE schema_version SET version = 52")


_V50_TO_V51_MIGRATIONS = [
    # ``bq_fqn`` carries the fully-qualified BigQuery path
    # (``project.dataset.table``) for a registered remote table when set,
    # so the orchestrator's rebuild path no longer has to reconstruct it
    # from the globally-attached ``_remote_attach`` project + the dual-
    # purpose ``bucket`` field (which is also a UX/RBAC label).
    # Nullable for backwards compat — rows without it keep using the
    # legacy ``<remote_attach.project>.<bucket>.<source_table>`` fallback.
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS bq_fqn VARCHAR",
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
            f'DuckDB-flavor `bq."ds"."tbl"` to BQ-native '
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
                    "v24 migration: rewrote source_query for row %r",
                    row_id,
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _v59_to_v60(conn: duckdb.DuckDBPyConnection) -> None:
    """Backfill ``usage_events.username`` / ``usage_session_summary.username``
    from ``users.email`` where the row has a resolved ``user_id``.

    Pre-v60 the ``username`` column was written by three writers with
    conflicting semantics:

    * REST event emitters (``sync.py``, ``stack.py``, ``memory.py``,
      ``web/router.py``) → full email (``user.get('email')``) or
      ``user['id']`` UUID when email empty.
    * Session pipeline via ``/data/user_sessions/<dir>/`` → directory
      name. From the session collector the directory is the OS
      username (typically the email local-part); from the upload API
      it is the user's UUID.

    Result: a single user surfaces in the admin telemetry dropdown
    under up to three different ``username`` values. The runner now
    normalises new writes to ``users.email``; this migration cleans up
    the historical rows so the dropdown is one row per user
    immediately.

    Only rows with a non-null ``user_id`` are touched — orphaned
    sessions (deleted users, never-matched directories) keep whatever
    label they had so the data isn't silently lost.
    """
    # Skip backfill on stub schemas (e.g. the v1→vN end-to-end test
    # seeds ``users`` with only an ``id`` column). The required
    # ``users.email`` plus ``usage_*.username`` / ``usage_*.user_id``
    # columns all come from earlier migrations on every real install;
    # if any of them is missing here, this is a synthetic fixture.
    def _cols(table: str) -> set[str]:
        return {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE lower(table_name) = lower(?)",
                [table],
            ).fetchall()
        }

    users_cols = _cols("users")
    if "email" not in users_cols:
        conn.execute("UPDATE schema_version SET version = 60")
        return

    for table, key_col in (
        ("usage_events", "user_id"),
        ("usage_session_summary", "user_id"),
    ):
        tcols = _cols(table)
        if {"username", key_col} - tcols:
            continue
        conn.execute(
            f"""
            UPDATE {table}
               SET username = u.email
              FROM users u
             WHERE {table}.{key_col} = u.id
               AND u.email IS NOT NULL
               AND u.email != ''
               AND {table}.username IS DISTINCT FROM u.email
            """
        )
    conn.execute("UPDATE schema_version SET version = 60")


def _v58_to_v59(conn: duckdb.DuckDBPyConnection) -> None:
    """v56: extended-content columns on ``data_packages`` + structured
    per-table doc columns on ``table_registry``.

    Backs the ``/catalog/p/<slug>`` rewrite per the extended-descriptions
    admin spec — owner attribution, curated tags,
    long-form description, use/skip arrays, package-level example
    questions on the package side; grain / platforms / partition /
    history / gotchas on the per-table side.

    All ALTERs are ADD COLUMN IF NOT EXISTS — idempotent + safe to
    re-run.
    """
    for col_sql in (
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS owner_name VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS owner_team VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS tags VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS long_description TEXT",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS when_to_use VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS when_not_to_use VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS example_questions VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS grain VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS platforms VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS partition_col VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS history VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS gotchas VARCHAR",
    ):
        conn.execute(col_sql)
    conn.execute("UPDATE schema_version SET version = 59")


def _v60_to_v61(conn: duckdb.DuckDBPyConnection) -> None:
    """v61: ``cli_auth_codes`` table — single-use exchange codes for the
    browser-loopback ``agnes auth login`` flow.

    Idempotent CREATE TABLE IF NOT EXISTS. Fresh installs already get the
    table from ``_SYSTEM_SCHEMA``; this migration covers the sequential
    upgrade path from a v60 instance.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cli_auth_codes (
            code_hash   VARCHAR PRIMARY KEY,
            user_id     VARCHAR NOT NULL,
            email       VARCHAR NOT NULL,
            created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            expires_at  TIMESTAMP NOT NULL,
            consumed_at TIMESTAMP
        )
    """)
    conn.execute("UPDATE schema_version SET version = 61")


def _v61_to_v62(conn: duckdb.DuckDBPyConnection) -> None:
    """v62: ``setup_tokens`` table for the Agnes Cowork one-click setup flow.

    Short-lived tokens (24 h) generated by ``POST /api/user/cowork-bundle``
    and consumed once by ``POST /api/auth/exchange-setup-token``, which mints
    a regular PAT without requiring the analyst to log in interactively.

    CREATE TABLE IF NOT EXISTS is idempotent — safe on fresh installs where
    the table already exists courtesy of ``_SYSTEM_SCHEMA``.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS setup_tokens (
            id          VARCHAR PRIMARY KEY,
            user_id     VARCHAR NOT NULL,
            token_hash  VARCHAR NOT NULL,
            expires_at  TIMESTAMP NOT NULL,
            used_at     TIMESTAMP,
            created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 62")


def _v62_to_v63(conn: duckdb.DuckDBPyConnection) -> None:
    """v63: Universal MCP — ``mcp_sources``, ``tool_registry``, ``tool_grants``.

    Tables for the inbound MCP connector (RFC #461). A row in ``mcp_sources``
    describes an external MCP server we ingest from (stdio command or
    HTTP/SSE URL). Each curated tool from that source becomes one row in
    ``tool_registry`` with a ``mode`` of ``materialize`` (scheduled extract
    into a parquet → analytics.duckdb table) or ``passthrough`` (live call
    forwarded to the upstream MCP at query time). ``tool_grants`` is the
    per-group ACL for passthrough tools, parallel to ``resource_grants``.

    CREATE TABLE IF NOT EXISTS is idempotent — safe on fresh installs.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_sources (
            id            VARCHAR PRIMARY KEY,
            name          VARCHAR NOT NULL UNIQUE,
            transport     VARCHAR NOT NULL,       -- stdio | http | sse
            command       VARCHAR,                -- stdio: executable path
            args          JSON,                   -- stdio: arg array
            url           VARCHAR,                -- http/sse: endpoint URL
            auth_method   VARCHAR,                -- none | bearer | basic
            auth_secret_env VARCHAR,              -- name of env var holding the secret (POC: no vault yet)
            enabled       BOOLEAN NOT NULL DEFAULT true,
            created_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at    TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_registry (
            tool_id          VARCHAR PRIMARY KEY,        -- "<source_name>.<exposed_name>"
            source_id        VARCHAR NOT NULL,
            original_name    VARCHAR NOT NULL,
            exposed_name     VARCHAR NOT NULL,
            mode             VARCHAR NOT NULL,           -- materialize | passthrough
            table_id         VARCHAR,                    -- FK to table_registry (materialize mode only)
            input_schema     JSON,                       -- MCP inputSchema
            description      VARCHAR,
            mutating         BOOLEAN NOT NULL DEFAULT false,
            pii_fields       JSON,                       -- array of column names to redact on output
            rate_limit_pm    INTEGER,                    -- per-minute, per-user (NULL = unlimited)
            schedule         VARCHAR,                    -- materialize only, e.g. 'every 6h'
            enabled          BOOLEAN NOT NULL DEFAULT true,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_grants (
            tool_id   VARCHAR NOT NULL,
            group_id  VARCHAR NOT NULL,
            PRIMARY KEY (tool_id, group_id)
        )
    """)
    conn.execute("UPDATE schema_version SET version = 63")



def _v62_to_v63(conn: duckdb.DuckDBPyConnection) -> None:
    """v63: ``mcp_secrets`` table — server-wide vault for MCP source auth.

    RFC #461 §4. One row per ``mcp_sources.id`` holds the Fernet-
    ciphertext of the upstream auth token. Replaces the legacy
    ``mcp_sources.auth_secret_env`` env-var pattern for HTTP/SSE
    sources — connectors/mcp/client.py first consults this table, then
    falls back to the env-var path so old registrations keep working.

    Per-user secrets (analyst-scoped OAuth tokens for upstream MCP) land
    in a follow-up v64 migration as ``mcp_user_secrets``.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_secrets (
            source_id        VARCHAR PRIMARY KEY,
            secret_value_enc BLOB NOT NULL,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 63")


def _v63_to_v64(conn: duckdb.DuckDBPyConnection) -> None:
    """v64: ``mcp_secrets`` table — server-wide vault for MCP source auth.

    RFC #461 §4. One row per ``mcp_sources.id`` holds the Fernet-
    ciphertext of the upstream auth token. Replaces the legacy
    ``mcp_sources.auth_secret_env`` env-var pattern for HTTP/SSE
    sources — connectors/mcp/client.py first consults this table, then
    falls back to the env-var path so old registrations keep working.

    Per-user secrets (analyst-scoped OAuth tokens for upstream MCP) land
    in a follow-up migration as ``mcp_user_secrets``.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_secrets (
            source_id        VARCHAR PRIMARY KEY,
            secret_value_enc BLOB NOT NULL,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 64")


def _v64_to_v65(conn: duckdb.DuckDBPyConnection) -> None:
    """v65: per-user MCP source secrets + ``scope`` column on ``mcp_sources``.

    RFC #461 §4 phase B. ``mcp_user_secrets(source_id, user_id, ...)``
    holds each analyst's own credential (their Notion/Slack/Linear OAuth
    token) for upstream MCP servers that authenticate per-caller. The
    new ``mcp_sources.scope`` column selects which lookup path
    ``connectors/mcp/client._lookup_secret_for_source`` follows:

      ``shared``    — default; use mcp_secrets (or auth_secret_env env var).
                      Materialize scheduled jobs always use this scope —
                      they don't have a calling user.
      ``per_user``  — REST invoke endpoint threads the caller's id;
                      look up mcp_user_secrets(source_id, user_id).
                      Falls through to shared if the analyst hasn't
                      stored their own credential yet (so the path stays
                      forgiving while operators bootstrap).

    ``CREATE TABLE IF NOT EXISTS`` + ``ADD COLUMN IF NOT EXISTS`` keep
    the migration idempotent on fresh and upgrade paths.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_user_secrets (
            source_id        VARCHAR NOT NULL,
            user_id          VARCHAR NOT NULL,
            secret_value_enc BLOB NOT NULL,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            PRIMARY KEY (source_id, user_id)
        )
    """)
    conn.execute(
        "ALTER TABLE mcp_sources ADD COLUMN IF NOT EXISTS scope VARCHAR DEFAULT 'shared'"
    )
    conn.execute("UPDATE schema_version SET version = 65")


def _v57_to_v58(conn: duckdb.DuckDBPyConnection) -> None:
    """v55: ``memory_domain_suggestions`` table — non-admin "Suggest a
    domain" affordance + admin moderation queue.

    Idempotent CREATE TABLE IF NOT EXISTS. Fresh installs already get
    the table from ``_SYSTEM_SCHEMA``; this migration covers the
    sequential-upgrade path from a v54 instance.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_domain_suggestions (
            id                VARCHAR PRIMARY KEY,
            name              VARCHAR NOT NULL,
            description       TEXT,
            rationale         TEXT,
            status            VARCHAR DEFAULT 'pending',
            created_by        VARCHAR,
            created_at        TIMESTAMP DEFAULT current_timestamp,
            resolved_at       TIMESTAMP,
            resolved_by       VARCHAR,
            resolution_note   TEXT,
            created_domain_id VARCHAR
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_domain_suggestions_status "
        "ON memory_domain_suggestions(status)"
    )
    conn.execute("UPDATE schema_version SET version = 58")


def _v56_to_v57(conn: duckdb.DuckDBPyConnection) -> None:
    """v54: soft-delete columns on data_packages / memory_domains / recipes.

    Powers the "Deleted. Undo (10s)" toast on admin pages — DELETE sets
    ``deleted_at`` instead of nuking the row, so the junction rows
    (data_package_tables, knowledge_item_domains) + any resource_grants
    referencing the resource id survive intact. The list/get endpoints
    filter ``deleted_at IS NULL`` so users never see soft-deleted rows.

    Idempotent ADD COLUMN IF NOT EXISTS.
    """
    for table in ("data_packages", "memory_domains", "recipes"):
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP"
        )
    conn.execute("UPDATE schema_version SET version = 57")


def _v55_to_v56(conn: duckdb.DuckDBPyConnection) -> None:
    """v53: ``recipes`` table — admin-curated query templates surfaced as
    a second tab on /catalog.

    Idempotent CREATE TABLE IF NOT EXISTS. Fresh installs already get
    the table from ``_SYSTEM_SCHEMA``; this migration covers the
    sequential-upgrade path from a v52 instance.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id                VARCHAR PRIMARY KEY,
            slug              VARCHAR UNIQUE NOT NULL,
            title             VARCHAR NOT NULL,
            description       TEXT,
            icon              VARCHAR,
            color             VARCHAR,
            sql_template      TEXT,
            related_table_ids JSON,
            status            VARCHAR DEFAULT 'prod',
            created_by        VARCHAR,
            created_at        TIMESTAMP DEFAULT current_timestamp,
            updated_at        TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 56")


def _v54_to_v55(conn: duckdb.DuckDBPyConnection) -> None:
    """v52: per-table docs columns on table_registry.

    Adds three admin-authored fields read by the new /catalog/t/<id>
    detail page: sample_questions (JSON array of strings),
    things_to_know (freeform text), pairs_well_with (JSON array of
    table_registry ids). All optional / NULL on legacy rows.

    Idempotent ADD COLUMN IF NOT EXISTS; safe to re-run.
    """
    conn.execute(
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS sample_questions JSON"
    )
    conn.execute(
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS things_to_know TEXT"
    )
    conn.execute(
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS pairs_well_with JSON"
    )
    conn.execute("UPDATE schema_version SET version = 55")


def _v53_to_v54(conn: duckdb.DuckDBPyConnection) -> None:
    """v51: lifecycle ``status`` + per-package ``category`` columns.

    Adds the surfaces the /catalog mockup audit identified as gaps:
    a small status pill on each card (driven by hero filter checkboxes)
    and an eyebrow line above the title (data_packages only — memory
    domains don't need a second-level category since the domain itself
    classifies its items).

    All ADD COLUMN IF NOT EXISTS — idempotent re-run is safe. The fresh
    install path picks the columns up from _SYSTEM_SCHEMA directly; this
    migration covers the sequential-upgrade path off an earlier version.
    """
    conn.execute(
        "ALTER TABLE data_packages "
        "ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'prod'"
    )
    conn.execute(
        "ALTER TABLE data_packages "
        "ADD COLUMN IF NOT EXISTS category VARCHAR"
    )
    conn.execute(
        "ALTER TABLE memory_domains "
        "ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'prod'"
    )
    conn.execute("UPDATE schema_version SET version = 54")


def _v52_to_v53(conn: duckdb.DuckDBPyConnection) -> None:
    """v50: ``cover_image_url`` on ``data_packages`` + ``memory_domains``.

    Closes the visual gap with /marketplace cards: marketplace items render
    real JPGs/PNGs from ``cover_photo_url`` while /catalog + /memory have
    been stuck with 2-letter initials. The upload endpoint at
    ``POST /api/admin/uploads/cover-image`` returns a relative URL that
    callers stash here; cards render ``<img>`` when set, fall back to the
    initials banner when NULL.

    Idempotent (``ADD COLUMN IF NOT EXISTS``) — re-running is safe. Bumps
    the version row locally so the fresh-install path (which calls every
    migration in sequence and relies on each step to stamp its own number
    — see _v51_to_v52 step 10) lands at 50 even if a future step in the
    same ladder fails before the outer driver gets to its UPDATE.
    """
    conn.execute(
        "ALTER TABLE data_packages "
        "ADD COLUMN IF NOT EXISTS cover_image_url VARCHAR"
    )
    conn.execute(
        "ALTER TABLE memory_domains "
        "ADD COLUMN IF NOT EXISTS cover_image_url VARCHAR"
    )
    conn.execute("UPDATE schema_version SET version = 53")


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
            conn.execute("INSERT INTO setup_banner (id, content) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING")
            # v26 instance_templates seed — three canonical keys with NULL
            # content (operator override absent → render OSS default).
            for key in ("welcome", "claude_md", "home"):
                conn.execute(
                    "INSERT INTO instance_templates (key, content) VALUES (?, NULL) ON CONFLICT (key) DO NOTHING",
                    [key],
                )
            # v41 audit_log indices: _SYSTEM_SCHEMA omits CREATE INDEX to
            # avoid failures when pre-existing audit_log lacks timestamp
            # (migration tests). Create them here for fresh installs; the
            # upgrade path uses _v40_to_v41 below.
            _v40_to_v41(conn)
            # v42 usage_* tables + indices. _SYSTEM_SCHEMA already creates
            # them via IF NOT EXISTS, so this is a safe no-op for fresh
            # installs; mirrors the established pattern for the upgrade
            # path below.
            _v41_to_v42(conn)
            # v43 user_observability_views — saved-views for /admin/activity.
            _v42_to_v43(conn)
            # v44 homepage-stats columns. _SYSTEM_SCHEMA already declares
            # them on fresh installs (no-op ALTERs); kept here for the
            # ladder's chronological readability.
            _v43_to_v44(conn)
            # v45 user_id column on usage tables. _SYSTEM_SCHEMA declares
            # the columns for fresh installs; migration adds them for
            # existing DBs. No-op on fresh.
            _v44_to_v45(conn)
            # v46 knowledge_item_user_dismissed — per-user opt-out for
            # curated memory items. _SYSTEM_SCHEMA already creates the
            # table on fresh installs; this call is a no-op there.
            _v45_to_v46(conn)
            # v47 fts index over knowledge_items — best-effort, silent
            # fallback to ILIKE search if the fts extension can't load.
            _v46_to_v47(conn)
            # v48 marketplace telemetry refactor — drops 4 legacy tables
            # and creates 2 new rollups. _SYSTEM_SCHEMA already creates
            # the new tables on fresh installs; the DROPs are no-ops
            # there because the legacy tables aren't in _SYSTEM_SCHEMA
            # anymore. Kept here for ladder readability.
            _v47_to_v48(conn)
            # v49 phase-1 Flea refactor — title, tagline, synthetic_name
            # columns. _SYSTEM_SCHEMA already declares them on fresh
            # installs; this call is a no-op (table empty, ALTER IF NOT
            # EXISTS, no rows to backfill, SET NOT NULL idempotent).
            _v48_to_v49_migrate(conn)
            # v50 UNIQUE INDEX on synthetic_name. _SYSTEM_SCHEMA already
            # creates the index on fresh installs; this call is a no-op
            # (table empty so no duplicates possible, CREATE UNIQUE
            # INDEX IF NOT EXISTS is idempotent).
            _v49_to_v50_migrate(conn)
            # v49 unified stack — Data Packages + Memory Domains junction +
            # requirement enum + is_required + user_stack_subscriptions.
            # _SYSTEM_SCHEMA already creates the new tables on fresh
            # installs; the migration body is idempotent (CREATE TABLE
            # IF NOT EXISTS / ALTER ... ADD COLUMN IF NOT EXISTS), so
            # this call no-ops apart from seeding canonical
            # memory_domains and bumping the version row.
            _v51_to_v52(conn)
            # v50 cover_image_url on data_packages + memory_domains.
            # _SYSTEM_SCHEMA already includes the column on fresh installs;
            # the migration's IF NOT EXISTS ALTERs no-op there.
            _v52_to_v53(conn)
            # v51 status + category on data_packages, status on
            # memory_domains. Same fresh-install no-op pattern.
            _v53_to_v54(conn)
            # v52 per-table docs columns on table_registry.
            _v54_to_v55(conn)
            # v53 recipes table.
            _v55_to_v56(conn)
            # v54 deleted_at columns on data_packages, memory_domains, recipes.
            _v56_to_v57(conn)
            # v55 memory_domain_suggestions table.
            _v57_to_v58(conn)
            # v56 extended content columns on data_packages + structured
            # per-table doc columns on table_registry.
            _v58_to_v59(conn)
            # v59→v60 backfills ``username`` in usage_events /
            # usage_session_summary from users.email. Fresh installs
            # have empty usage_* tables, so the UPDATE is a no-op.
            _v59_to_v60(conn)
            # v60→v61 creates the ``cli_auth_codes`` table (browser-
            # loopback login exchange codes).
            _v60_to_v61(conn)
            # v61→v62: setup_tokens table for Agnes Cowork one-click setup.
            _v61_to_v62(conn)
            # v62→v63: Universal MCP — mcp_sources, tool_registry, tool_grants.
            _v62_to_v63(conn)
            # v63→v64: mcp_secrets — shared vault for MCP source auth.
            _v63_to_v64(conn)
            # v64→v65: per-user MCP secrets + scope column on mcp_sources.
            _v64_to_v65(conn)
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
                    "SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'role'"
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
                _v35_to_v36_migrate(conn)
            if current < 37:
                for sql in _V36_TO_V37_MIGRATIONS:
                    conn.execute(sql)
            if current < 38:
                _v37_to_v38_migrate(conn)
            if current < 39:
                for sql in _V38_TO_V39_MIGRATIONS:
                    conn.execute(sql)
            if current < 40:
                for sql in _V39_TO_V40_MIGRATIONS:
                    conn.execute(sql)
            if current < 41:
                _v40_to_v41(conn)
            if current < 42:
                _v41_to_v42(conn)
            if current < 43:
                _v42_to_v43(conn)
            if current < 44:
                _v43_to_v44(conn)
            if current < 45:
                _v44_to_v45(conn)
            if current < 46:
                _v45_to_v46(conn)
            if current < 47:
                _v46_to_v47(conn)
            if current < 48:
                _v47_to_v48(conn)
            if current < 49:
                _v48_to_v49_migrate(conn)
            if current < 50:
                _v49_to_v50_migrate(conn)
            if current < 51:
                for sql in _V50_TO_V51_MIGRATIONS:
                    conn.execute(sql)
            if current < 52:
                _v51_to_v52(conn)
            if current < 53:
                _v52_to_v53(conn)
            if current < 54:
                _v53_to_v54(conn)
            if current < 55:
                _v54_to_v55(conn)
            if current < 56:
                _v55_to_v56(conn)
            if current < 57:
                _v56_to_v57(conn)
            if current < 58:
                _v57_to_v58(conn)
            if current < 59:
                _v58_to_v59(conn)
            if current < 60:
                _v59_to_v60(conn)
            if current < 61:
                _v60_to_v61(conn)
            if current < 62:
                _v61_to_v62(conn)
            if current < 63:
                _v62_to_v63(conn)
            if current < 64:
                _v63_to_v64(conn)
            if current < 65:
                _v64_to_v65(conn)
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
                    e,
                    _get_state_dir() / "system.duckdb.pre-migrate",
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


def close_analytics_db() -> None:
    """Close the shared analytics DB connection. Called on app shutdown.

    Mirrors `close_system_db()` above (best-effort CHECKPOINT then
    close, swallow exceptions). Analytics DB is the parquet-views layer;
    a dirty WAL on it is less consequential than on the system DB
    (read-only views can be rebuilt by the orchestrator on next start)
    but the CHECKPOINT keeps the file on-disk clean for any operator
    poking at it with the duckdb CLI.
    """
    global _analytics_db_conn, _analytics_db_path
    if _analytics_db_conn:
        try:
            _analytics_db_conn.execute("CHECKPOINT")
            logger.debug("close_analytics_db: CHECKPOINT ok")
        except Exception as exc:
            logger.warning("close_analytics_db: CHECKPOINT failed (%s); proceeding to close", exc)
        try:
            _analytics_db_conn.close()
        except Exception as exc:
            logger.debug("close_analytics_db: close raised (%s); ignoring", exc)
        _analytics_db_conn = None
        _analytics_db_path = None
