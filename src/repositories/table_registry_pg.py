"""Postgres-backed table registry repository.

Mirrors ``src/repositories/table_registry.py``. The ``primary_key`` /
``where_filters`` encode/decode helpers are re-exported from the original
module so behaviour stays bit-identical across backends.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.repositories.table_registry import (
    _decode_primary_key,
    _decode_where_filters,
    _encode_primary_key,
    _encode_where_filters,
)


class TableRegistryPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def register(
        self,
        id: str,
        name: str,
        folder: Optional[str] = None,
        sync_strategy: Optional[str] = None,
        primary_key: Union[None, str, List[str]] = None,
        description: Optional[str] = None,
        registered_by: Optional[str] = None,
        source_type: Optional[str] = None,
        bucket: Optional[str] = None,
        source_table: Optional[str] = None,
        source_query: Optional[str] = None,
        query_mode: str = "local",
        sync_schedule: Optional[str] = None,
        profile_after_sync: bool = True,
        registered_at: Optional[datetime] = None,
        incremental_window_days: Optional[int] = None,
        max_history_days: Optional[int] = None,
        incremental_column: Optional[str] = None,
        where_filters: Union[None, str, List[Dict[str, Any]]] = None,
        partition_by: Optional[str] = None,
        partition_granularity: Optional[str] = None,
        initial_load_chunk_days: Optional[int] = None,
    ) -> None:
        ts = registered_at or datetime.now(timezone.utc)
        encoded_pk = _encode_primary_key(primary_key)
        encoded_filters = _encode_where_filters(where_filters)
        effective_strategy = sync_strategy or "full_refresh"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO table_registry (id, name, folder, sync_strategy,
                          primary_key, description, registered_by, registered_at,
                          source_type, bucket, source_table, source_query, query_mode,
                          sync_schedule, profile_after_sync,
                          incremental_window_days, max_history_days, incremental_column,
                          where_filters, partition_by, partition_granularity,
                          initial_load_chunk_days)
                       VALUES (:id, :name, :folder, :strategy, :pk, :description,
                               :registered_by, :registered_at,
                               :source_type, :bucket, :source_table, :source_query, :query_mode,
                               :sync_schedule, :profile_after_sync,
                               :iwd, :mhd, :icol, :wf, :pby, :pgr, :ilcd)
                       ON CONFLICT (id) DO UPDATE SET
                         name = EXCLUDED.name,
                         folder = EXCLUDED.folder,
                         sync_strategy = EXCLUDED.sync_strategy,
                         primary_key = EXCLUDED.primary_key,
                         description = EXCLUDED.description,
                         registered_at = EXCLUDED.registered_at,
                         source_type = EXCLUDED.source_type,
                         bucket = EXCLUDED.bucket,
                         source_table = EXCLUDED.source_table,
                         source_query = EXCLUDED.source_query,
                         query_mode = EXCLUDED.query_mode,
                         sync_schedule = EXCLUDED.sync_schedule,
                         profile_after_sync = EXCLUDED.profile_after_sync,
                         incremental_window_days = EXCLUDED.incremental_window_days,
                         max_history_days = EXCLUDED.max_history_days,
                         incremental_column = EXCLUDED.incremental_column,
                         where_filters = EXCLUDED.where_filters,
                         partition_by = EXCLUDED.partition_by,
                         partition_granularity = EXCLUDED.partition_granularity,
                         initial_load_chunk_days = EXCLUDED.initial_load_chunk_days"""
                ),
                {
                    "id": id,
                    "name": name,
                    "folder": folder,
                    "strategy": effective_strategy,
                    "pk": encoded_pk,
                    "description": description,
                    "registered_by": registered_by,
                    "registered_at": ts,
                    "source_type": source_type,
                    "bucket": bucket,
                    "source_table": source_table,
                    "source_query": source_query,
                    "query_mode": query_mode,
                    "sync_schedule": sync_schedule,
                    "profile_after_sync": profile_after_sync,
                    "iwd": incremental_window_days,
                    "mhd": max_history_days,
                    "icol": incremental_column,
                    "wf": encoded_filters,
                    "pby": partition_by,
                    "pgr": partition_granularity,
                    "ilcd": initial_load_chunk_days,
                },
            )

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        if "primary_key" in row_dict:
            row_dict["primary_key"] = _decode_primary_key(row_dict["primary_key"])
        if "where_filters" in row_dict:
            row_dict["where_filters"] = _decode_where_filters(row_dict["where_filters"])
        return row_dict

    def unregister(self, table_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM table_registry WHERE id = :id"),
                {"id": table_id},
            )

    def get(self, table_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM table_registry WHERE id = :id"),
                {"id": table_id},
            ).mappings().first()
        return self._decode_row(dict(row)) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT * FROM table_registry ORDER BY name")
            ).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def list_by_source(self, source_type: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM table_registry WHERE source_type = :st ORDER BY name"
                ),
                {"st": source_type},
            ).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def find_by_bq_path(
        self,
        bucket: str,
        source_table: str,
    ) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT * FROM table_registry
                       WHERE source_type = 'bigquery'
                         AND bucket IS NOT NULL
                         AND source_table IS NOT NULL
                         AND lower(bucket) = lower(:bucket)
                         AND lower(source_table) = lower(:source_table)
                       ORDER BY registered_at ASC
                       LIMIT 1"""
                ),
                {"bucket": bucket, "source_table": source_table},
            ).mappings().first()
        return self._decode_row(dict(row)) if row else None

    def list_local(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            if source_type:
                rows = conn.execute(
                    sa.text(
                        """SELECT * FROM table_registry
                           WHERE query_mode = 'local' AND source_type = :st
                           ORDER BY name"""
                    ),
                    {"st": source_type},
                ).mappings().all()
            else:
                rows = conn.execute(
                    sa.text(
                        "SELECT * FROM table_registry WHERE query_mode = 'local' ORDER BY name"
                    ),
                ).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]
