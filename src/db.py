"""DuckDB connection management for the analytics layer.

Post-PG cutover this module owns only the analytics side: the
``server.duckdb`` file that ATTACHes ``extract.duckdb`` files written
by the connectors and exposes views over them. Business / app state
(users, RBAC, table_registry, sync_state, audit_log, knowledge,
store entities, …) lives in Postgres — see ``src/db_pg.py``.

Public surface:

* ``get_analytics_db()`` — shared writable cursor on the analytics DB.
* ``get_analytics_db_readonly()`` — fresh per-call read-only handle
  with every ``extract.duckdb`` ATTACHed and remote extensions
  re-LOADed so views (BigQuery, etc.) resolve.
* ``close_analytics_db()`` — best-effort CHECKPOINT + close on shutdown.
* ``_get_data_dir`` / ``_get_state_dir`` — path resolvers; kept here
  because the analytics file path derives from them.
* ``SYSTEM_ADMIN_GROUP`` / ``SYSTEM_EVERYONE_GROUP`` — canonical names
  for the two seeded system groups (alembic ``0003_rbac``); kept here
  so existing ``from src.db import SYSTEM_ADMIN_GROUP`` imports work.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

import duckdb

from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError

logger = logging.getLogger(__name__)


# Canonical names of the seeded system user_groups. Alembic 0003_rbac
# inserts these rows; lookups across the codebase use the constants
# rather than literal strings so a rename touches one spot.
SYSTEM_ADMIN_GROUP = "Admin"
SYSTEM_EVERYONE_GROUP = "Everyone"


_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _maybe_instrument(con, db_tag: str):
    """Wrap a DuckDB connection with the debug-panel recorder when DEBUG=1.

    DEBUG is read on each call so tests can toggle via monkeypatch.setenv
    without reloading this module. Connection creation is off the hot
    path; the instrumentation pass-through is the prod default.
    """
    if os.environ.get("DEBUG", "").lower() not in ("1", "true", "yes"):
        return con
    from app.debug.duckdb_panel import InstrumentedConnection

    return InstrumentedConnection(con, db_tag)


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


def _get_state_dir() -> Path:
    """Path to the writable state directory.

    Resolution order:
      1. ``STATE_DIR`` env var (explicit override).
      2. ``${DATA_DIR}/state`` (default).

    Use the override when state should live on a separate disk mounted
    in parallel with ``/data`` rather than nested inside it.
    """
    state = os.environ.get("STATE_DIR", "")
    if state:
        return Path(state)
    return _get_data_dir() / "state"


_analytics_db_lock = threading.Lock()
_analytics_db_conn: duckdb.DuckDBPyConnection | None = None
_analytics_db_path: str | None = None


def get_analytics_db() -> duckdb.DuckDBPyConnection:
    """Singleton cursor on the analytics DB (``$DATA_DIR/analytics/server.duckdb``).

    Each call returns a fresh cursor on the same underlying connection.
    Callers that ``.close()`` the cursor only close the cursor — the
    connection stays alive for the next caller. Re-opens transparently
    when ``DATA_DIR`` changes (test fixtures that swap dirs).
    """
    global _analytics_db_conn, _analytics_db_path
    db_path = str(_get_data_dir() / "analytics" / "server.duckdb")

    with _analytics_db_lock:
        if _analytics_db_conn is None or _analytics_db_path != db_path:
            if _analytics_db_conn is not None:
                try:
                    _analytics_db_conn.close()
                except Exception:
                    pass
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _analytics_db_conn = duckdb.connect(db_path)
            _analytics_db_path = db_path
        return _maybe_instrument(_analytics_db_conn.cursor(), "analytics")


def _reattach_remote_extensions(conn: duckdb.DuckDBPyConnection, extracts_dir: Path) -> None:
    """Re-LOAD DuckDB extensions listed in ``_remote_attach`` tables of each extract.duckdb.

    Called from ``get_analytics_db_readonly`` after ATTACHing the
    extract files so remote views (e.g. BigQuery) resolve at query
    time. LOAD only — never INSTALL — to keep the read path off the
    network; the rebuild path (orchestrator) is responsible for INSTALL.
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
        if ext_dir.name not in attached_dbs:
            continue

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

        try:
            attached_dbs = {
                r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()
            }
        except Exception:
            pass

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
                conn.execute(f"LOAD {extension};")
                token = os.environ.get(token_env, "") if token_env else ""
                safe_url = escape_sql_string_literal(url)

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
                        f"ATTACH '{safe_url}' AS {alias} "
                        f"(TYPE {extension}, TOKEN '{escaped_token}')"
                    )
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
    """Per-call read-only connection to the analytics DB.

    ATTACHes every ``extract.duckdb`` file under ``$DATA_DIR/extracts/``
    so master views resolve, then re-LOADs each source's declared
    remote extensions. Stays per-call (not singleton) because each
    invocation rebuilds the ATTACH set against the current on-disk
    state.
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
    _reattach_remote_extensions(conn, extracts_dir)
    return _maybe_instrument(conn, "analytics_ro")


def close_analytics_db() -> None:
    """Close the shared analytics DB connection on shutdown.

    Best-effort CHECKPOINT then close; both swallow exceptions because
    leaving a dirty WAL on the analytics file is recoverable (the
    orchestrator rebuilds views on next start), and a bound shutdown
    path mustn't raise.
    """
    global _analytics_db_conn, _analytics_db_path
    if _analytics_db_conn:
        try:
            _analytics_db_conn.execute("CHECKPOINT")
            logger.debug("close_analytics_db: CHECKPOINT ok")
        except Exception as exc:
            logger.warning(
                "close_analytics_db: CHECKPOINT failed (%s); proceeding to close",
                exc,
            )
        try:
            _analytics_db_conn.close()
        except Exception as exc:
            logger.debug("close_analytics_db: close raised (%s); ignoring", exc)
        _analytics_db_conn = None
        _analytics_db_path = None
