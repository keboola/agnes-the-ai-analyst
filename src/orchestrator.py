"""Sync orchestrator — ATTACHes extract.duckdb files into master analytics.duckdb.

Remote table support
--------------------
Extractors that create views referencing external DuckDB extensions (e.g. Keboola,
BigQuery) must include a ``_remote_attach`` table in their extract.duckdb:

    CREATE TABLE _remote_attach (
        alias     VARCHAR,  -- DuckDB alias used in views, e.g. 'kbc'
        extension VARCHAR,  -- Extension name, e.g. 'keboola'
        url       VARCHAR,  -- Connection URL
        token_env VARCHAR   -- Env-var name holding the auth token (NOT the token itself)
    );

At rebuild time the orchestrator reads ``_remote_attach``, installs/loads the
extension, reads the token from the environment, and ATTACHes the external source
so that remote views resolve correctly.
"""

import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

import duckdb

from src.orchestrator_security import (
    escape_sql_string_literal,
    is_builtin_extension,
    is_extension_allowed,
    is_token_env_allowed,
)

logger = logging.getLogger(__name__)

_rebuild_lock = threading.Lock()

# Identifier validation lives in src/identifier_validation.py so the
# orchestrator and the extractors share the same regex (#81 Group D).
# The local names are kept as aliases so existing call sites need no
# rename — they import from a single source of truth now.
from src.identifier_validation import (  # noqa: E402
    _SAFE_IDENTIFIER,  # noqa: F401  (re-exported for any historical caller)
    validate_identifier as _validate_identifier,
)


