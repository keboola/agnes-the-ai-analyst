"""Repository for audit logging."""

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, List, Dict

import duckdb


class AuditRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def log(
        self,
        user_id: Optional[str] = None,
        action: str = "",
        resource: Optional[str] = None,
        params: Optional[dict] = None,
        result: Optional[str] = None,
        duration_ms: Optional[int] = None,
        *,
        params_before: Optional[dict] = None,
        client_ip: Optional[str] = None,
        client_kind: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> str:
        """Insert one audit_log row. Returns the new row id.

        The four kwargs after `*` are v40 additions; legacy callers using
        positional args or the original kwargs are unaffected. `params_before`
        is only used for mutating actions where rollback / diff is meaningful;
        leave None for reads, ticks, queries.
        """
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO audit_log
               (id, timestamp, user_id, action, resource, params, result, duration_ms,
                params_before, client_ip, client_kind, correlation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                entry_id, now, user_id, action, resource,
                json.dumps(params) if params else None,
                result, duration_ms,
                json.dumps(params_before) if params_before else None,
                client_ip, client_kind, correlation_id,
            ],
        )
        return entry_id

    def query(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        user_id: Optional[str] = None,
        action: Optional[str] = None,         # legacy single-action filter
        action_prefix: Optional[str] = None,
        action_in: Optional[List[str]] = None,
        resource: Optional[str] = None,
        resource_prefix: Optional[str] = None,
        result_pattern: Optional[str] = None,
        correlation_id: Optional[str] = None,
        q: Optional[str] = None,
        cursor: Optional[tuple] = None,        # keyset (timestamp, id)
        limit: int = 100,
    ) -> tuple[List[Dict[str, Any]], Optional[tuple]]:
        """Query audit_log with rich filters; returns (rows, next_cursor).

        Cursor encodes (timestamp, id) so pagination is stable under
        same-second writes. Pass the returned cursor back as `cursor=` for
        the next page. `None` cursor on input = newest page; `None` cursor
        in return = last page reached.
        """
        where = []
        params: List[Any] = []
        if since is not None:
            where.append("timestamp >= ?"); params.append(since)
        if until is not None:
            where.append("timestamp < ?"); params.append(until)
        if user_id is not None:
            where.append("user_id = ?"); params.append(user_id)
        if action is not None:
            where.append("action = ?"); params.append(action)
        if action_prefix is not None:
            where.append("action LIKE ?"); params.append(action_prefix + "%")
        if action_in:
            placeholders = ",".join("?" for _ in action_in)
            where.append(f"action IN ({placeholders})")
            params.extend(action_in)
        if resource is not None:
            where.append("resource = ?"); params.append(resource)
        if resource_prefix is not None:
            where.append("resource LIKE ?"); params.append(resource_prefix + "%")
        if result_pattern is not None:
            where.append("result LIKE ?"); params.append(result_pattern)
        if correlation_id is not None:
            where.append("correlation_id = ?"); params.append(correlation_id)
        if q:
            # Full-text search is a table scan on `params` JSON cast to text.
            # Safeguard: if caller passes `q` without a `since` filter, force a
            # 7-day cap so we don't scan the entire audit_log. Proper FTS lands
            # in Phase B/C (see parent spec §5.5).
            if since is None:
                since = datetime.now(timezone.utc) - timedelta(days=7)
                where.append("timestamp >= ?"); params.append(since)
            where.append("CAST(params AS VARCHAR) LIKE ?"); params.append(f"%{q}%")
        if cursor is not None:
            ts, cid = cursor
            # Keyset: rows strictly older than the cursor, breaking ties by id desc
            where.append("(timestamp, id) < (?, ?)")
            params.extend([ts, cid])

        sql = "SELECT * FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Fetch limit+1 to determine whether there's a next page
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit + 1)
        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return [], None
        columns = [desc[0] for desc in self.conn.description]
        out = [dict(zip(columns, r)) for r in rows]

        next_cursor: Optional[tuple] = None
        if len(out) > limit:
            last_shown = out[limit - 1]
            next_cursor = (last_shown["timestamp"], last_shown["id"])
            out = out[:limit]
        return out, next_cursor

    def query_actions(
        self,
        actions: List[str],
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return rows whose action is in the given list, newest first."""
        if not actions:
            return []
        placeholders = ",".join("?" for _ in actions)
        sql = f"SELECT * FROM audit_log WHERE action IN ({placeholders}) ORDER BY timestamp DESC LIMIT ?"
        results = self.conn.execute(sql, list(actions) + [limit]).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def query_for_resources(
        self,
        resources: List[str],
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Activity timeline for one or more resource refs.

        Each ``resources`` entry is a full ``resource`` value (e.g.
        ``"store_submission:abc123"``, ``"store_entity:def456"``). Used
        by the submission-detail page to render *"when did each rescan /
        override / approval happen, and who did it"* — proves that the
        latest verdict on the row is fresh and not a stale render.
        """
        if not resources:
            return []
        placeholders = ",".join("?" for _ in resources)
        sql = (
            f"SELECT * FROM audit_log WHERE resource IN ({placeholders}) "
            f"ORDER BY timestamp DESC LIMIT ?"
        )
        results = self.conn.execute(sql, list(resources) + [limit]).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        rows: List[Dict[str, Any]] = []
        for row in results:
            d = dict(zip(columns, row))
            v = d.get("params")
            if isinstance(v, str):
                try:
                    d["params"] = json.loads(v) if v else None
                except (ValueError, TypeError):
                    pass
            rows.append(d)
        return rows

    # -----------------------------------------------------------------
    # aggregates — counts, governance feed, observability facets/KPIs
    # -----------------------------------------------------------------
    def count_for_user(self, user_id: str) -> int:
        """Total audit rows recorded for one user."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE user_id = ?", [user_id]
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def query_governance(
        self,
        *,
        action: Optional[str] = None,
        prefixes: tuple = ("corporate_memory.", "km_"),
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Governance audit feed: ``corporate_memory.*`` + legacy ``km_*`` rows.

        When ``action`` is given, match it exactly across both prefixes
        (``prefix0+action``, ``prefix1+action``); otherwise match every row
        whose action starts with either prefix. Newest first, paged by
        LIMIT/OFFSET.
        """
        p0, p1 = prefixes
        if action:
            sql = (
                "SELECT * FROM audit_log WHERE action IN (?, ?) "
                "ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?"
            )
            params: List[Any] = [f"{p0}{action}", f"{p1}{action}", limit, offset]
        else:
            sql = (
                "SELECT * FROM audit_log "
                "WHERE action LIKE ? OR action LIKE ? "
                "ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?"
            )
            params = [f"{p0}%", f"{p1}%", limit, offset]
        results = self.conn.execute(sql, params).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def facets(
        self,
        *,
        since: datetime,
        scheduler_actions: list[str],
        limit: int = 50,
    ) -> "dict[str, list[dict]]":
        """Distinct facet values present in ``audit_log`` since ``since``.

        Five GROUP BY COUNT(*) buckets (no users JOIN — the caller resolves
        user emails separately): users, actions, results, resources, sources.
        Each bucket is largest-first, capped at ``limit``.
        """
        users = self.conn.execute(
            "SELECT user_id AS id, COUNT(*) AS n FROM audit_log "
            "WHERE timestamp >= ? AND user_id IS NOT NULL "
            "GROUP BY user_id ORDER BY n DESC LIMIT ?",
            [since, limit],
        ).fetchall()
        actions = self.conn.execute(
            "SELECT action AS label, COUNT(*) AS n FROM audit_log "
            "WHERE timestamp >= ? AND action IS NOT NULL "
            "GROUP BY action ORDER BY n DESC LIMIT ?",
            [since, limit],
        ).fetchall()
        results = self.conn.execute(
            "SELECT COALESCE(result, '—') AS label, COUNT(*) AS n "
            "FROM audit_log WHERE timestamp >= ? "
            "GROUP BY result ORDER BY n DESC LIMIT ?",
            [since, limit],
        ).fetchall()
        resources = self.conn.execute(
            "SELECT resource AS label, COUNT(*) AS n FROM audit_log "
            "WHERE timestamp >= ? AND resource IS NOT NULL "
            "GROUP BY resource ORDER BY n DESC LIMIT ?",
            [since, limit],
        ).fetchall()
        sched_in = ",".join("?" for _ in scheduler_actions)
        source_rows = self.conn.execute(
            f"""
            SELECT
              CASE
                WHEN client_kind IS NOT NULL AND client_kind != '' THEN client_kind
                WHEN action IN ({sched_in}) THEN 'scheduler'
                WHEN user_id IS NULL THEN 'system'
                ELSE 'other'
              END AS src,
              COUNT(*) AS n
            FROM audit_log WHERE timestamp >= ?
            GROUP BY src ORDER BY n DESC LIMIT ?
            """,
            list(scheduler_actions) + [since, limit],
        ).fetchall()
        return {
            "users":     [{"id": r[0], "count": r[1]} for r in users],
            "actions":   [{"value": r[0], "count": r[1]} for r in actions],
            "results":   [{"value": r[0], "count": r[1]} for r in results],
            "resources": [{"value": r[0], "count": r[1]} for r in resources],
            "sources":   [{"value": r[0], "count": r[1]} for r in source_rows],
        }

    def kpis(self, *, since: datetime) -> "dict[str, Any]":
        """Headline KPIs over the window: events, active users, errors, p95.

        ``p95`` uses DuckDB's ``approx_quantile`` (the PG sibling uses an
        exact ``percentile_cont``; results may differ within tolerance). The
        error-rate ratio is left to the caller.
        """
        row = self.conn.execute(
            """
            SELECT
              COUNT(*) AS events_total,
              COUNT(DISTINCT user_id) FILTER (WHERE user_id IS NOT NULL) AS active_users,
              COUNT(*) FILTER (WHERE result IS NOT NULL AND result LIKE 'error%') AS errors,
              CAST(approx_quantile(duration_ms, 0.95) AS INTEGER) AS p95
            FROM audit_log WHERE timestamp >= ?
            """,
            [since],
        ).fetchone()
        if row is None:
            return {"events_total": 0, "active_users": 0, "errors": 0, "p95": None}
        return {
            "events_total": int(row[0] or 0),
            "active_users": int(row[1] or 0),
            "errors": int(row[2] or 0),
            "p95": int(row[3]) if row[3] is not None else None,
        }
