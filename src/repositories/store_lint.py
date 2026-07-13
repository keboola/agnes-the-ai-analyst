"""Repository for the skill linter's persisted state (v89).

Four tables:

* ``store_lint_runs`` — one row per lint pass (``trigger`` in
  ``scheduler`` | ``admin`` | ``publish``).
* ``store_lint_findings`` — the *current* generation of findings per store
  entity. ``replace_findings`` deletes the entity's prior generation and
  inserts the new one atomically — no history is retained here (the
  dismissals table below is the audit trail for admin actions).
* ``store_lint_dismissals`` — per-``(entity_id, rule_id)`` admin dismissal,
  keyed to the ``content_hash`` it was dismissed against. A subsequent
  content change makes the stored hash stale, which auto-resets the
  dismissal (the finding reappears) without any extra bookkeeping.
* ``store_lint_entity_state`` — last-lint marker per entity, upserted on
  every ``replace_findings`` (even a clean, zero-finding lint) so the
  unchanged-content skip can read the hash for clean entities too.

``LintFinding`` dicts (``src/store_guardrails/skill_lint.py``) carry
``rule_id``, ``severity``, ``message``, ``evidence`` (dict), ``doc_url``.
``evidence`` is JSON-serialized on write and parsed back to a dict on read
so the public surface returns the same shape on both engines.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import duckdb

_FINDING_COLS = "id, run_id, entity_id, rule_id, severity, message, evidence, doc_url, content_hash, created_at"
_RUN_COLS = "id, trigger, started_at, finished_at, entities_linted, entities_skipped, findings_count"


class StoreLintRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    # -- runs ---------------------------------------------------------

    def start_run(self, trigger: str) -> str:
        run_id = f"slr_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO store_lint_runs (id, trigger, started_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            [run_id, trigger],
        )
        return run_id

    def finish_run(self, run_id: str, *, linted: int, skipped: int, findings: int) -> None:
        self.conn.execute(
            "UPDATE store_lint_runs SET finished_at = CURRENT_TIMESTAMP, "
            "entities_linted = ?, entities_skipped = ?, findings_count = ? WHERE id = ?",
            [linted, skipped, findings, run_id],
        )

    def last_run(self, trigger: Optional[str] = None) -> Optional[Dict[str, Any]]:
        sql = f"SELECT {_RUN_COLS} FROM store_lint_runs"
        params: List[Any] = []
        if trigger is not None:
            sql += " WHERE trigger = ?"
            params.append(trigger)
        sql += " ORDER BY started_at DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return self._run_to_dict(row) if row else None

    # -- findings -------------------------------------------------------

    def replace_findings(
        self,
        entity_id: str,
        run_id: str,
        findings: List[Dict[str, Any]],
        content_hash: str,
    ) -> None:
        """Delete the entity's previous findings, insert the new generation.

        Also upserts ``store_lint_entity_state`` — even for an empty
        ``findings`` list — so the unchanged-content skip works for clean
        entities (a findings-only hash read would return nothing after a
        clean lint and force a re-lint every pass).
        """
        self.conn.execute("DELETE FROM store_lint_findings WHERE entity_id = ?", [entity_id])
        self.conn.execute(
            "INSERT INTO store_lint_entity_state (entity_id, content_hash, run_id, linted_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT (entity_id) DO UPDATE SET "
            "content_hash = EXCLUDED.content_hash, run_id = EXCLUDED.run_id, linted_at = EXCLUDED.linted_at",
            [entity_id, content_hash, run_id],
        )
        for finding in findings:
            self.conn.execute(
                "INSERT INTO store_lint_findings "
                "(id, run_id, entity_id, rule_id, severity, message, evidence, doc_url, content_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                [
                    f"slf_{uuid.uuid4().hex[:12]}",
                    run_id,
                    entity_id,
                    finding["rule_id"],
                    finding["severity"],
                    finding["message"],
                    json.dumps(finding.get("evidence") or {}),
                    finding.get("doc_url") or "",
                    content_hash,
                ],
            )

    def carry_forward(self, entity_id: str, new_run_id: str) -> None:
        """Re-tag the entity's existing findings to ``new_run_id`` without
        touching their content — used when a re-lint skips an entity whose
        content hasn't changed but the findings should still be attributed
        to the latest run."""
        self.conn.execute(
            "UPDATE store_lint_findings SET run_id = ? WHERE entity_id = ?",
            [new_run_id, entity_id],
        )

    def latest_findings(self, entity_id: str, *, include_dismissed: bool = True) -> List[Dict[str, Any]]:
        sql = f"SELECT {_FINDING_COLS} FROM store_lint_findings WHERE entity_id = ?"
        params: List[Any] = [entity_id]
        if not include_dismissed:
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM store_lint_dismissals d "
                "WHERE d.entity_id = store_lint_findings.entity_id "
                "AND d.rule_id = store_lint_findings.rule_id "
                "AND d.content_hash = store_lint_findings.content_hash)"
            )
        sql += " ORDER BY created_at"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._finding_to_dict(r) for r in rows]

    def all_latest_findings(self, *, include_dismissed: bool = False) -> List[Dict[str, Any]]:
        sql = f"SELECT {_FINDING_COLS} FROM store_lint_findings"
        if not include_dismissed:
            sql += (
                " WHERE NOT EXISTS (SELECT 1 FROM store_lint_dismissals d "
                "WHERE d.entity_id = store_lint_findings.entity_id "
                "AND d.rule_id = store_lint_findings.rule_id "
                "AND d.content_hash = store_lint_findings.content_hash)"
            )
        sql += " ORDER BY entity_id, created_at"
        rows = self.conn.execute(sql).fetchall()
        return [self._finding_to_dict(r) for r in rows]

    def last_content_hash(self, entity_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM store_lint_entity_state WHERE entity_id = ?",
            [entity_id],
        ).fetchone()
        return row[0] if row else None

    def delete_for_entity(self, entity_id: str) -> None:
        """Store delete hook parity — purge lint state for a hard-deleted entity."""
        self.conn.execute("DELETE FROM store_lint_findings WHERE entity_id = ?", [entity_id])
        self.conn.execute("DELETE FROM store_lint_dismissals WHERE entity_id = ?", [entity_id])
        self.conn.execute("DELETE FROM store_lint_entity_state WHERE entity_id = ?", [entity_id])

    # -- dismissals -------------------------------------------------------

    def dismiss(self, entity_id: str, rule_id: str, user_id: str, content_hash: str) -> None:
        self.conn.execute(
            "INSERT INTO store_lint_dismissals (entity_id, rule_id, dismissed_by, dismissed_at, content_hash) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?) "
            "ON CONFLICT (entity_id, rule_id) DO UPDATE SET "
            "dismissed_by = excluded.dismissed_by, dismissed_at = excluded.dismissed_at, "
            "content_hash = excluded.content_hash",
            [entity_id, rule_id, user_id, content_hash],
        )

    def is_dismissed(self, entity_id: str, rule_id: str, content_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT content_hash FROM store_lint_dismissals WHERE entity_id = ? AND rule_id = ?",
            [entity_id, rule_id],
        ).fetchone()
        if row is None:
            return False
        return row[0] == content_hash

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _finding_to_dict(row) -> Dict[str, Any]:
        keys = (
            "id",
            "run_id",
            "entity_id",
            "rule_id",
            "severity",
            "message",
            "evidence",
            "doc_url",
            "content_hash",
            "created_at",
        )
        d = dict(zip(keys, row))
        if isinstance(d.get("evidence"), str):
            try:
                d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else {}
            except (ValueError, TypeError):
                d["evidence"] = {}
        return d

    @staticmethod
    def _run_to_dict(row) -> Dict[str, Any]:
        keys = (
            "id",
            "trigger",
            "started_at",
            "finished_at",
            "entities_linted",
            "entities_skipped",
            "findings_count",
        )
        return dict(zip(keys, row))
