"""Sync orchestrator — ATTACHes extract.duckdb files into master analytics.duckdb."""

import logging
import os
import threading
from pathlib import Path
from typing import Dict, List

import duckdb

logger = logging.getLogger(__name__)

_rebuild_lock = threading.Lock()


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

    def _do_rebuild(self) -> Dict[str, List[str]]:
        extracts_dir = _get_extracts_dir()
        if not extracts_dir.exists():
            logger.warning("Extracts directory %s does not exist", extracts_dir)
            return {}

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

                tables = self._attach_and_create_views(
                    conn, ext_dir.name, str(db_file)
                )
                if tables:
                    result[ext_dir.name] = tables
                    logger.info("Attached %s: %d tables", ext_dir.name, len(tables))
        finally:
            conn.close()

        # Atomic swap: replace analytics.duckdb with new version
        import shutil
        if Path(tmp_path).exists():
            shutil.move(tmp_path, self._db_path)

        return result

    def _do_rebuild_source(self, source_name: str) -> List[str]:
        extracts_dir = _get_extracts_dir()
        db_file = extracts_dir / source_name / "extract.duckdb"
        if not db_file.exists():
            logger.warning("No extract.duckdb for source %s", source_name)
            return []

        tmp_path = self._db_path + ".tmp"
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
        conn = duckdb.connect(tmp_path)
        try:
            # Detach if already attached
            try:
                conn.execute(f"DETACH {source_name}")
            except Exception:
                pass
            tables = self._attach_and_create_views(conn, source_name, str(db_file))
        finally:
            conn.close()

        import shutil
        if Path(tmp_path).exists():
            shutil.move(tmp_path, self._db_path)

        return tables

    def _attach_and_create_views(
        self, conn: duckdb.DuckDBPyConnection, source_name: str, db_path: str
    ) -> List[str]:
        """ATTACH extract.duckdb, read _meta, create views in master."""
        tables = []
        try:
            conn.execute(f"ATTACH '{db_path}' AS {source_name} (READ_ONLY)")

            # Read _meta to know what's available
            meta_rows = conn.execute(
                f"SELECT table_name, rows, size_bytes, query_mode "
                f"FROM {source_name}._meta"
            ).fetchall()

            for table_name, rows, size_bytes, query_mode in meta_rows:
                conn.execute(
                    f"CREATE OR REPLACE VIEW \"{table_name}\" AS "
                    f"SELECT * FROM {source_name}.\"{table_name}\""
                )
                tables.append(table_name)

            # Update sync_state in system DB
            self._update_sync_state(meta_rows)

        except Exception as e:
            logger.error("Failed to attach %s: %s", source_name, e)

        return tables

    def _update_sync_state(self, meta_rows: list) -> None:
        """Update sync_state table in system.duckdb from _meta entries."""
        try:
            from src.db import get_system_db
            from src.repositories.sync_state import SyncStateRepository

            sys_conn = get_system_db()
            try:
                repo = SyncStateRepository(sys_conn)
                for table_name, rows, size_bytes, query_mode in meta_rows:
                    repo.update_sync(
                        table_id=table_name,
                        rows=rows or 0,
                        file_size_bytes=size_bytes or 0,
                        hash="",  # TODO: compute from parquet file
                    )
            finally:
                sys_conn.close()
        except Exception as e:
            logger.warning("Could not update sync_state: %s", e)
