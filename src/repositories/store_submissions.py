"""Repository for ``store_submissions`` — flea-market guardrail audit trail.

Every POST/PUT to ``/api/store/entities`` writes a row here capturing the
inline-check verdicts and (asynchronously) the LLM security review outcome.
Powers ``/admin/store/submissions`` and the override workflow. See
``src/store_guardrails/`` for the check pipeline that fills these rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb


VALID_STATUSES = {
    "pending_inline",
    "blocked_inline",
    "pending_llm",
    "approved",
    "blocked_llm",
    "review_error",
    "overridden",
    # 'deleted' is set when the linked entity is hard-deleted (admin DELETE
    # ?hard=true). The entity row is gone, so the JOIN-based filter can't
    # reach it — explicit marker required so the Deleted chip can surface
    # the row.
    "deleted",
    # 'archived' is DEPRECATED in writes. Post-v35, archive lifecycle is
    # read live from `store_entities.visibility_status` via LEFT JOIN
    # rather than denormalized onto submissions. Kept in the validator
    # only to preserve historical rows from instances that ran the prior
    # denormalized path (`mark_archived_for_entity`, removed in v36).
    # No new code path writes this value.
    "archived",
}


class StoreSubmissionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    @staticmethod
    def _row_to_dict(columns: List[str], row: tuple) -> Dict[str, Any]:
        d = dict(zip(columns, row))
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
        self.conn.execute(
            """INSERT INTO store_submissions
                (id, entity_id, submitter_id, submitter_email, type, name,
                 version, status, inline_checks, llm_findings,
                 file_size, bundle_sha256,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                sub_id, entity_id, submitter_id, submitter_email, type, name,
                version, status,
                json.dumps(inline_checks) if inline_checks is not None else None,
                json.dumps(llm_findings) if llm_findings is not None else None,
                int(file_size) if file_size is not None else None,
                bundle_sha256,
                now, now,
            ],
        )
        return sub_id

    # mark_archived_for_entity removed in v36 — lifecycle is read live
    # via JOIN on store_entities.visibility_status. See list_for_admin.

    def mark_deleted_for_entity(self, entity_id: str) -> int:
        """Mark every submission row linked to ``entity_id`` as
        ``status='deleted'`` after a hard delete.

        ``entity_id`` is preserved as a tombstone pointer — the
        ``store_entities`` row is gone, but the linkage lets the
        admin detail page resolve the activity timeline by querying
        ``audit_log`` for ``store_entity:{entity_id}`` even after the
        live row is dropped. UUID collision risk is negligible.

        Submission row + sha256 + size + verdict survive — admin can
        still see what was hard-deleted under the "Deleted" filter
        chip. Bundle bytes are gone (mirrors the TTL purge contract).
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM store_submissions WHERE entity_id = ?",
            [entity_id],
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE store_submissions "
            "   SET status = 'deleted', updated_at = ? "
            "WHERE entity_id = ?",
            [datetime.now(timezone.utc), entity_id],
        )
        return int(before)

    def mark_bundle_purged(self, id: str) -> None:
        """TTL job hook: bundle bytes have been removed from disk; persist
        the timestamp so the detail UI can render *"Bundle purged on …"*
        instead of leaving Download greyed with no explanation. Submission
        row + sha256 stay intact for forensics.
        """
        self.conn.execute(
            """UPDATE store_submissions
                  SET bundle_purged_at = ?,
                      entity_id = NULL,
                      updated_at = ?
                WHERE id = ?""",
            [datetime.now(timezone.utc), datetime.now(timezone.utc), id],
        )

    def count_blocked_for_submitter_since(
        self, submitter_id: str, since,
    ) -> int:
        """Spam-quota helper. Counts submissions by ``submitter_id`` whose
        verdict is one of the rejected/error states
        (``blocked_inline | blocked_llm | review_error``) newer than
        ``since`` (a ``datetime`` — typically now - 24h). Called from
        the POST entry point; refusal bounds disk growth from a single
        bot looping on malformed/risky ZIPs.

        Pre-fix this counted ONLY ``blocked_inline``. A bad-actor
        submitter who triggered ten ``blocked_llm`` verdicts was
        unbounded. All three states represent rejected uploads — count
        them together.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) FROM store_submissions "
            "WHERE submitter_id = ? "
            "  AND status IN ('blocked_inline', 'blocked_llm', 'review_error') "
            "  AND created_at >= ?",
            [submitter_id, since],
        ).fetchone()
        return int(row[0]) if row else 0

    # Backward-compat alias — still used in some operator scripts.
    # Routes to the broader counter post-#9.
    count_blocked_inline_for_submitter_since = count_blocked_for_submitter_since

    def update_status(
        self,
        id: str,
        *,
        status: str,
        llm_findings: Optional[Dict[str, Any]] = None,
        reviewed_by_model: Optional[str] = None,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid submission status: {status!r}")
        sets = ["status = ?", "updated_at = ?"]
        params: List[Any] = [status, datetime.now(timezone.utc)]
        if llm_findings is not None:
            sets.append("llm_findings = ?")
            params.append(json.dumps(llm_findings))
        if reviewed_by_model is not None:
            sets.append("reviewed_by_model = ?")
            params.append(reviewed_by_model)
        params.append(id)
        self.conn.execute(
            f"UPDATE store_submissions SET {', '.join(sets)} WHERE id = ?",
            params,
        )

    def set_override(
        self,
        id: str,
        *,
        admin_user_id: str,
        reason: str,
    ) -> None:
        """Mark a previously-blocked submission as admin-overridden.

        Visibility flip on the linked store_entities row is the caller's
        responsibility — the override path in ``app/api/admin.py`` calls
        ``StoreEntitiesRepository.set_visibility(entity_id, 'approved')``
        in the same transaction.
        """
        self.conn.execute(
            """UPDATE store_submissions
                  SET status = 'overridden',
                      override_by = ?,
                      override_reason = ?,
                      updated_at = ?
                WHERE id = ?""",
            [admin_user_id, reason, datetime.now(timezone.utc), id],
        )

    def count_for_submitter(self, submitter_id: str, exclude_id: Optional[str] = None) -> int:
        """Number of submissions by a single user. Used by the detail page
        footer to render *"N other attempts by alice@x.com"* — pass the
        current submission's id as ``exclude_id`` to exclude it from the
        count so the link reads naturally as "others".
        """
        if exclude_id:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM store_submissions WHERE submitter_id = ? AND id != ?",
                [submitter_id, exclude_id],
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM store_submissions WHERE submitter_id = ?",
                [submitter_id],
            ).fetchone()
        return int(row[0]) if row else 0

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM store_submissions WHERE id = ?", [id]
        ).fetchall()
        if not rows:
            return None
        columns = [d[0] for d in self.conn.description]
        return self._row_to_dict(columns, rows[0])

    def latest_for_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM store_submissions
                WHERE entity_id = ?
                ORDER BY created_at DESC
                LIMIT 1""",
            [entity_id],
        ).fetchall()
        if not rows:
            return None
        columns = [d[0] for d in self.conn.description]
        return self._row_to_dict(columns, rows[0])

    # Whitelisted column names for the click-to-sort UI. ``status`` and
    # ``name`` get NULL-safe wrapping so the sort is deterministic across
    # legacy rows; epoch() bypass on ``created_at`` mirrors the bug
    # workaround in the default-order branch below.
    #
    # Mapping is sort-key → fully qualified SQL expression (already
    # disambiguated against the LEFT JOIN). Bad input raises 400 at the
    # API edge — see ``list_for_admin`` below. Pre-fix the qualification
    # used a chain of ``str.replace(...)`` calls that risked partial
    # replacement when one column name was a substring of another;
    # the explicit dict eliminates the footgun.
    _SORT_COLUMNS: Dict[str, str] = {
        "created_at": "epoch(s.created_at)",
        "file_size":  "COALESCE(s.file_size, 0)",
        "status":     "s.status",
        "name":       "LOWER(s.name)",
    }

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
        """Filtered + paginated listing for /admin/store/submissions.

        v36+ architecture: ``status`` is the verdict (immutable, set at
        review time). The entity's ``visibility_status`` is the live
        lifecycle (archived / approved / hidden / pending) — read live
        via LEFT JOIN. Filtering by lifecycle (Archived / Deleted
        chips) goes through the JOIN; filtering by verdict (Pending /
        Needs review / Approved / Overridden) hits ``status`` directly.

        Adversarial review verdict (chip → SQL truth table):
          * default (no chip)  : exclude lifecycle-end states
          * Pending            : verdict = pending_* AND not archived
          * Needs review       : verdict = blocked/error AND not archived
          * Approved           : verdict = approved AND lifecycle = approved
          * Overridden         : verdict = overridden AND not archived
          * Archived           : entity.visibility_status = 'archived'
          * Deleted            : status = 'deleted'

        ``status`` parameter retains the comma-separated ``status IN ()``
        semantics for backward compat with admin scripts; the chip
        translation lives in the calling layer (``app/api/admin.py``)
        which builds the right combination of ``status`` + the new
        ``lifecycle`` filter param below.
        """
        # Substring + scalar filters are AND-composed onto a base set
        # of clauses; lifecycle handling is its own branch below.
        clauses: List[str] = []
        params: List[Any] = []

        # Verdict filter: pass-through ``status IN (...)`` if explicitly
        # set. When the caller sets *only* lifecycle (e.g. archived
        # chip), they pass status=None; we don't over-filter on status.
        if status:
            placeholders = ",".join("?" for _ in status)
            clauses.append(f"s.status IN ({placeholders})")
            params.extend(status)

        if submitter_id:
            clauses.append("s.submitter_id = ?")
            params.append(submitter_id)
        if type_:
            clauses.append("s.type = ?")
            params.append(type_)
        if name_substr:
            clauses.append("LOWER(s.name) LIKE ?")
            params.append(f"%{name_substr.lower()}%")
        if version_substr:
            clauses.append("LOWER(COALESCE(s.version, '')) LIKE ?")
            params.append(f"%{version_substr.lower()}%")

        # Lifecycle filter — chip-driven, replaces the legacy
        # `status='archived'` / `status='deleted'` denormalization.
        # 'archived' reads live from entity.visibility_status; 'deleted'
        # uses the submission terminal marker (entity row is gone).
        if lifecycle == "archived":
            clauses.append("e.visibility_status = 'archived'")
        elif lifecycle == "deleted":
            clauses.append("s.status = 'deleted'")
        elif not status:
            # Default view: hide both lifecycle-end states so the queue
            # stays focused on actionable rows. Chip routing opts back
            # in by passing lifecycle='archived' or 'deleted'.
            clauses.append(
                "(e.visibility_status IS NULL OR e.visibility_status != 'archived')"
            )
            clauses.append("s.status != 'deleted'")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        # COUNT and SELECT both go through the LEFT JOIN so paging
        # totals match the items list under any filter. Index on
        # store_submissions(entity_id) (idx_store_submissions_entity)
        # already covers the JOIN key — no schema change needed.
        total_row = self.conn.execute(
            f"SELECT COUNT(*) FROM store_submissions s "
            f"LEFT JOIN store_entities e ON e.id = s.entity_id "
            f"{where}",
            params,
        ).fetchone()
        total = int(total_row[0]) if total_row else 0

        # Whitelist lookup — values are already JOIN-qualified in
        # _SORT_COLUMNS. Unknown sort_by raises ValueError; the API
        # caller maps that to a 400.
        sort_key = sort_by or "created_at"
        if sort_key not in self._SORT_COLUMNS:
            raise ValueError(f"invalid_sort_key: {sort_key!r}")
        col_expr = self._SORT_COLUMNS[sort_key]
        order = "ASC" if (sort_order or "desc").lower() == "asc" else "DESC"

        sql = (
            f"SELECT s.*, "
            f"  e.visibility_status AS entity_visibility_status, "
            f"  e.version_history   AS entity_version_history, "
            f"  e.version_no        AS entity_version_no "
            f"FROM store_submissions s "
            f"LEFT JOIN store_entities e ON e.id = s.entity_id "
            f"{where} "
            f"ORDER BY {col_expr} {order}, s.id "
            f"LIMIT {int(limit)} OFFSET {int(skip)}"
        )
        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return [], int(total)
        columns = [d[0] for d in self.conn.description]
        items = [self._row_to_dict(columns, r) for r in rows]
        # Derive ``version_no`` for each row by matching the submission's
        # version hash against the entity's version_history. Surfaces a
        # human-friendly v1/v2/v3 label in the admin queue + detail page
        # (the raw ``s.version`` is the hash). Falls back to None when
        # the entity is gone (hard-deleted) or the hash isn't in history.
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
            sub_hash = item.get("version")
            for entry in history:
                try:
                    if entry.get("hash") == sub_hash:
                        item["version_no"] = int(entry.get("n"))
                        break
                except (TypeError, ValueError):
                    continue
        return items, int(total)
