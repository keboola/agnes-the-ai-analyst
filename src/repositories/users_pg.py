"""Postgres-backed user repository.

Mirrors ``src/repositories/users.py``. Public surface matches; storage
is SQLAlchemy Core over the singleton engine from ``src.db_pg``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class UsersPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).mappings().first()
        return dict(row) if row else None

    def get_by_ids(self, user_ids: List[str]) -> Dict[str, Optional[str]]:
        """Bulk map ``user_id → email`` for the given ids (PG sibling of the
        DuckDB ``get_by_ids``). Missing rows are absent; empty input → ``{}``."""
        ids = list(user_ids)
        if not ids:
            return {}
        stmt = sa.text("SELECT id, email FROM users WHERE id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt, {"ids": ids}).all()
        return {r[0]: r[1] for r in rows}

    def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM users WHERE email = :email"), {"email": email}).mappings().first()
        return dict(row) if row else None

    def get_by_slack_user_id(self, slack_user_id: str) -> Optional[Dict[str, Any]]:
        """Resolve the account bound to a Slack ``user_id`` (NULL until the
        analyst redeems a /agnes verification code)."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM users WHERE slack_user_id = :sid"),
                    {"sid": slack_user_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        """Exhaustive enumeration of users (no pagination).

        Mirrors DuckDB UserRepository.list_all() — used by bootstrap paths
        that need the full set (auth router login, app/main.py admin
        promotion). API endpoint listings use list_paginated() instead.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT * FROM users ORDER BY email")).mappings().all()
        return [dict(r) for r in rows]

    def list_paginated(self, limit: int = 1000, offset: int = 0) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM users ORDER BY email LIMIT :limit OFFSET :offset"),
                    {"limit": limit, "offset": offset},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def search_recent(
        self,
        limit: int = 10,
        search: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """The N most recently registered users (``created_at`` DESC),
        optionally narrowed by ``search`` (email OR name, case-insensitive)
        and/or ``group_id`` membership. Mirrors DuckDB
        ``UserRepository.search_recent``."""
        clauses: List[str] = []
        params: Dict[str, Any] = {"limit": limit}
        if search:
            clauses.append("(u.email ILIKE :search OR u.name ILIKE :search)")
            params["search"] = f"%{search}%"
        if group_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM user_group_members m WHERE m.user_id = u.id AND m.group_id = :group_id)"
            )
            params["group_id"] = group_id
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        f"SELECT u.* FROM users u{where} ORDER BY u.created_at DESC NULLS LAST, u.email LIMIT :limit"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def count_all(self) -> int:
        with self._engine.connect() as conn:
            return conn.execute(sa.text("SELECT COUNT(*) FROM users")).scalar() or 0

    def create(
        self,
        id: str,
        email: str,
        name: str,
        password_hash: Optional[str] = None,
        must_change_password: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO users (id, email, name, password_hash, must_change_password, created_at, updated_at)
                       VALUES (:id, :email, :name, :password_hash, :must_change_password, :created_at, :updated_at)"""
                ),
                {
                    "id": id,
                    "email": email,
                    "name": name,
                    "password_hash": password_hash,
                    "must_change_password": must_change_password,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def update(self, id: str, **kwargs) -> None:
        allowed = {
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
            "onboarded",
            "last_pull_at",
            "slack_user_id",
            "must_change_password",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE users SET {set_clause} WHERE id = :user_id"),
                {**updates, "user_id": id},
            )

    def consume_reset_token(self, *, email: str, token: str, cutoff, consume_id: str) -> bool:
        """Atomically consume a password-reset token (PG sibling of the DuckDB
        method). UPDATE + verifying SELECT run in one transaction; returns True
        iff this call won the race."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE users SET reset_token = :cid, reset_token_created = NULL "
                    "WHERE email = :email AND reset_token = :token "
                    "AND reset_token_created IS NOT NULL AND reset_token_created >= :cutoff "
                    "AND active = TRUE"
                ),
                {"cid": consume_id, "email": email, "token": token, "cutoff": cutoff},
            )
            row = conn.execute(
                sa.text("SELECT reset_token FROM users WHERE email = :email"),
                {"email": email},
            ).fetchone()
        return bool(row and row[0] == consume_id)

    def count_admins(self, active_only: bool = True) -> int:
        sql = """
            SELECT COUNT(DISTINCT u.id)
            FROM users u
            JOIN user_group_members m ON m.user_id = u.id
            JOIN user_groups g ON g.id = m.group_id
            WHERE g.name = 'Admin'
        """
        if active_only:
            sql += " AND COALESCE(u.active, TRUE) = TRUE"
        with self._engine.connect() as conn:
            return conn.execute(sa.text(sql)).scalar() or 0

    def delete(self, user_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM user_group_members WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            conn.execute(
                sa.text("DELETE FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            )
