"""Postgres-backed view ownership repository.

Mirrors ``src/repositories/view_ownership.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ViewOwnershipPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get_owner(self, view_name: str) -> Optional[str]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT source_name FROM view_ownership WHERE view_name = :v"),
                {"v": view_name},
            ).first()
        return row[0] if row else None

    def get_all(self) -> Dict[str, str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT view_name, source_name FROM view_ownership")
            ).all()
        return {r[0]: r[1] for r in rows}

    def claim(self, view_name: str, source_name: str) -> bool:
        existing = self.get_owner(view_name)
        if existing is None:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO view_ownership (view_name, source_name, registered_at) "
                        "VALUES (:v, :s, :now)"
                    ),
                    {"v": view_name, "s": source_name, "now": datetime.now(timezone.utc)},
                )
            return True
        return existing == source_name

    def release(self, view_name: str, source_name: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "DELETE FROM view_ownership WHERE view_name = :v AND source_name = :s RETURNING 1"
                ),
                {"v": view_name, "s": source_name},
            ).first()
        return row is not None

    def reconcile(
        self, current_pairs: List[Tuple[str, str]]
    ) -> List[Tuple[str, str]]:
        live = set(current_pairs)
        with self._engine.begin() as conn:
            all_rows = conn.execute(
                sa.text("SELECT source_name, view_name FROM view_ownership")
            ).all()
            dropped = [
                (src, view) for src, view in all_rows
                if (src, view) not in live
            ]
            for src, view in dropped:
                conn.execute(
                    sa.text(
                        "DELETE FROM view_ownership "
                        "WHERE source_name = :s AND view_name = :v"
                    ),
                    {"s": src, "v": view},
                )
        return dropped

    def list_for_source(self, source_name: str) -> List[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT view_name FROM view_ownership "
                    "WHERE source_name = :s ORDER BY view_name"
                ),
                {"s": source_name},
            ).all()
        return [r[0] for r in rows]
