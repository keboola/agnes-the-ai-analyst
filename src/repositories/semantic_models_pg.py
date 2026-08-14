"""Postgres-backed repository for ``semantic_models`` + ``data_package_semantic_models``.

Mirrors ``src/repositories/semantic_models.py`` (the DuckDB impl). Implementation
differences vs. DuckDB:

- ``document_json`` / ``validation_errors`` are JSONB columns; writes go through
  ``CAST(:p AS JSONB)`` with a ``json.dumps(value)`` bind, reads come back as
  native Python dicts/lists via psycopg's adapter — no manual ``json.loads``
  round-trip on the read path.
- ``upsert`` follows the same DELETE-then-INSERT shape as the DuckDB sibling
  (not ``ON CONFLICT``): the natural key is ``(source, source_ref, slug)``, and
  SQL NULL is never equal to itself, so an ``ON CONFLICT`` target on that tuple
  would silently miss every row with a NULL ``source_ref``. Both statements run
  inside one transaction.
- DuckDB's ``UNNEST(?::VARCHAR[])`` becomes Postgres's ``= ANY(CAST(:keep AS
  TEXT[]))``; the explicit cast keeps an empty Python list from raising
  "could not determine data type of parameter" on the driver side.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_JSON_COLS = ("document_json", "validation_errors")


def _json_param(v: Optional[Any]) -> Optional[str]:
    return None if v is None else json.dumps(v)


class SemanticModelsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(row)
        for k in _JSON_COLS:
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v) if v else None
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def upsert(
        self,
        *,
        id,
        slug,
        name,
        description,
        document,
        document_json,
        spec_version,
        content_hash,
        source,
        source_ref,
        status,
        validation_errors,
        validated_at,
    ) -> Dict[str, Any]:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM semantic_models WHERE source = :source "
                    "AND source_ref IS NOT DISTINCT FROM :source_ref AND slug = :slug"
                ),
                {"source": source, "source_ref": source_ref, "slug": slug},
            )
            conn.execute(
                sa.text(
                    """
                    INSERT INTO semantic_models
                      (id, slug, name, description, document, document_json,
                       spec_version, content_hash, source, source_ref, status,
                       validation_errors, validated_at, created_at, updated_at)
                    VALUES
                      (:id, :slug, :name, :description, :document,
                       CAST(:document_json AS JSONB),
                       :spec_version, :content_hash, :source, :source_ref, :status,
                       CAST(:validation_errors AS JSONB), :validated_at,
                       current_timestamp, current_timestamp)
                    """
                ),
                {
                    "id": id,
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "document": document,
                    "document_json": _json_param(document_json),
                    "spec_version": spec_version,
                    "content_hash": content_hash,
                    "source": source,
                    "source_ref": source_ref,
                    "status": status,
                    "validation_errors": _json_param(validation_errors),
                    "validated_at": validated_at,
                },
            )
        return self.get(id)  # type: ignore[return-value]

    def get(self, model_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM semantic_models WHERE id = :id"),
                    {"id": model_id},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM semantic_models WHERE slug = :slug ORDER BY updated_at DESC"),
                    {"slug": slug},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def list_all(self, *, source: Optional[str] = None, source_ref: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM semantic_models"
        clauses = []
        params: Dict[str, Any] = {}
        if source is not None:
            clauses.append("source = :source")
            params["source"] = source
        if source_ref is not None:
            clauses.append("source_ref IS NOT DISTINCT FROM :source_ref")
            params["source_ref"] = source_ref
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def delete(self, model_id: str) -> bool:
        existed = self.get(model_id) is not None
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM data_package_semantic_models WHERE model_id = :id"),
                {"id": model_id},
            )
            conn.execute(
                sa.text("DELETE FROM semantic_models WHERE id = :id"),
                {"id": model_id},
            )
        return existed

    def delete_missing(self, *, source: str, source_ref: Optional[str], keep_slugs: List[str]) -> List[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id FROM semantic_models "
                    "WHERE source = :source AND source_ref IS NOT DISTINCT FROM :source_ref "
                    "  AND NOT (slug = ANY(CAST(:keep AS TEXT[]))) ORDER BY id"
                ),
                {"source": source, "source_ref": source_ref, "keep": list(keep_slugs)},
            ).all()
        ids = [r[0] for r in rows]
        for model_id in ids:
            self.delete(model_id)
        return ids

    def link_package(self, package_id: str, model_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM data_package_semantic_models WHERE package_id = :pkg AND model_id = :model"),
                {"pkg": package_id, "model": model_id},
            )
            conn.execute(
                sa.text("INSERT INTO data_package_semantic_models (package_id, model_id) VALUES (:pkg, :model)"),
                {"pkg": package_id, "model": model_id},
            )

    def unlink_package(self, package_id: str, model_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM data_package_semantic_models WHERE package_id = :pkg AND model_id = :model"),
                {"pkg": package_id, "model": model_id},
            )

    def list_for_package(self, package_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT m.* FROM semantic_models m "
                        "JOIN data_package_semantic_models j ON j.model_id = m.id "
                        "WHERE j.package_id = :pkg ORDER BY m.name"
                    ),
                    {"pkg": package_id},
                )
                .mappings()
                .all()
            )
        return [self._decode_row(dict(r)) for r in rows]
