"""Postgres-backed store_submissions repository.

Mirrors ``src/repositories/store_submissions.py``. JSON columns are
JSONB in PG; the casting bridge happens in the INSERT/UPDATE strings.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.repositories.store_submissions import VALID_STATUSES


class StoreSubmissionsPgRepository:
    _TERMINAL_STATUSES = frozenset({"approved", "overridden", "blocked_inline"})

    _SORT_COLUMNS: Dict[str, str] = {
        # PG: EXTRACT(EPOCH FROM ts) replaces DuckDB's epoch(ts).
        "created_at": "EXTRACT(EPOCH FROM s.created_at)",
        "file_size":  "COALESCE(s.file_size, 0)",
        "status":     "s.status",
        "name":       "LOWER(s.name)",
    }

    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _normalize_row(d: Dict[str, Any]) -> Dict[str, Any]:
        for k in ("inline_checks", "llm_findings"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v) if v else None
                except (ValueError, TypeError):
                    d[k] = None
        return d

    def create(
        self,
        *,
        submitter_id: str,
        submitter_email: Optional[str],
        type: str,
        name: str,
        version: Optional[str],
        status: str,
        entity_id: Optional[str] = None,
        inline_checks: Optional[Dict[str, Any]] = None,
        llm_findings: Optional[Dict[str, Any]] = None,
        file_size: Optional[int] = None,
        bundle_sha256: Optional[str] = None,
    ) -> str:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid submission status: {status!r}")
        sub_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO store_submissions
                        (id, entity_id, submitter_id, submitter_email, type, name,
                         version, status, inline_checks, llm_findings,
                         file_size, bundle_sha256,
                         created_at, updated_at)
                    VALUES (:id, :eid, :sid, :se, :t, :n, :v, :s,
                            CAST(:ic AS JSONB), CAST(:lf AS JSONB),
                            :fs, :bs, :now, :now)"""
                ),
                {
                    "id": sub_id, "eid": entity_id, "sid": submitter_id,
                    "se": submitter_email, "t": type, "n": name,
                    "v": version, "s": status,
                    "ic": json.dumps(inline_checks) if inline_checks is not None else None,
                    "lf": json.dumps(llm_findings) if llm_findings is not None else None,
                    "fs": int(file_size) if file_size is not None else None,
                    "bs": bundle_sha256,
                    "now": now,
                },
            )
        return sub_id

    def mark_deleted_for_entity(self, entity_id: str) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "UPDATE store_submissions "
                    "SET status = 'deleted', updated_at = :now "
                    "WHERE entity_id = :eid RETURNING 1"
                ),
                {"now": datetime.now(timezone.utc), "eid": entity_id},
            ).all()
        return len(rows)

    def mark_bundle_purged(self, id: str) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE store_submissions
                          SET bundle_purged_at = :now,
                              entity_id = NULL,
                              updated_at = :now
                        WHERE id = :id"""
                ),
                {"now": now, "id": id},
            )

    def count_blocked_for_submitter_since(
        self, submitter_id: str, since,
    ) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM store_submissions "
                    "WHERE submitter_id = :sid "
                    "  AND status IN ('blocked_llm', 'review_error') "
                    "  AND created_at >= :since"
                ),
                {"sid": submitter_id, "since": since},
            ).first()
        return int(row[0]) if row else 0

    count_blocked_inline_for_submitter_since = count_blocked_for_submitter_since

    def update_status(
        self,
        id: str,
        *,
        status: str,
        llm_findings: Optional[Dict[str, Any]] = None,
        reviewed_by_model: Optional[str] = None,
        allow_terminal_overwrite: bool = False,
    ) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid submission status: {status!r}")
        sets = ["status = :status", "updated_at = :now"]
        params: Dict[str, Any] = {
            "status": status,
            "now": datetime.now(timezone.utc),
            "id": id,
        }
        if llm_findings is not None:
            sets.append("llm_findings = CAST(:lf AS JSONB)")
            params["lf"] = json.dumps(llm_findings)
        if reviewed_by_model is not None:
            sets.append("reviewed_by_model = :rbm")
            params["rbm"] = reviewed_by_model

        where_clauses = ["id = :id"]
        if not allow_terminal_overwrite:
            terminal_keys: List[str] = []
            for i, st in enumerate(self._TERMINAL_STATUSES):
                k = f"term_{i}"
                terminal_keys.append(f":{k}")
                params[k] = st
            where_clauses.append(f"status NOT IN ({','.join(terminal_keys)})")

        sql = (
            f"UPDATE store_submissions SET {', '.join(sets)} "
            f"WHERE {' AND '.join(where_clauses)} RETURNING 1"
        )
        with self._engine.begin() as conn:
            row = conn.execute(sa.text(sql), params).first()
        return row is not None

    def set_inline_result(
        self,
        id: str,
        *,
        inline_checks: Optional[Dict[str, Any]],
        status: str,
    ) -> None:
        """Parity with the DuckDB repo — admin rescan writeback (replace
        inline_checks, clear llm_findings, set status), unconditional."""
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid submission status: {status!r}")
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE store_submissions "
                    "   SET inline_checks = CAST(:ic AS JSONB), llm_findings = NULL, "
                    "       status = :status, updated_at = :now "
                    " WHERE id = :id"
                ),
                {
                    "ic": json.dumps(inline_checks) if inline_checks is not None else None,
                    "status": status,
                    "now": datetime.now(timezone.utc),
                    "id": id,
                },
            )

    def set_override(
        self,
        id: str,
        *,
        admin_user_id: str,
        reason: str,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE store_submissions
                          SET status = 'overridden',
                              override_by = :by,
                              override_reason = :reason,
                              updated_at = :now
                        WHERE id = :id"""
                ),
                {
                    "by": admin_user_id,
                    "reason": reason,
                    "now": datetime.now(timezone.utc),
                    "id": id,
                },
            )

    def reap_stuck_pending_llm(
        self,
        *,
        grace_seconds: int,
        error_payload: Dict[str, Any],
    ) -> List[Tuple[str, str]]:
        """Postgres mirror of
        ``StoreSubmissionsRepository.reap_stuck_pending_llm``. One atomic
        ``UPDATE … WHERE status='pending_llm' AND created_at < cutoff
        RETURNING`` does the flip and reports the reaped rows so the
        reaper writes audit entries without a second round-trip.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    """UPDATE store_submissions
                          SET status = 'review_error',
                              llm_findings = CAST(:lf AS JSONB),
                              updated_at = :now
                        WHERE status = 'pending_llm'
                          AND created_at < :cutoff
                    RETURNING id, submitter_id"""
                ),
                {
                    "lf": json.dumps(error_payload),
                    "now": now,
                    "cutoff": cutoff,
                },
            ).all()
        return [(r[0], r[1]) for r in rows]

    def count_for_submitter(self, submitter_id: str, exclude_id: Optional[str] = None) -> int:
        with self._engine.connect() as conn:
            if exclude_id:
                row = conn.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM store_submissions "
                        "WHERE submitter_id = :sid AND id != :eid"
                    ),
                    {"sid": submitter_id, "eid": exclude_id},
                ).first()
            else:
                row = conn.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM store_submissions WHERE submitter_id = :sid"
                    ),
                    {"sid": submitter_id},
                ).first()
        return int(row[0]) if row else 0

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM store_submissions WHERE id = :id"),
                {"id": id},
            ).mappings().first()
        return self._normalize_row(dict(row)) if row else None

    def latest_for_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT * FROM store_submissions
                        WHERE entity_id = :eid
                        ORDER BY created_at DESC
                        LIMIT 1"""
                ),
                {"eid": entity_id},
            ).mappings().first()
        return self._normalize_row(dict(row)) if row else None

    def delete(self, id: str) -> bool:
        """Parity with the DuckDB repo — hard-delete a submission row by id.
        Returns ``True`` when a row was removed, ``False`` otherwise."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM store_submissions WHERE id = :id"),
                {"id": id},
            )
        return result.rowcount > 0

    def list_for_entity(self, entity_id: str) -> List[Dict[str, Any]]:
        """Parity with the DuckDB repo — every submission linked to
        ``entity_id``, newest first, fixed projection (no JSON columns)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id, status, version, created_at, reviewed_by_model "
                    "FROM store_submissions "
                    "WHERE entity_id = :eid "
                    "ORDER BY created_at DESC"
                ),
                {"eid": entity_id},
            ).mappings().all()
        return [dict(r) for r in rows]

    def list_for_admin(
        self,
        *,
        status: Optional[List[str]] = None,
        submitter_id: Optional[str] = None,
        type_: Optional[str] = None,
        name_substr: Optional[str] = None,
        version_substr: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        lifecycle: Optional[str] = None,
        limit: int = 100,
        skip: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        clauses: List[str] = []
        params: Dict[str, Any] = {}

        if status:
            st_keys: List[str] = []
            for i, st in enumerate(status):
                k = f"st_{i}"
                st_keys.append(f":{k}")
                params[k] = st
            clauses.append(f"s.status IN ({','.join(st_keys)})")

        if submitter_id:
            clauses.append("s.submitter_id = :submitter_id")
            params["submitter_id"] = submitter_id
        if type_:
            clauses.append("s.type = :type_")
            params["type_"] = type_
        if name_substr:
            clauses.append("LOWER(s.name) LIKE :name_substr")
            params["name_substr"] = f"%{name_substr.lower()}%"
        if version_substr:
            clauses.append("LOWER(COALESCE(s.version, '')) LIKE :version_substr")
            params["version_substr"] = f"%{version_substr.lower()}%"

        if lifecycle == "archived":
            clauses.append("e.visibility_status = 'archived'")
        elif lifecycle == "deleted":
            clauses.append("s.status = 'deleted'")
        elif not status:
            clauses.append(
                "(e.visibility_status IS NULL OR e.visibility_status != 'archived')"
            )
            clauses.append("s.status != 'deleted'")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sort_key = sort_by or "created_at"
        if sort_key not in self._SORT_COLUMNS:
            raise ValueError(f"invalid_sort_key: {sort_key!r}")
        col_expr = self._SORT_COLUMNS[sort_key]
        order = "ASC" if (sort_order or "desc").lower() == "asc" else "DESC"

        with self._engine.connect() as conn:
            total = conn.execute(
                sa.text(
                    f"SELECT COUNT(*) FROM store_submissions s "
                    f"LEFT JOIN store_entities e ON e.id = s.entity_id "
                    f"{where}"
                ),
                params,
            ).scalar() or 0

            list_params = {**params, "limit": int(limit), "offset": int(skip)}
            rows = conn.execute(
                sa.text(
                    f"SELECT s.*, "
                    f"  e.visibility_status AS entity_visibility_status, "
                    f"  e.version_history   AS entity_version_history, "
                    f"  e.version_no        AS entity_version_no "
                    f"FROM store_submissions s "
                    f"LEFT JOIN store_entities e ON e.id = s.entity_id "
                    f"{where} "
                    f"ORDER BY {col_expr} {order}, s.id "
                    f"LIMIT :limit OFFSET :offset"
                ),
                list_params,
            ).mappings().all()
        if not rows:
            return [], int(total)
        items = [self._normalize_row(dict(r)) for r in rows]
        for item in items:
            history = item.get("entity_version_history")
            if isinstance(history, str):
                try:
                    history = json.loads(history) if history else []
                except (ValueError, TypeError):
                    history = []
            elif history is None:
                history = []
            item["entity_version_history"] = history
            item["version_no"] = None
            sub_id = item.get("id")
            for entry in history:
                try:
                    if entry.get("submission_id") == sub_id:
                        item["version_no"] = int(entry.get("n"))
                        break
                except (TypeError, ValueError):
                    continue
        return items, int(total)
