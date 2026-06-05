"""Repository for ``store_submissions`` — flea-market guardrail audit trail.

Every POST/PUT to ``/api/store/entities`` writes a row here capturing the
inline-check verdicts and (asynchronously) the LLM security review outcome.
Powers ``/admin/store/submissions`` and the override workflow. See
``src/store_guardrails/`` for the check pipeline that fills these rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
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
        verdict is ``blocked_llm`` or ``review_error`` newer than
        ``since`` (a ``datetime`` — typically now - 24h). Called from
        the POST entry point; refusal bounds the load placed on the
        async LLM reviewer by a single bot looping risky bundles.

        Inline failures (manifest/content validation, static-security
        deny-list) are hard-rejected upstream without creating a
        submission row — they don't consume the LLM-tier quota.
        Slowapi rate limits + the audit_log
        ``store.upload.security_blocked`` trail cover that path.

        Historical note: pre-#9 this counted only ``blocked_inline``;
        a bot triggering ``blocked_llm`` verdicts was unbounded.
        Post-#9 it widened to all three. The current incarnation
        narrows back to LLM-tier states since inline failures no
        longer create rows. Legacy ``blocked_inline`` rows in DBs that
        ran the v30 contract are still present (historical audit) but
        intentionally excluded from the live counter.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) FROM store_submissions "
            "WHERE submitter_id = ? "
            "  AND status IN ('blocked_llm', 'review_error') "
            "  AND created_at >= ?",
            [submitter_id, since],
        ).fetchone()
        return int(row[0]) if row else 0

    # Backward-compat alias — still used in some operator scripts.
    # Routes to the broader counter post-#9.
    count_blocked_inline_for_submitter_since = count_blocked_for_submitter_since

    # Terminal states whose `status` should never be silently overwritten
    # by an asynchronous (BG-task) writer. Admin-triggered actions
    # (override, delete) call dedicated repo methods or set
    # ``allow_terminal_overwrite=True`` explicitly. The BG-task path
    # in ``runner.run_llm_review`` calls ``update_status`` without that
    # flag — so a late LLM verdict racing with an admin override OR
    # with a more recent terminal verdict can no longer clobber the
    # row.
    _TERMINAL_STATUSES = frozenset({"approved", "overridden", "blocked_inline"})

    def update_status(
        self,
        id: str,
        *,
        status: str,
        llm_findings: Optional[Dict[str, Any]] = None,
        reviewed_by_model: Optional[str] = None,
        allow_terminal_overwrite: bool = False,
    ) -> bool:
        """Update a submission's status. Returns ``True`` when the row
        was actually updated, ``False`` when a compare-and-swap skipped
        the write because the row had already moved to a terminal state.

        The CAS protects against the BG-task race surfaced by the
        adversarial review of PR #316: a late LLM verdict could
        previously clobber ``status='overridden'`` (admin force-published
        the submission while the LLM was still running). With the guard,
        BG callers no-op on terminal rows; admin paths still call
        ``set_override`` etc. which write unconditionally.
        """
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
        where_clauses = ["id = ?"]
        params.append(id)
        if not allow_terminal_overwrite:
            placeholders = ",".join("?" for _ in self._TERMINAL_STATUSES)
            where_clauses.append(f"status NOT IN ({placeholders})")
            params.extend(self._TERMINAL_STATUSES)
        sql = (
            f"UPDATE store_submissions SET {', '.join(sets)} "
            f"WHERE {' AND '.join(where_clauses)}"
        )
        result = self.conn.execute(sql, params)
        # DuckDB returns a relation with the rowcount in row 0, col 0
        # for an UPDATE. fetchone() is the portable way to read it.
        try:
            row = result.fetchone()
            rowcount = int(row[0]) if row else 0
        except Exception:
            rowcount = 0
        return rowcount > 0

    def set_inline_result(
        self,
        id: str,
        *,
        inline_checks: Optional[Dict[str, Any]],
        status: str,
    ) -> None:
        """Admin rescan writeback: replace ``inline_checks``, clear any prior
        ``llm_findings``, and set ``status``.

        Unconditional (admin-triggered), unlike ``update_status`` which guards
        terminal states with a CAS — a rescan must be able to flip an already
        'approved' row back to 'blocked_inline'. Backs the /admin store rescan
        route on both backends (was a raw ``subs.conn.execute`` that broke on PG).
        """
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid submission status: {status!r}")
        self.conn.execute(
            "UPDATE store_submissions "
            "   SET inline_checks = ?, llm_findings = NULL, "
            "       status = ?, updated_at = ? "
            " WHERE id = ?",
            [
                json.dumps(inline_checks) if inline_checks is not None else None,
                status,
                datetime.now(timezone.utc),
                id,
            ],
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

    def reap_stuck_pending_llm(
        self,
        *,
        grace_seconds: int,
        error_payload: Dict[str, Any],
    ) -> List[Tuple[str, str]]:
        """Flip every ``pending_llm`` row older than ``grace_seconds`` to
        ``review_error``, stamping ``llm_findings`` with ``error_payload``.

        Returns ``[(submission_id, submitter_id), …]`` for the rows that
        were actually flipped so the caller can write one audit row each.
        Scoped strictly to ``pending_llm`` — the per-row CAS in the WHERE
        clause keeps it idempotent and never touches a row that another
        worker resolved in between. Engine-specific SQL lives here (not in
        the reaper) so the Postgres sibling can mirror it; see
        ``store_submissions_pg.StoreSubmissionsPgRepository``.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
        rows = self.conn.execute(
            """SELECT id, submitter_id
                 FROM store_submissions
                WHERE status = 'pending_llm'
                  AND created_at < ?""",
            [cutoff],
        ).fetchall()
        if not rows:
            return []
        now = datetime.now(timezone.utc)
        payload = json.dumps(error_payload)
        reaped: List[Tuple[str, str]] = []
        for sub_id, submitter_id in rows:
            self.conn.execute(
                """UPDATE store_submissions
                      SET status = 'review_error',
                          llm_findings = ?,
                          updated_at = ?
                    WHERE id = ?
                      AND status = 'pending_llm'""",
                [payload, now, sub_id],
            )
            reaped.append((sub_id, submitter_id))
        return reaped

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
            # Look up THIS submission's version_no by submission_id,
            # NOT by hash. Hash-based lookup mislabeled every
            # byte-identical reupload (and every reused-verdict
            # restore — common after PR #332) as v1 because the loop
            # picked the FIRST history entry with matching hash.
            # Same fix-pattern as PR #330 for runner / override.
            sub_id = item.get("id")
            for entry in history:
                try:
                    if entry.get("submission_id") == sub_id:
                        item["version_no"] = int(entry.get("n"))
                        break
                except (TypeError, ValueError):
                    continue
        return items, int(total)