def _atomic_swap_db(tmp_path: str, target_path: str) -> None:
    """Atomically replace target DuckDB file, cleaning up WAL files."""
    import shutil
    target = Path(target_path)
    tmp = Path(tmp_path)

    # Remove old WAL file if it exists
    old_wal = Path(str(target) + ".wal")
    if old_wal.exists():
        old_wal.unlink()

    # Move temp DB into place
    if tmp.exists():
        shutil.move(str(tmp), str(target))

    # Clean up temp WAL
    tmp_wal = Path(str(tmp) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()


def _get_extracts_dir() -> Path:
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    return data_dir / "extracts"


class SyncOrchestrator:
    """Scans /data/extracts/*, ATTACHes each extract.duckdb, creates master views."""

    def __init__(self, analytics_db_path: str | None = None):
        # analytics_db_path allows override for testing
        if analytics_db_path:
            self._db_path = analytics_db_path
        else:
            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            self._db_path = str(data_dir / "analytics" / "server.duckdb")
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    def rebuild(self) -> Dict[str, List[str]]:
        """Scan all extract directories, ATTACH each, create master views.

        Returns: {source_name: [table_names]} for logging.
        """
        with _rebuild_lock:
            return self._do_rebuild()

    def rebuild_source(self, source_name: str) -> List[str]:
        """Rebuild views from a single source (e.g. after Jira webhook)."""
        with _rebuild_lock:
            return self._do_rebuild_source(source_name)

    def _scan_meta_pairs(self, extracts_dir: Path) -> tuple:
        """Read every connector's `_meta` and return (pairs, clean) where:

        - ``pairs`` — list of (source_name, table_name) tuples successfully
          gathered from `_meta`.
        - ``clean`` — True iff every source's pre-scan succeeded. False if
          any source's `_meta` couldn't be read (transient I/O, mid-write,
          missing/corrupt extract.duckdb).

        Used by view_ownership.reconcile to release stale claims before
        the main rebuild loop tries to claim new names. The ``clean`` flag
        guards against a correctness bug: if source B's pre-scan fails
        and we naively reconcile against an incomplete `pairs` list, B's
        prior ownership is dropped, and another source could claim B's
        name in the same rebuild — a silent overwrite, exactly what
        Group C is meant to prevent. Callers MUST skip reconcile when
        ``clean`` is False; per-row claim-time collision detection still
        catches actual collisions.
        """
        pairs: List[tuple] = []
        clean = True
        for ext_dir in sorted(extracts_dir.iterdir()):
            if not ext_dir.is_dir():
                continue
            db_file = ext_dir / "extract.duckdb"
            if not db_file.exists():
                continue
            if not _validate_identifier(ext_dir.name, "source_name"):
                continue
            try:
                ro_conn = duckdb.connect(str(db_file), read_only=True)
                try:
                    rows = ro_conn.execute(
                        "SELECT table_name FROM _meta"
                    ).fetchall()
                    for (table_name,) in rows:
                        if _validate_identifier(table_name, "table_name"):
                            pairs.append((ext_dir.name, table_name))
                finally:
                    ro_conn.close()
            except Exception as e:
                logger.warning(
                    "scan_meta_pairs: failed to read %s (%s) — "
                    "skipping reconcile this rebuild to avoid releasing "
                    "ownerships prematurely",
                    ext_dir.name, e,
                )
                clean = False
        return pairs, clean

    def _do_rebuild(self) -> Dict[str, List[str]]:
        extracts_dir = _get_extracts_dir()
        if not extracts_dir.exists():
            logger.warning("Extracts directory %s does not exist", extracts_dir)
            return {}

        # Issue #81 Group C — load view ownership map from system DB so we
        # can detect cross-connector view-name collisions during this
        # rebuild and refuse to silently overwrite a previously-claimed
        # name. The map is kept in system.duckdb (analytics.duckdb is
        # rebuilt fresh each time and would not survive).
        from src.db import get_system_db
        from src.repositories.view_ownership import ViewOwnershipRepository
        sys_conn_for_views = get_system_db()
        view_repo = None
        try:
            view_repo = ViewOwnershipRepository(sys_conn_for_views)
            # Pre-scan every connector's _meta so we can run the reconcile
            # pass BEFORE claims are evaluated. This makes "owner stopped
            # publishing → name freed → another source can claim" work in
            # the SAME rebuild rather than requiring two consecutive runs.
            #
            # Correctness: only reconcile when EVERY source's pre-scan
            # succeeded. Otherwise a transient I/O failure on source B
            # would drop B's prior ownership and let another source steal
            # B's name — silent overwrite, exactly the bug Group C
            # prevents. Per-row claim-time collision detection still
            # catches actual collisions even without reconcile this run.
            current_pairs, pre_scan_clean = self._scan_meta_pairs(extracts_dir)
            if pre_scan_clean:
                view_repo.reconcile(current_pairs)
            else:
                logger.warning(
                    "view_ownership: skipping reconcile this rebuild — "
                    "pre-scan was incomplete; renamed tables will release "
                    "their names on the next clean rebuild instead"
                )
            existing_owners = view_repo.get_all()
        except Exception as e:
            logger.warning(
                "view_ownership pre-scan failed: %s — proceeding without "
                "collision detection", e,
            )
            existing_owners = {}
            view_repo = None
            try:
                sys_conn_for_views.close()
            except Exception:
                pass
            sys_conn_for_views = None

        # Track every (source, view) pair this rebuild successfully claims.
        claimed_pairs: List[tuple] = []

        result = {}
        # Write to temp file then rename — avoids lock conflict with query endpoint
        tmp_path = self._db_path + ".tmp"
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
        conn = duckdb.connect(tmp_path)
        try:
            # Detach any previously attached databases (except main and temp)
            attached = [
                row[0]
                for row in conn.execute(
                    "SELECT database_name FROM duckdb_databases() "
                    "WHERE database_name NOT IN ('memory', 'system', 'temp')"
                ).fetchall()
            ]
            for db_name in attached:
                if db_name != Path(self._db_path).stem:
                    try:
                        conn.execute(f"DETACH {db_name}")
                    except Exception:
                        pass

            for ext_dir in sorted(extracts_dir.iterdir()):
                if not ext_dir.is_dir():
                    continue
                db_file = ext_dir / "extract.duckdb"
                if not db_file.exists():
                    logger.debug("Skipping %s — no extract.duckdb", ext_dir.name)
                    continue

                if not _validate_identifier(ext_dir.name, "source_name"):
                    continue

                tables = self._attach_and_create_views(
                    conn, ext_dir.name, str(db_file),
                    existing_owners=existing_owners,
                    claimed_pairs=claimed_pairs,
                    view_repo=view_repo if sys_conn_for_views else None,
                )
                if tables:
                    result[ext_dir.name] = tables
                    logger.info("Attached %s: %d tables", ext_dir.name, len(tables))

            # No end-of-rebuild reconcile: the pre-scan reconcile above
            # already released stale ownerships using a complete view of
            # every source's `_meta`. Reconciling again here against
            # `claimed_pairs` (which excludes refused collisions and any
            # source that failed to attach) would incorrectly drop the
            # legitimate prior owner of a name when its DB happens to be
            # transiently unreadable. See test
            # `test_pre_scan_failure_does_not_release_ownership` for the
            # contract.
        finally:
            conn.execute("CHECKPOINT")
            conn.close()
            if sys_conn_for_views is not None:
                try:
                    sys_conn_for_views.close()
                except Exception:
                    pass

        # Atomic swap: replace analytics.duckdb with new version
        _atomic_swap_db(tmp_path, self._db_path)

        return result

    def _do_rebuild_source(self, source_name: str) -> List[str]:
        """Rebuild views for a single source by doing a full rebuild.

        A full rebuild is necessary because the analytics DB is created fresh
        each time (temp file + atomic swap). Rebuilding only one source would
        destroy views from all other sources.
        """
        extracts_dir = _get_extracts_dir()
        db_file = extracts_dir / source_name / "extract.duckdb"
        if not db_file.exists():
            logger.warning("No extract.duckdb for source %s", source_name)
            return []

        result = self._do_rebuild()
        return result.get(source_name, [])

    def _attach_and_create_views(
        self,
        conn: duckdb.DuckDBPyConnection,
        source_name: str,
        db_path: str,
        existing_owners: Optional[Dict[str, str]] = None,
        claimed_pairs: Optional[List[tuple]] = None,
        view_repo=None,
    ) -> List[str]:
        """ATTACH extract.duckdb, read _meta, create views in master.

        Issue #81 Group C — when ``existing_owners`` and ``view_repo`` are
        provided, the orchestrator checks for cross-connector view-name
        collisions and refuses to overwrite a name owned by another source.
        ``claimed_pairs`` accumulates the (source, view) tuples this
        rebuild successfully claims; the caller uses it for end-of-rebuild
        reconcile.
        """
        if existing_owners is None:
            existing_owners = {}
        tables = []
        try:
            conn.execute(f"ATTACH '{db_path}' AS {source_name} (READ_ONLY)")

            # Re-ATTACH external extensions needed by remote views
            self._attach_remote_extensions(conn, source_name)

            # Read _meta to know what's available
            meta_rows = conn.execute(
                f"SELECT table_name, rows, size_bytes, query_mode "
                f"FROM {source_name}._meta"
            ).fetchall()

            for table_name, rows, size_bytes, query_mode in meta_rows:
                if not _validate_identifier(table_name, "table_name"):
                    continue

                # Issue #81 Group C — refuse cross-connector collisions.
                # First-come-first-served: the source already in
                # view_ownership keeps the name; any other source that
                # tries to claim it gets logged + skipped until the
                # operator renames one side. Re-claim by the same source
                # is fine (idempotent rebuild).
                if view_repo is not None:
                    if not view_repo.claim(table_name, source_name):
                        # Query live owner — covers two cases:
                        # (1) stale snapshot from rebuild start (existing_owners),
                        # (2) two sources both first-time-claim the same name
                        #     in this rebuild — the loser sees the winner here.
                        prior_owner = (
                            view_repo.get_owner(table_name)
                            or existing_owners.get(table_name, "<unknown>")
                        )
                        logger.error(
                            "view_ownership collision: %s already owns view %r; "
                            "%s.%s will NOT be exposed. Rename `name` in the "
                            "table_registry on one side to resolve.",
                            prior_owner, table_name, source_name, table_name,
                        )
                        continue
                    if claimed_pairs is not None:
                        claimed_pairs.append((source_name, table_name))

                conn.execute(
                    f"CREATE OR REPLACE VIEW \"{table_name}\" AS "
                    f"SELECT * FROM {source_name}.\"{table_name}\""
                )
                tables.append(table_name)

            # Update sync_state in system DB
            self._update_sync_state(meta_rows, source_name)

        except Exception as e:
            logger.error("Failed to attach %s: %s", source_name, e)

        return tables

    def _attach_remote_extensions(
        self, conn: duckdb.DuckDBPyConnection, source_name: str
    ) -> None:
        """Read _remote_attach from extract.duckdb and ATTACH external sources."""
        try:
            # DuckDB attached-DB layout: ATTACH 'extract.duckdb' AS <alias>
            # exposes information_schema.tables with table_catalog=<alias>
            # and table_schema='main'. The earlier draft used
            # table_schema=<alias> here, which never matched and made
            # _attach_remote_extensions a silent no-op for every
            # connector — defeating the entire Group A hardening in
            # production. db.py:_reattach_remote_extensions already used
            # the correct column; this aligns the rebuild path.
            tables = conn.execute(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_catalog='{source_name}' AND table_name='_remote_attach'"
            ).fetchall()
            if not tables:
                return
        except Exception:
            return

        rows = conn.execute(
            f"SELECT alias, extension, url, token_env FROM {source_name}._remote_attach"
        ).fetchall()

        for alias, extension, url, token_env in rows:
            # Identifier sanity (defense against weird input). The hard
            # security boundary is the allowlist a few lines down.
            if not _validate_identifier(alias, "remote_attach alias"):
                continue
            if not _validate_identifier(extension, "remote_attach extension"):
                continue

            # #81 Group A.1 — extension allowlist. The connector does NOT
            # get to pick what extensions the orchestrator loads.
            if not is_extension_allowed(extension):
                logger.error(
                    "Remote attach %s: extension %r is not in the allowlist; refusing. "
                    "Override via AGNES_REMOTE_ATTACH_EXTENSIONS if intended.",
                    alias, extension,
                )
                continue

            # #81 Group A.2 — token-env hard allowlist. Refuses well-known
            # runtime secrets (JWT_SECRET_KEY, OPENAI_API_KEY, …) that a
            # malicious connector might ask us to send to its server.
            if token_env and not is_token_env_allowed(token_env):
                logger.error(
                    "Remote attach %s: token_env %r is not in the allowlist; refusing. "
                    "Override via AGNES_REMOTE_ATTACH_TOKEN_ENVS if intended.",
                    alias, token_env,
                )
                continue

            token = os.environ.get(token_env, "") if token_env else ""
            if token_env and not token:
                logger.warning(
                    "Remote attach %s: env var %s not set, skipping", alias, token_env
                )
                continue

            try:
                # Skip if already attached (e.g. multiple sources share same extension)
                attached = {
                    r[0] for r in conn.execute(
                        "SELECT database_name FROM duckdb_databases()"
                    ).fetchall()
                }
                if alias in attached:
                    logger.debug("Remote source %s already attached", alias)
                    continue

                # #81 Group A.1 — built-ins LOAD only; community needs INSTALL+LOAD.
                if is_builtin_extension(extension):
                    conn.execute(f"LOAD {extension};")
                else:
                    conn.execute(f"INSTALL {extension} FROM community; LOAD {extension};")
                # #81 Group A.3 — escape URL single-quotes (mirrors src/db.py).
                safe_url = escape_sql_string_literal(url)
                if token:
                    escaped_token = escape_sql_string_literal(token)
                    conn.execute(
                        f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')"
                    )
                else:
                    # Extensions like BigQuery handle auth via env (e.g. GOOGLE_APPLICATION_CREDENTIALS)
                    conn.execute(
                        f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)"
                    )
                logger.info("Attached remote source %s via %s extension", alias, extension)
            except Exception as e:
                logger.error("Failed to attach remote source %s: %s", alias, e)

    def _update_sync_state(self, meta_rows: list, source_name: str) -> None:
        """Update sync_state table in system.duckdb from _meta entries."""
        try:
            from src.db import get_system_db
            from src.repositories.sync_state import SyncStateRepository

            extracts_dir = _get_extracts_dir()
            sys_conn = get_system_db()
            try:
                repo = SyncStateRepository(sys_conn)
                for table_name, rows, size_bytes, query_mode in meta_rows:
                    # Compute hash from parquet file stats (fast, no file read)
                    pq_path = extracts_dir / source_name / "data" / f"{table_name}.parquet"
                    if pq_path.exists():
                        stat = pq_path.stat()
                        file_hash = hashlib.md5(
                            f"{stat.st_mtime_ns}:{stat.st_size}".encode()
                        ).hexdigest()[:12]
                    else:
                        file_hash = ""

                    repo.update_sync(
                        table_id=table_name,
                        rows=rows or 0,
                        file_size_bytes=size_bytes or 0,
                        hash=file_hash,
                    )
            finally:
                sys_conn.close()
        except Exception as e:
            logger.warning("Could not update sync_state: %s", e)
