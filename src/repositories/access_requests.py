"""Repository for access requests."""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class AccessRequestRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def create(self, user_id: str, user_email: str, table_id: str, reason: str = "") -> str:
        """Create a new access request. Returns request ID."""
        req_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO access_requests (id, user_id, user_email, table_id, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            [req_id, user_id, user_email, table_id, reason, now],
        )
        return req_id

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM access_requests WHERE id = ?", [request_id]).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_pending(self) -> List[Dict[str, Any]]:
        """List all pending requests (for admin)."""
        results = self.conn.execute(
            "SELECT * FROM access_requests WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def list_by_user(self, user_id: str) -> List[Dict[str, Any]]:
        """List all requests by a user."""
        results = self.conn.execute(
            "SELECT * FROM access_requests WHERE user_id = ? ORDER BY created_at DESC",
            [user_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def approve(self, request_id: str, reviewed_by: str) -> bool:
        """Approve request and grant access."""
        req = self.get(request_id)
        if not req or req["status"] != "pending":
            return False

        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE access_requests SET status = 'approved', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            [reviewed_by, now, request_id],
        )

        # Auto-grant permission
        from src.repositories.sync_settings import DatasetPermissionRepository
        DatasetPermissionRepository(self.conn).grant(req["user_id"], req["table_id"], "read")

        return True

    def deny(self, request_id: str, reviewed_by: str) -> bool:
        """Deny a request."""
        req = self.get(request_id)
        if not req or req["status"] != "pending":
            return False

        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE access_requests SET status = 'denied', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            [reviewed_by, now, request_id],
        )
        return True

    def has_pending_request(self, user_id: str, table_id: str) -> bool:
        """Check if user already has a pending request for this table."""
        result = self.conn.execute(
            "SELECT id FROM access_requests WHERE user_id = ? AND table_id = ? AND status = 'pending'",
            [user_id, table_id],
        ).fetchone()
        return result is not None
