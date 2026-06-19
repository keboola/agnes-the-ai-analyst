"""Repository for user management."""

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class UserRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM users WHERE id = ?", [user_id]).fetchone()
        return self._row_to_dict(result)

    def get_by_ids(self, user_ids: List[str]) -> Dict[str, Optional[str]]:
        """Bulk map ``user_id → email`` for the given ids. Missing rows are
        absent from the dict; an empty input returns ``{}``. Used by callers
        that previously ran a raw ``SELECT id, email ... WHERE id IN (...)`` on
        a system connection (#518) — routing through the factory keeps the read
        on the active backend."""
        ids = list(user_ids)
        if not ids:
            return {}
        placeholders = ",".join(["?"] * len(ids))
        rows = self.conn.execute(
            f"SELECT id, email FROM users WHERE id IN ({placeholders})", ids
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM users WHERE email = ?", [email]).fetchone()
        return self._row_to_dict(result)

    def get_by_slack_user_id(self, slack_user_id: str) -> Optional[Dict[str, Any]]:
        """Resolve the account bound to a Slack ``user_id`` (NULL until the
        analyst redeems a /agnes verification code). Used by the Slack bot to
        map an incoming Slack identity to an Agnes user."""
        result = self.conn.execute("SELECT * FROM users WHERE slack_user_id = ?", [slack_user_id]).fetchone()
        return self._row_to_dict(result)

    def list_all(self) -> List[Dict[str, Any]]:
        """Return EVERY user row. Used by bootstrap-lock + startup
        warning paths that need to inspect the whole table (see
        ``app/auth/router.py::bootstrap`` and ``app/main.py``'s
        no-password-set warning). Do NOT add a LIMIT here — the
        bootstrap check ``[u for u in list_all() if u.get('password_hash')]``
        re-opens the endpoint if any password-holder gets paginated
        out, which would let an unauthenticated caller claim admin
        on instances with >LIMIT users. API-surface pagination uses
        ``list_paginated()`` below.
        """
        results = self.conn.execute("SELECT * FROM users ORDER BY email").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def list_paginated(self, limit: int = 1000, offset: int = 0) -> List[Dict[str, Any]]:
        """Paginated user listing for the admin API surface (#336
        ADV-009). Safe to bound — callers explicitly opt into the
        windowed shape and the API enforces ``limit <= 10000`` at
        the Query()-validation layer. Do not call from bootstrap /
        startup paths that need exhaustive enumeration; use
        ``list_all()`` for those.
        """
        results = self.conn.execute("SELECT * FROM users ORDER BY email LIMIT ? OFFSET ?", [limit, offset]).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def search_recent(
        self,
        limit: int = 10,
        search: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """The N most recently registered users (``created_at`` DESC),
        optionally narrowed by a free-text ``search`` (email OR name, case
        -insensitive) and/or membership in ``group_id``.

        Backs the /admin/users page: the table shows only this
        bounded, server-filtered window instead of pulling every account to
        the client. ``EXISTS`` (not a JOIN) keeps the row set free of
        duplicates when a user holds the same group via multiple sources.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if search:
            clauses.append("(u.email ILIKE ? OR u.name ILIKE ?)")
            like = f"%{search}%"
            params += [like, like]
        if group_id:
            clauses.append("EXISTS (SELECT 1 FROM user_group_members m WHERE m.user_id = u.id AND m.group_id = ?)")
            params.append(group_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        results = self.conn.execute(
            f"SELECT u.* FROM users u{where} ORDER BY u.created_at DESC NULLS LAST, u.email LIMIT ?",
            params,
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def count_all(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create(
        self,
        id: str,
        email: str,
        name: str,
        password_hash: Optional[str] = None,
        must_change_password: bool = False,
    ) -> None:
        """Create a user. Group memberships are populated separately.

        Admin promotion happens via ``user_group_members`` (Admin system
        group), not a column on the user row — see ``app.auth.access`` and
        ``UserGroupMembersRepository``.

        New users are NOT auto-added to Everyone: the implicit membership
        was removed when Google-prefix mapping landed because access
        deployments need every membership to be traceable to a real source
        (admin grant, Google sync, or explicit system seed). If you need
        the previous "every new user is in Everyone" behavior, add a
        ``system_seed`` row in the caller after ``create``.
        """
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO users (id, email, name, password_hash, must_change_password, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [id, email, name, password_hash, must_change_password, now, now],
        )

    def update(self, id: str, **kwargs) -> None:
        # Group membership is materialized in `user_group_members`; writers
        # there go through `UserGroupMembersRepository` instead of `update`.
        # The legacy `role` column was dropped in v19.
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
            # v26: explicit "I've finished init" signal flipped by
            # /api/me/onboarded — kept out of the legacy allow-list
            # historically because the endpoint used raw conn.execute.
            "onboarded",
            # v44: per-user pull timestamp — bumped on /api/sync/manifest.
            "last_pull_at",
            # v71: Slack identity binding — set when the analyst redeems a
            # /agnes verification code (services/slack_bot/binding.py).
            "slack_user_id",
            # v77: forced-password-change flag (set on seeded/admin-set
            # passwords, cleared when the user sets their own).
            "must_change_password",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [id]
        self.conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)

    def consume_reset_token(self, *, email: str, token: str, cutoff, consume_id: str) -> bool:
        """Atomically consume a password-reset token: stamp it with ``consume_id``
        iff it is the valid, unexpired token for an active ``email``. Returns True
        iff THIS call won the race (mirrors the magic-link CAS). Goes through the
        repo (not a raw connection) so it runs on the ACTIVE backend — a raw
        DuckDB cursor here silently failed on Postgres instances.

        On a concurrent verify, DuckDB raises a TransactionContext conflict on
        the losing UPDATE; that means another caller won the CAS, so we report
        a loss (``False``) rather than letting the conflict surface as a 500.
        Postgres serializes the two UPDATEs instead (the loser matches zero
        rows), so it reaches the same ``False`` without raising."""
        try:
            self.conn.execute(
                "UPDATE users SET reset_token = ?, reset_token_created = NULL "
                "WHERE email = ? AND reset_token = ? AND reset_token_created IS NOT NULL "
                "AND reset_token_created >= ? AND active = TRUE",
                [consume_id, email, token, cutoff],
            )
        except Exception as exc:  # noqa: BLE001 — DuckDB optimistic-concurrency conflict
            err = str(exc).lower()
            if "conflict" in err or "transaction" in err:
                return False
            raise
        row = self.conn.execute(
            "SELECT reset_token FROM users WHERE email = ?", [email]
        ).fetchone()
        return bool(row and row[0] == consume_id)

    def count_admins(self, active_only: bool = True) -> int:
        """Count active users in the Admin system group."""
        sql = """
            SELECT COUNT(DISTINCT u.id)
            FROM users u
            JOIN user_group_members m ON m.user_id = u.id
            JOIN user_groups g ON g.id = m.group_id
            WHERE g.name = 'Admin'
        """
        if active_only:
            sql += " AND COALESCE(u.active, TRUE) = TRUE"
        result = self.conn.execute(sql).fetchone()
        return int(result[0]) if result else 0

    def delete(self, user_id: str) -> None:
        """Delete user + cascade their group memberships."""
        self.conn.execute(
            "DELETE FROM user_group_members WHERE user_id = ?",
            [user_id],
        )
        self.conn.execute("DELETE FROM users WHERE id = ?", [user_id])
