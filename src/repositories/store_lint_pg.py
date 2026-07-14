"""Postgres-backed repository for the skill linter's persisted state (v89).

Mirrors ``src/repositories/store_lint.py`` (the DuckDB impl) on the
``StoreLintRepository`` public surface. Cross-engine parity is covered by
``tests/db_pg/test_store_lint_contract.py``.

The ``evidence`` column is JSON text on both engines; we serialize on write
and parse on read so the public surface returns a Python dict on both.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_FINDING_COLS = "id, run_id, entity_id, rule_id, severity, message, evidence, doc_url, content_hash, created_at"
_RUN_COLS = "id, trigger, started_at, finished_at, entities_linted, entities_skipped, findings_count"


class StoreLintPgRepository:
    """Postgres twin of ``StoreLintRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    # -- runs ---------------------------------------------------------

    def start_run(self, trigger: str) -> str:
        run_id = f"slr_{uuid.uuid4().hex[:12]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO store_lint_runs (id, trigger, started_at) VALUES (:id, :trigger, CURRENT_TIMESTAMP)"
                ),
                {"id": run_id, "trigger": trigger},
            )
        return run_id

    def finish_run(self, run_id: str, *, linted: int, skipped: int, findings: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE store_lint_runs SET finished_at = CURRENT_TIMESTAMP, "
                    "entities_linted = :linted, entities_skipped = :skipped, "
                    "findings_count = :findings WHERE id = :id"
                ),
                {"linted": linted, "skipped": skipped, "findings": findings, "id": run_id},
            )

    def last_run(self, trigger: Optional[str] = None) -> Optional[Dict[str, Any]]:
        sql = f"SELECT {_RUN_COLS} FROM store_lint_runs"
        params: Dict[str, Any] = {}
        if trigger is not None:
            sql += " WHERE trigger = :trigger"
            params["trigger"] = trigger
        sql += " ORDER BY started_at DESC LIMIT 1"
        with self._engine.connect() as conn:
            row = conn.execute(sa.text(sql), params).fetchone()
        return self._run_to_dict(row) if row else None

    def last_full_audit_run(self) -> Optional[Dict[str, Any]]:
        """Most recent full-corpus audit run — ``scheduler`` or ``admin`` only.

        The audit self-guard must ignore per-publish (``trigger='publish'``)
        runs: those fire on every skill publish, so counting them would let
        routine publishing perpetually reset the interval and starve the
        scheduled retro-audit.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"SELECT {_RUN_COLS} FROM store_lint_runs "
                    "WHERE trigger IN ('scheduler', 'admin') ORDER BY started_at DESC LIMIT 1"
                )
            ).fetchone()
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
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM store_lint_findings WHERE entity_id = :entity_id"),
                {"entity_id": entity_id},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO store_lint_entity_state (entity_id, content_hash, run_id, linted_at) "
                    "VALUES (:entity_id, :content_hash, :run_id, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (entity_id) DO UPDATE SET "
                    "content_hash = EXCLUDED.content_hash, run_id = EXCLUDED.run_id, "
                    "linted_at = EXCLUDED.linted_at"
                ),
                {"entity_id": entity_id, "content_hash": content_hash, "run_id": run_id},
            )
            for finding in findings:
                conn.execute(
                    sa.text(
                        "INSERT INTO store_lint_findings "
                        "(id, run_id, entity_id, rule_id, severity, message, evidence, doc_url, "
                        "content_hash, created_at) "
                        "VALUES (:id, :run_id, :entity_id, :rule_id, :severity, :message, :evidence, "
                        ":doc_url, :content_hash, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": f"slf_{uuid.uuid4().hex[:12]}",
                        "run_id": run_id,
                        "entity_id": entity_id,
                        "rule_id": finding["rule_id"],
                        "severity": finding["severity"],
                        "message": finding["message"],
                        "evidence": json.dumps(finding.get("evidence") or {}),
                        "doc_url": finding.get("doc_url") or "",
                        "content_hash": content_hash,
                    },
                )

    def carry_forward(self, entity_id: str, new_run_id: str) -> None:
        """Re-tag the entity's existing findings to ``new_run_id`` without
        touching their content."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE store_lint_findings SET run_id = :run_id WHERE entity_id = :entity_id"),
                {"run_id": new_run_id, "entity_id": entity_id},
            )

    def latest_findings(self, entity_id: str, *, include_dismissed: bool = True) -> List[Dict[str, Any]]:
        sql = f"SELECT {_FINDING_COLS} FROM store_lint_findings WHERE entity_id = :entity_id"
        if not include_dismissed:
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM store_lint_dismissals d "
                "WHERE d.entity_id = store_lint_findings.entity_id "
                "AND d.rule_id = store_lint_findings.rule_id "
                "AND d.content_hash = store_lint_findings.content_hash)"
            )
        sql += " ORDER BY created_at"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), {"entity_id": entity_id}).fetchall()
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
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql)).fetchall()
        return [self._finding_to_dict(r) for r in rows]

    def last_content_hash(self, entity_id: str) -> Optional[str]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT content_hash FROM store_lint_entity_state WHERE entity_id = :entity_id"),
                {"entity_id": entity_id},
            ).fetchone()
        return row[0] if row else None

    def delete_for_entity(self, entity_id: str) -> None:
        """Store delete hook parity — purge lint state for a hard-deleted entity."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM store_lint_findings WHERE entity_id = :entity_id"),
                {"entity_id": entity_id},
            )
            conn.execute(
                sa.text("DELETE FROM store_lint_dismissals WHERE entity_id = :entity_id"),
                {"entity_id": entity_id},
            )
            conn.execute(
                sa.text("DELETE FROM store_lint_entity_state WHERE entity_id = :entity_id"),
                {"entity_id": entity_id},
            )

    # -- dismissals -------------------------------------------------------

    def dismiss(self, entity_id: str, rule_id: str, user_id: str, content_hash: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO store_lint_dismissals (entity_id, rule_id, dismissed_by, dismissed_at, content_hash) "
                    "VALUES (:entity_id, :rule_id, :user_id, CURRENT_TIMESTAMP, :content_hash) "
                    "ON CONFLICT (entity_id, rule_id) DO UPDATE SET "
                    "dismissed_by = excluded.dismissed_by, dismissed_at = excluded.dismissed_at, "
                    "content_hash = excluded.content_hash"
                ),
                {
                    "entity_id": entity_id,
                    "rule_id": rule_id,
                    "user_id": user_id,
                    "content_hash": content_hash,
                },
            )

    def is_dismissed(self, entity_id: str, rule_id: str, content_hash: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT content_hash FROM store_lint_dismissals WHERE entity_id = :entity_id AND rule_id = :rule_id"
                ),
                {"entity_id": entity_id, "rule_id": rule_id},
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
