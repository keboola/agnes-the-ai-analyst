"""DuckDB connection management and schema versioning.

Provides get_system_db() for the system state database
and get_analytics_db() for the analytics database with parquet views.
"""

import logging
import os
import re
import shutil
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

SCHEMA_VERSION = 13

_SYSTEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TIMESTAMP DEFAULT current_timestamp
);

-- v13: authorization is now via user_groups + user_group_members + resource_grants.
-- DEPRECATED legacy column kept as NULL artifact:
--   role: from v8/v9 enum (viewer/analyst/km_admin/admin); ignored by app
-- The groups JSON column was dropped in v13 (replaced by user_group_members).
-- DuckDB ALTER DROP COLUMN may be blocked by historic FKs on this table; legacy
-- columns are NULL-ed in the migration and physically dropped in a future
-- table-rebuild release.
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR PRIMARY KEY,
    email VARCHAR UNIQUE NOT NULL,
    name VARCHAR,
    role VARCHAR,
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
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP
);

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

CREATE TABLE IF NOT EXISTS table_registry (
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
    is_public BOOLEAN DEFAULT true,
    registered_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS table_profiles (
    table_id VARCHAR PRIMARY KEY,
    profile JSON NOT NULL,
    profiled_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS dataset_permissions (
    user_id VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    access VARCHAR DEFAULT 'read',
    PRIMARY KEY (user_id, dataset)
);

CREATE TABLE IF NOT EXISTS access_requests (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    user_email VARCHAR NOT NULL,
    table_id VARCHAR NOT NULL,
    reason TEXT,
    status VARCHAR DEFAULT 'pending',
    reviewed_by VARCHAR,
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT current_timestamp
);

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
    last_error      TEXT
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
    updated_at      TIMESTAMP DEFAULT current_timestamp,
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
CREATE TABLE IF NOT EXISTS user_group_members (
    user_id   VARCHAR NOT NULL,
    group_id  VARCHAR NOT NULL,
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
CREATE TABLE IF NOT EXISTS resource_grants (
    id            VARCHAR PRIMARY KEY,
    group_id      VARCHAR NOT NULL,
    resource_type VARCHAR NOT NULL,
    resource_id   VARCHAR NOT NULL,
    assigned_at   TIMESTAMP DEFAULT current_timestamp,
    assigned_by   VARCHAR,
    UNIQUE (group_id, resource_type, resource_id)
);
"""


import threading

_system_db_lock = threading.Lock()
_system_db_conn: duckdb.DuckDBPyConnection | None = None
_system_db_path: str | None = None


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


def get_system_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the system state database.

    Uses a single shared connection per DATA_DIR to avoid DuckDB lock
    conflicts between the main app and background tasks. Returns a cursor
    so callers can safely close() it without closing the underlying connection.
    """
    global _system_db_conn, _system_db_path
    db_path = str(_get_data_dir() / "state" / "system.duckdb")

    with _system_db_lock:
        if _system_db_conn is None or _system_db_path != db_path:
            # Close old connection if DATA_DIR changed (e.g., in tests)
            if _system_db_conn is not None:
                try:
                    _system_db_conn.close()
                except Exception:
                    pass
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _system_db_conn = duckdb.connect(db_path)
            _system_db_path = db_path
            _ensure_schema(_system_db_conn)
        return _system_db_conn.cursor()


def get_analytics_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the analytics database (parquet views)."""
    db_path = _get_data_dir() / "analytics" / "server.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


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
                if token:
                    escaped_token = escape_sql_string_literal(token)
                    conn.execute(
                        f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')"
                    )
                else:
                    conn.execute(
                        f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)"
                    )
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
        return conn
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
    return conn


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
                        pass  # already present (re-run safety)

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
                    pass

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
                pass

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
                    pass  # already migrated (re-run safety)

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


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they don't exist. Apply migrations if schema version changed.

    The first action — running ``_SYSTEM_SCHEMA`` unconditionally — is the
    self-heal pass for split-brain DBs. Scenario: a contributor's DB landed
    at schema_version=N from a partial migration (crash mid-DDL, parallel
    WIP branch with a different table set, etc.), but the on-disk file is
    missing tables this binary expects. Without this pass, the migration
    block below skips because ``current >= SCHEMA_VERSION`` and every
    runtime query against the missing table crashes.

    Because ``_SYSTEM_SCHEMA`` is all ``CREATE TABLE IF NOT EXISTS``,
    running it unconditionally is idempotent: existing tables stay
    untouched (columns + data preserved), missing tables get created.
    Cost: dozens of no-op DDLs per process start.
    """
    conn.execute(_SYSTEM_SCHEMA)

    current = get_schema_version(conn)
    if current < SCHEMA_VERSION:
        # Snapshot before migration for rollback support
        if current > 0:
            try:
                db_path = Path(os.environ.get("DATA_DIR", "./data")) / "state" / "system.duckdb"
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
            conn.execute(
                "UPDATE schema_version SET version = ?, applied_at = current_timestamp",
                [SCHEMA_VERSION],
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
    """Close the shared system DB connection. Called on app shutdown."""
    global _system_db_conn, _system_db_path
    if _system_db_conn:
        try:
            _system_db_conn.close()
        except Exception:
            pass
        _system_db_conn = None
        _system_db_path = None
