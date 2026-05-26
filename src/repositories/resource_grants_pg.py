"""Postgres-backed resource-grants repository.

Mirrors ``src/repositories/resource_grants.py``. ``fanout_system_for_group``
is soft-failed when ``marketplace_plugins`` isn't migrated yet (Phase F
in progress); once the table lands, the try/except becomes unnecessary.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError


class ResourceGrantsPgRepository:
    _SELECT_COLS = (
        "id, group_id, resource_type, resource_id, "
        "assigned_at, assigned_by"
    )

    def __init__(self, engine: Engine):
        self._engine = engine

    def list_all(
        self,
        resource_type: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        params: Dict[str, Any] = {}
        if resource_type:
            where.append("g.resource_type = :rtype")
            params["rtype"] = resource_type
        if group_id:
            where.append("g.group_id = :gid")
            params["gid"] = group_id
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        sql = (
            f"""SELECT g.id, g.group_id, ug.name AS group_name,
                       g.resource_type, g.resource_id,
                       g.assigned_at, g.assigned_by
                FROM resource_grants g
                JOIN user_groups ug ON ug.id = g.group_id
                {where_sql}
                ORDER BY ug.name, g.resource_type, g.resource_id"""
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    def list_for_groups(
        self,
        group_ids: List[str],
        resource_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not group_ids:
            return []
        in_keys: List[str] = []
        params: Dict[str, Any] = {}
        for i, gid in enumerate(group_ids):
            k = f"g_{i}"
            in_keys.append(f":{k}")
            params[k] = gid
        type_clause = ""
        if resource_type:
            type_clause = "AND resource_type = :rtype"
            params["rtype"] = resource_type

        sql = (
            f"""SELECT {self._SELECT_COLS}
                FROM resource_grants
                WHERE group_id IN ({','.join(in_keys)}) {type_clause}
                ORDER BY resource_type, resource_id"""
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    def get(self, grant_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(f"SELECT {self._SELECT_COLS} FROM resource_grants WHERE id = :id"),
                {"id": grant_id},
            ).mappings().first()
        return dict(row) if row else None

    def has_grant(
        self,
        group_ids: List[str],
        resource_type: str,
        resource_id: str,
    ) -> bool:
        if not group_ids:
            return False
        in_keys: List[str] = []
        params: Dict[str, Any] = {"rtype": resource_type, "rid": resource_id}
        for i, gid in enumerate(group_ids):
            k = f"g_{i}"
            in_keys.append(f":{k}")
            params[k] = gid
        sql = (
            f"""SELECT 1 FROM resource_grants
                WHERE group_id IN ({','.join(in_keys)})
                  AND resource_type = :rtype
                  AND resource_id = :rid
                LIMIT 1"""
        )
        with self._engine.connect() as conn:
            row = conn.execute(sa.text(sql), params).first()
        return row is not None

    def create(
        self,
        group_id: str,
        resource_type: str,
        resource_id: str,
        assigned_by: Optional[str] = None,
    ) -> str:
        grant_id = str(uuid4())
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO resource_grants
                       (id, group_id, resource_type, resource_id, assigned_by)
                       VALUES (:id, :gid, :rtype, :rid, :ab)"""
                ),
                {
                    "id": grant_id,
                    "gid": group_id,
                    "rtype": resource_type,
                    "rid": resource_id,
                    "ab": assigned_by,
                },
            )
        return grant_id

    def delete(self, grant_id: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text("DELETE FROM resource_grants WHERE id = :id RETURNING 1"),
                {"id": grant_id},
            ).first()
        return row is not None

    def delete_by_resource(
        self,
        resource_type: str,
        resource_id: str,
    ) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    """DELETE FROM resource_grants
                       WHERE resource_type = :rtype AND resource_id = :rid
                       RETURNING 1"""
                ),
                {"rtype": resource_type, "rid": resource_id},
            ).all()
        return len(rows)

    def delete_all_for_group(self, group_id: str) -> int:
        """Drop every grant for ``group_id``. Used by the group-delete cascade."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM resource_grants WHERE group_id = :gid RETURNING 1"
                ),
                {"gid": group_id},
            ).all()
        return len(rows)

    def count_for_group(self, group_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM resource_grants WHERE group_id = :gid"),
                {"gid": group_id},
            ).first()
        return int(row[0]) if row else 0

    def fanout_system_for_group(
        self,
        group_id: str,
        assigned_by: Optional[str] = None,
    ) -> int:
        """Grant every ``is_system=TRUE`` marketplace_plugin to ``group_id``.

        Soft-fail if ``marketplace_plugins`` isn't migrated yet (Phase F
        in progress). Once that port lands, drop the try/except.
        """
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sa.text(
                        "SELECT marketplace_id, name FROM marketplace_plugins "
                        "WHERE is_system = TRUE"
                    ),
                ).all()
        except Exception:
            return 0

        inserted = 0
        for marketplace_id, plugin_name in rows:
            resource_id = f"{marketplace_id}/{plugin_name}"
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        sa.text(
                            """INSERT INTO resource_grants
                               (id, group_id, resource_type, resource_id, assigned_by)
                               VALUES (:id, :gid, 'marketplace_plugin', :rid, :ab)
                               ON CONFLICT ON CONSTRAINT uq_resource_grants_group_type_id
                                 DO NOTHING"""
                        ),
                        {
                            "id": str(uuid4()),
                            "gid": group_id,
                            "rid": resource_id,
                            "ab": assigned_by,
                        },
                    )
                    inserted += 1
            except IntegrityError:
                continue
        return inserted
