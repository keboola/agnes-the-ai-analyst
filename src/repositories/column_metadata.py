"""Repository for column metadata."""

import json
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class ColumnMetadataRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def save(
        self,
        table_id: str,
        column_name: str,
        basetype: Optional[str] = None,
        description: Optional[str] = None,
        confidence: str = "manual",
        source: str = "manual",
    ) -> dict:
        """Insert or update column metadata. Returns the saved record."""
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO column_metadata (table_id, column_name, basetype, description, confidence, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (table_id, column_name) DO UPDATE SET
                basetype = excluded.basetype,
                description = excluded.description,
                confidence = excluded.confidence,
                source = excluded.source,
                updated_at = excluded.updated_at""",
            [table_id, column_name, basetype, description, confidence, source, now],
        )
        return self.get(table_id, column_name)

    def get(self, table_id: str, column_name: str) -> Optional[Dict[str, Any]]:
        """Select by composite PK. Returns None if not found."""
        result = self.conn.execute(
            "SELECT * FROM column_metadata WHERE table_id = ? AND column_name = ?",
            [table_id, column_name],
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_for_table(self, table_id: str) -> List[Dict[str, Any]]:
        """Select all columns for a table, ordered by column_name."""
        results = self.conn.execute(
            "SELECT * FROM column_metadata WHERE table_id = ? ORDER BY column_name",
            [table_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def delete(self, table_id: str, column_name: str) -> bool:
        """Delete column metadata. Returns True if a row was deleted."""
        before = self.conn.execute(
            "SELECT COUNT(*) FROM column_metadata WHERE table_id = ? AND column_name = ?",
            [table_id, column_name],
        ).fetchone()[0]
        if before == 0:
            return False
        self.conn.execute(
            "DELETE FROM column_metadata WHERE table_id = ? AND column_name = ?",
            [table_id, column_name],
        )
        return True

    def import_proposal(self, proposal_path: str) -> int:
        """Import a proposal JSON file.

        Format:
            {
                "tables": {
                    "orders": {
                        "columns": {
                            "id": {"basetype": "STRING", "description": "...", "confidence": "high"}
                        }
                    }
                }
            }

        Sets source="ai_enrichment". Returns count of columns imported.
        """
        with open(proposal_path, "r", encoding="utf-8") as f:
            proposal = json.load(f)

        count = 0
        tables = proposal.get("tables", {})
        for table_id, table_data in tables.items():
            columns = table_data.get("columns", {})
            for column_name, col_data in columns.items():
                self.save(
                    table_id=table_id,
                    column_name=column_name,
                    basetype=col_data.get("basetype"),
                    description=col_data.get("description"),
                    confidence=col_data.get("confidence", "high"),
                    source="ai_enrichment",
                )
                count += 1
        return count
