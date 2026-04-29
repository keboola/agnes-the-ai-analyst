"""Repositories for Telegram links, pending codes, and script registry."""

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class TelegramRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def link_user(self, user_id: str, chat_id: int) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO telegram_links (user_id, chat_id, linked_at)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET chat_id = excluded.chat_id, linked_at = excluded.linked_at""",
            [user_id, chat_id, now],
        )

    def unlink_user(self, user_id: str) -> None:
        self.conn.execute("DELETE FROM telegram_links WHERE user_id = ?", [user_id])

    def get_link(self, user_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM telegram_links WHERE user_id = ?", [user_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def get_all_links(self) -> List[Dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM telegram_links").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]


class PendingCodeRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def create_code(self, code: str, chat_id: int) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "INSERT INTO pending_codes (code, chat_id, created_at) VALUES (?, ?, ?)",
            [code, chat_id, now],
        )

    def verify_code(self, code: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM pending_codes WHERE code = ?", [code]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        row = dict(zip(columns, result))
        self.conn.execute("DELETE FROM pending_codes WHERE code = ?", [code])
        return row


class ScriptRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def deploy(
        self, id: str, name: str, owner: Optional[str] = None,
        schedule: Optional[str] = None, source: str = "",
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO script_registry (id, name, owner, schedule, source, deployed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, schedule = excluded.schedule,
                source = excluded.source, deployed_at = excluded.deployed_at""",
            [id, name, owner, schedule, source, now],
        )

    def undeploy(self, script_id: str) -> None:
        self.conn.execute("DELETE FROM script_registry WHERE id = ?", [script_id])

    def get(self, script_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM script_registry WHERE id = ?", [script_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_all(self, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        if owner:
            results = self.conn.execute(
                "SELECT * FROM script_registry WHERE owner = ? ORDER BY name", [owner]
            ).fetchall()
        else:
            results = self.conn.execute("SELECT * FROM script_registry ORDER BY name").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def claim_for_run(self, script_id: str) -> bool:
        """Atomically set last_status='running' iff the script is idle.

        Returns True iff this caller is the new owner of the run slot.
        Returns False if the script does not exist OR is already running.

        Implementation: UPDATE … WHERE last_status IS DISTINCT FROM 'running'
        + RETURNING id. DuckDB supports IS DISTINCT FROM and RETURNING; if
        zero rows come back, somebody else already owns the slot.
        """
        now = datetime.now(timezone.utc)
        result = self.conn.execute(
            """UPDATE script_registry
               SET last_status = 'running', last_run = ?
               WHERE id = ?
                 AND last_status IS DISTINCT FROM 'running'
               RETURNING id""",
            [now, script_id],
        ).fetchone()
        return result is not None

    def record_run_result(self, script_id: str, status: str) -> None:
        """Write the terminal status of a finished run (clears 'running').

        Accepts only 'success' or 'failure' — 'running' would re-arm the
        flag instead of clearing it, defeating the purpose of the call.
        """
        if status not in ("success", "failure"):
            raise ValueError(
                f"record_run_result: status must be 'success' or 'failure', "
                f"got {status!r}"
            )
        self.conn.execute(
            "UPDATE script_registry SET last_status = ? WHERE id = ?",
            [status, script_id],
        )
