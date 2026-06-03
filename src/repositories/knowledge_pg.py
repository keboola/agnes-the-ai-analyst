"""Postgres-backed knowledge repository.

Mirrors ``src/repositories/knowledge.py``. Key differences:

  - JSON columns (``tags``, ``entities``, ``contributors``) are JSONB —
    psycopg returns them as native Python list/dict, so the legacy
    string-decoding path in ``_normalize_row`` only fires defensively.

  - Full-text search uses Postgres ``to_tsvector('english', title || ' ' || content)``
    with ``plainto_tsquery`` and ``ts_rank`` for ranking, instead of
    DuckDB's BM25 extension. Falls back to ``ILIKE`` when an FTS execute
    raises. Same overall shape (search → results sorted by score) and
    same filter surface as the DuckDB original.

  - ``json_each`` is identical in PG.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


logger = logging.getLogger(__name__)


class KnowledgePgRepository:
    _JSON_LIST_COLUMNS = ("tags", "entities")
    _UPDATABLE_FIELDS = {
        "title", "content", "category", "tags", "domain", "entities",
        "source_type", "source_ref", "source_user", "audience",
        "confidence", "status", "sensitivity", "is_personal",
        "valid_from", "valid_until", "supersedes",
    }
    _JSON_BIND_COLUMNS = {"tags", "entities", "contributors"}

    def __init__(self, engine: Engine):
        self._engine = engine

    @classmethod
    def _normalize_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        for col in cls._JSON_LIST_COLUMNS:
            v = row.get(col)
            if v is None:
                row[col] = []
            elif isinstance(v, list):
                continue
            elif isinstance(v, str):
                try:
                    parsed = json.loads(v) if v else []
                except (ValueError, TypeError):
                    parsed = []
                row[col] = parsed if isinstance(parsed, list) else []
            else:
                row[col] = []
        return row

    def _rows(self, rows) -> List[Dict[str, Any]]:
        return [self._normalize_row(dict(r)) for r in rows]

    # ----- core item CRUD -----

    def get_by_id(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM knowledge_items WHERE id = :id"),
                {"id": item_id},
            ).mappings().first()
        return self._normalize_row(dict(row)) if row else None

    def get_by_ids(self, item_ids: List[str]) -> Dict[str, Any]:
        if not item_ids:
            return {}
        id_keys = []
        params: Dict[str, Any] = {}
        for i, iid in enumerate(item_ids):
            k = f"id_{i}"
            id_keys.append(f":{k}")
            params[k] = iid
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"SELECT * FROM knowledge_items WHERE id IN ({','.join(id_keys)})"
                ),
                params,
            ).mappings().all()
        return {r["id"]: self._normalize_row(dict(r)) for r in rows}

    def create(
        self,
        id: str,
        title: str,
        content: str,
        category: str,
        source_user: Optional[str] = None,
        tags: Optional[List[str]] = None,
        status: str = "pending",
        confidence: Optional[float] = None,
        domain: Optional[str] = None,
        entities: Optional[List[str]] = None,
        source_type: str = "claude_local_md",
        source_ref: Optional[str] = None,
        valid_from: Optional[datetime] = None,
        valid_until: Optional[datetime] = None,
        supersedes: Optional[str] = None,
        sensitivity: str = "internal",
        is_personal: bool = False,
        is_required: bool = False,
    ) -> None:
        # NOTE (parity): the DuckDB repo also accepts ``added_by``, used to
        # attribute the domain link in its knowledge_item_domains join table.
        # This PG repo stores ``domain`` inline on knowledge_items (no join
        # table in this path), so there is no separate attribution to record;
        # ``added_by`` is intentionally not part of this signature. ``is_required``
        # IS a real column and is wired below.
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO knowledge_items (
                        id, title, content, category, source_user, tags, status,
                        confidence, domain, entities, source_type, source_ref,
                        valid_from, valid_until, supersedes, sensitivity, is_personal,
                        is_required, created_at, updated_at
                    ) VALUES (:id, :title, :content, :category, :su,
                              CAST(:tags AS JSONB), :status,
                              :confidence, :domain,
                              CAST(:entities AS JSONB),
                              :st, :sr,
                              :vf, :vu, :sup, :sens, :ip,
                              :ireq, :now, :now)"""
                ),
                {
                    "id": id, "title": title, "content": content,
                    "category": category, "su": source_user,
                    "tags": json.dumps(tags) if tags else None,
                    "status": status, "confidence": confidence,
                    "domain": domain,
                    "entities": json.dumps(entities) if entities else None,
                    "st": source_type, "sr": source_ref,
                    "vf": valid_from, "vu": valid_until, "sup": supersedes,
                    "sens": sensitivity, "ip": is_personal,
                    "ireq": is_required, "now": now,
                },
            )

    def set_is_required(self, item_id: str, value: bool) -> None:
        """Toggle the global ``is_required`` flag without touching ``status``
        (parity with the DuckDB repo). ``status`` stays reserved for lifecycle;
        the Required tier rides on this boolean.
        """
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE knowledge_items SET is_required = :v, updated_at = :now "
                    "WHERE id = :id"
                ),
                {"v": bool(value), "now": now, "id": item_id},
            )

    def update(self, item_id: str, **fields) -> None:
        safe = {k: v for k, v in fields.items() if k in self._UPDATABLE_FIELDS}
        if not safe:
            return
        now = datetime.now(timezone.utc)
        sets: List[str] = []
        params: Dict[str, Any] = {"item_id": item_id, "updated_at": now}
        for k, v in safe.items():
            if k in self._JSON_BIND_COLUMNS and v is not None and not isinstance(v, str):
                v = json.dumps(v)
            if k in self._JSON_BIND_COLUMNS:
                sets.append(f"{k} = CAST(:{k} AS JSONB)")
            else:
                sets.append(f"{k} = :{k}")
            params[k] = v
        sets.append("updated_at = :updated_at")
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"UPDATE knowledge_items SET {', '.join(sets)} WHERE id = :item_id"
                ),
                params,
            )

    def update_status(self, item_id: str, status: str) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE knowledge_items SET status = :s, updated_at = :now WHERE id = :id"
                ),
                {"s": status, "now": now, "id": item_id},
            )

    # ----- list / search / count -----

    def list_items(
        self,
        statuses: Optional[List[str]] = None,
        category: Optional[str] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
        upvoted_by_user: Optional[str] = None,
        dismissed_by_user: Optional[str] = None,
        hide_dismissed: bool = False,
        limit: int = 100,
        offset: int = 0,
        is_required: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        sql_parts = ["SELECT * FROM knowledge_items WHERE 1=1"]
        params: Dict[str, Any] = {"limit": limit, "offset": offset}

        if is_required is not None:
            sql_parts.append(" AND is_required = :ireq"); params["ireq"] = bool(is_required)
        if statuses:
            keys = []
            for i, s in enumerate(statuses):
                k = f"st_{i}"; keys.append(f":{k}"); params[k] = s
            sql_parts.append(f" AND status IN ({','.join(keys)})")
        if category:
            sql_parts.append(" AND category = :category"); params["category"] = category
        if upvoted_by_user:
            sql_parts.append(
                " AND id IN (SELECT item_id FROM knowledge_votes "
                "WHERE user_id = :upv AND vote > 0)"
            )
            params["upv"] = upvoted_by_user
        if domain:
            sql_parts.append(" AND domain = :domain"); params["domain"] = domain
        if source_type:
            sql_parts.append(" AND source_type = :st_param"); params["st_param"] = source_type
        if exclude_personal:
            sql_parts.append(" AND (is_personal = FALSE OR is_personal IS NULL)")
        if user_groups is not None:
            visibility = ["audience IS NULL", "audience = 'all'"]
            if user_groups:
                keys = []
                for i, g in enumerate(user_groups):
                    k = f"ug_{i}"; keys.append(f":{k}"); params[k] = g
                visibility.append(f"audience IN ({','.join(keys)})")
            if granted_domains:
                keys = []
                for i, d in enumerate(granted_domains):
                    k = f"gd_{i}"; keys.append(f":{k}"); params[k] = d
                visibility.append(f"domain IN ({','.join(keys)})")
            sql_parts.append(" AND (" + " OR ".join(visibility) + ")")
        if hide_dismissed and dismissed_by_user:
            sql_parts.append(
                " AND NOT EXISTS ("
                " SELECT 1 FROM knowledge_item_user_dismissed d"
                " WHERE d.item_id = knowledge_items.id"
                "   AND d.user_id = :dbu"
                "   AND knowledge_items.status != 'mandatory'"
                ")"
            )
            params["dbu"] = dismissed_by_user
        sql_parts.append(" ORDER BY updated_at DESC LIMIT :limit OFFSET :offset")

        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("".join(sql_parts)), params).mappings().all()
        return self._rows(rows)

    def search(
        self,
        query: str,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        category: Optional[str] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        dismissed_by_user: Optional[str] = None,
        hide_dismissed: bool = False,
        limit: int = 100,
        offset: int = 0,
        is_required: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """FTS via Postgres ``to_tsvector`` + ``plainto_tsquery`` /
        ``ts_rank`` with ILIKE fallback.
        """
        filter_parts, filter_params = self._build_filter_clauses(
            statuses=statuses, category=category, domain=domain,
            source_type=source_type, exclude_personal=exclude_personal,
            user_groups=user_groups, granted_domains=granted_domains,
            dismissed_by_user=dismissed_by_user, hide_dismissed=hide_dismissed,
            is_required=is_required,
        )
        params: Dict[str, Any] = dict(filter_params)
        params["q"] = query
        params["limit"] = limit
        params["offset"] = offset

        fts_sql = (
            "SELECT *, ts_rank("
            "  to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,'')), "
            "  plainto_tsquery('english', :q)"
            ") AS bm25_score FROM knowledge_items "
            "WHERE to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,'')) "
            "  @@ plainto_tsquery('english', :q)"
            + "".join(filter_parts)
            + " ORDER BY bm25_score DESC, updated_at DESC "
              "LIMIT :limit OFFSET :offset"
        )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sa.text(fts_sql), params).mappings().all()
            return self._rows(rows)
        except Exception as e:
            logger.warning("PG FTS failed (%s); falling back to ILIKE", e)
            ilike_sql = (
                "SELECT *, NULL AS bm25_score FROM knowledge_items "
                "WHERE (title ILIKE :pattern OR content ILIKE :pattern)"
                + "".join(filter_parts)
                + " ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
            )
            ilike_params = dict(filter_params)
            ilike_params["pattern"] = f"%{query}%"
            ilike_params["limit"] = limit
            ilike_params["offset"] = offset
            with self._engine.connect() as conn:
                rows = conn.execute(sa.text(ilike_sql), ilike_params).mappings().all()
            return self._rows(rows)

    def count_items(
        self,
        search: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        category: Optional[str] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
        dismissed_by_user: Optional[str] = None,
        hide_dismissed: bool = False,
        is_required: Optional[bool] = None,
    ) -> int:
        filter_parts, filter_params = self._build_filter_clauses(
            statuses=statuses, category=category, domain=domain,
            source_type=source_type, exclude_personal=exclude_personal,
            user_groups=user_groups, granted_domains=granted_domains,
            dismissed_by_user=dismissed_by_user, hide_dismissed=hide_dismissed,
            is_required=is_required,
        )
        if not search:
            sql = "SELECT COUNT(*) FROM knowledge_items WHERE 1=1" + "".join(filter_parts)
            with self._engine.connect() as conn:
                return int(conn.execute(sa.text(sql), filter_params).scalar() or 0)

        params = dict(filter_params)
        params["q"] = search
        fts_sql = (
            "SELECT COUNT(*) FROM knowledge_items "
            "WHERE to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,'')) "
            "  @@ plainto_tsquery('english', :q)"
            + "".join(filter_parts)
        )
        try:
            with self._engine.connect() as conn:
                return int(conn.execute(sa.text(fts_sql), params).scalar() or 0)
        except Exception as e:
            logger.warning("PG FTS count failed (%s); falling back to ILIKE", e)
            ilike_params = dict(filter_params)
            ilike_params["pattern"] = f"%{search}%"
            ilike_sql = (
                "SELECT COUNT(*) FROM knowledge_items "
                "WHERE (title ILIKE :pattern OR content ILIKE :pattern)"
                + "".join(filter_parts)
            )
            with self._engine.connect() as conn:
                return int(conn.execute(sa.text(ilike_sql), ilike_params).scalar() or 0)

    def _build_filter_clauses(
        self,
        *,
        statuses: Optional[List[str]],
        category: Optional[str],
        domain: Optional[str],
        source_type: Optional[str],
        exclude_personal: bool,
        user_groups: Optional[List[str]],
        granted_domains: Optional[List[str]],
        dismissed_by_user: Optional[str],
        hide_dismissed: bool,
        is_required: Optional[bool] = None,
    ) -> tuple[list[str], Dict[str, Any]]:
        parts: List[str] = []
        params: Dict[str, Any] = {}
        if is_required is not None:
            parts.append(" AND is_required = :ireq"); params["ireq"] = bool(is_required)
        if statuses:
            keys = []
            for i, s in enumerate(statuses):
                k = f"st_{i}"; keys.append(f":{k}"); params[k] = s
            parts.append(f" AND status IN ({','.join(keys)})")
        if category:
            parts.append(" AND category = :category"); params["category"] = category
        if domain:
            parts.append(" AND domain = :domain"); params["domain"] = domain
        if source_type:
            parts.append(" AND source_type = :st_param"); params["st_param"] = source_type
        if exclude_personal:
            parts.append(" AND (is_personal = FALSE OR is_personal IS NULL)")
        if user_groups is not None:
            vis = ["audience IS NULL", "audience = 'all'"]
            if user_groups:
                keys = []
                for i, g in enumerate(user_groups):
                    k = f"ug_{i}"; keys.append(f":{k}"); params[k] = g
                vis.append(f"audience IN ({','.join(keys)})")
            if granted_domains:
                keys = []
                for i, d in enumerate(granted_domains):
                    k = f"gd_{i}"; keys.append(f":{k}"); params[k] = d
                vis.append(f"domain IN ({','.join(keys)})")
            parts.append(" AND (" + " OR ".join(vis) + ")")
        if hide_dismissed and dismissed_by_user:
            parts.append(
                " AND NOT EXISTS ("
                " SELECT 1 FROM knowledge_item_user_dismissed d"
                " WHERE d.item_id = knowledge_items.id"
                "   AND d.user_id = :dbu"
                "   AND knowledge_items.status != 'mandatory'"
                ")"
            )
            params["dbu"] = dismissed_by_user
        return parts, params

    # ----- domain / contributions / personal flag -----

    def list_by_domain(
        self,
        domain: str,
        statuses: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        sql_parts = ["SELECT * FROM knowledge_items WHERE domain = :domain"]
        params: Dict[str, Any] = {"domain": domain, "limit": limit}
        if statuses:
            keys = []
            for i, s in enumerate(statuses):
                k = f"st_{i}"; keys.append(f":{k}"); params[k] = s
            sql_parts.append(f" AND status IN ({','.join(keys)})")
        sql_parts.append(" ORDER BY updated_at DESC LIMIT :limit")
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("".join(sql_parts)), params).mappings().all()
        return self._rows(rows)

    def get_user_contributions(self, source_user: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM knowledge_items "
                    "WHERE source_user = :su ORDER BY updated_at DESC"
                ),
                {"su": source_user},
            ).mappings().all()
        return self._rows(rows)

    def set_personal(self, item_id: str, is_personal: bool) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE knowledge_items SET is_personal = :ip, updated_at = :now "
                    "WHERE id = :id"
                ),
                {"ip": is_personal, "now": now, "id": item_id},
            )

    # ----- votes -----

    def vote(self, item_id: str, user_id: str, vote: int) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO knowledge_votes (item_id, user_id, vote, voted_at)
                       VALUES (:i, :u, :v, :now)
                       ON CONFLICT (item_id, user_id) DO UPDATE SET
                         vote = EXCLUDED.vote, voted_at = EXCLUDED.voted_at"""
                ),
                {"i": item_id, "u": user_id, "v": vote, "now": now},
            )

    def unvote(self, item_id: str, user_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM knowledge_votes WHERE item_id = :i AND user_id = :u"
                ),
                {"i": item_id, "u": user_id},
            )

    def get_votes(self, item_id: str) -> Dict[str, int]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT
                        COALESCE(SUM(CASE WHEN vote > 0 THEN 1 ELSE 0 END), 0) AS upvotes,
                        COALESCE(SUM(CASE WHEN vote < 0 THEN 1 ELSE 0 END), 0) AS downvotes
                       FROM knowledge_votes WHERE item_id = :i"""
                ),
                {"i": item_id},
            ).first()
        return {"upvotes": int(row[0]), "downvotes": int(row[1])}

    # ----- dismissals (v46) -----

    def dismiss(self, user_id: str, item_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO knowledge_item_user_dismissed (user_id, item_id)
                       VALUES (:u, :i)
                       ON CONFLICT (user_id, item_id) DO NOTHING"""
                ),
                {"u": user_id, "i": item_id},
            )

    def undismiss(self, user_id: str, item_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM knowledge_item_user_dismissed WHERE user_id = :u AND item_id = :i"
                ),
                {"u": user_id, "i": item_id},
            )

    def is_dismissed(self, user_id: str, item_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM knowledge_item_user_dismissed "
                    "WHERE user_id = :u AND item_id = :i"
                ),
                {"u": user_id, "i": item_id},
            ).first()
        return row is not None

    def list_dismissed_ids(self, user_id: str) -> List[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT item_id FROM knowledge_item_user_dismissed WHERE user_id = :u"
                ),
                {"u": user_id},
            ).all()
        return [r[0] for r in rows]

    # ----- contradictions -----

    def create_contradiction(
        self,
        item_a_id: str,
        item_b_id: str,
        explanation: str,
        severity: Optional[str] = None,
        suggested_resolution: Optional[Any] = None,
    ) -> str:
        if isinstance(suggested_resolution, dict):
            suggested_resolution_db: Optional[str] = json.dumps(suggested_resolution)
        else:
            suggested_resolution_db = suggested_resolution
        contradiction_id = f"kc_{uuid.uuid4().hex[:12]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO knowledge_contradictions (
                        id, item_a_id, item_b_id, explanation, severity, suggested_resolution
                    ) VALUES (:id, :a, :b, :ex, :sev, :sr)"""
                ),
                {
                    "id": contradiction_id, "a": item_a_id, "b": item_b_id,
                    "ex": explanation, "sev": severity, "sr": suggested_resolution_db,
                },
            )
        return contradiction_id

    @staticmethod
    def _decode_suggested_resolution(row: Dict[str, Any]) -> Dict[str, Any]:
        raw = row.get("suggested_resolution")
        if isinstance(raw, str) and raw and raw.lstrip().startswith("{"):
            try:
                row["suggested_resolution"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        return row

    def list_contradictions(
        self,
        resolved: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        sql_parts = ["SELECT * FROM knowledge_contradictions WHERE 1=1"]
        params: Dict[str, Any] = {"limit": limit}
        if resolved is not None:
            sql_parts.append(" AND resolved = :r"); params["r"] = resolved
        sql_parts.append(" ORDER BY detected_at DESC LIMIT :limit")
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("".join(sql_parts)), params).mappings().all()
        decoded = [self._normalize_row(dict(r)) for r in rows]
        return [self._decode_suggested_resolution(r) for r in decoded]

    def resolve_contradiction(
        self,
        contradiction_id: str,
        resolved_by: str,
        resolution: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE knowledge_contradictions
                    SET resolved = TRUE, resolved_by = :rb, resolved_at = :now, resolution = :res
                    WHERE id = :id"""
                ),
                {"rb": resolved_by, "now": now, "res": resolution, "id": contradiction_id},
            )

    def get_contradiction(self, contradiction_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM knowledge_contradictions WHERE id = :id"),
                {"id": contradiction_id},
            ).mappings().first()
        if row is None:
            return None
        d = self._normalize_row(dict(row))
        return self._decode_suggested_resolution(d)

    # ----- verification evidence -----

    def create_evidence(
        self,
        item_id: str,
        source_user: Optional[str] = None,
        source_ref: Optional[str] = None,
        detection_type: Optional[str] = None,
        user_quote: Optional[str] = None,
    ) -> str:
        evidence_id = f"ev_{uuid.uuid4().hex[:12]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO verification_evidence (
                        id, item_id, source_user, source_ref, detection_type, user_quote
                    ) VALUES (:id, :i, :su, :sr, :dt, :uq)"""
                ),
                {
                    "id": evidence_id, "i": item_id, "su": source_user,
                    "sr": source_ref, "dt": detection_type, "uq": user_quote,
                },
            )
        return evidence_id

    def list_evidence(self, item_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT * FROM verification_evidence
                       WHERE item_id = :i ORDER BY created_at ASC"""
                ),
                {"i": item_id},
            ).mappings().all()
        return [self._normalize_row(dict(r)) for r in rows]

    # ----- item relations -----

    @staticmethod
    def _canonical_pair(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def create_relation(
        self,
        item_a_id: str,
        item_b_id: str,
        relation_type: str,
        score: Optional[float] = None,
    ) -> None:
        if item_a_id == item_b_id:
            raise ValueError("Cannot create relation between an item and itself")
        a, b = self._canonical_pair(item_a_id, item_b_id)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO knowledge_item_relations
                       (item_a_id, item_b_id, relation_type, score)
                       VALUES (:a, :b, :rt, :score)
                       ON CONFLICT (item_a_id, item_b_id, relation_type) DO NOTHING"""
                ),
                {"a": a, "b": b, "rt": relation_type, "score": score},
            )

    def list_relations(
        self,
        relation_type: Optional[str] = None,
        resolved: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        sql_parts = ["SELECT * FROM knowledge_item_relations WHERE 1=1"]
        params: Dict[str, Any] = {"limit": limit}
        if relation_type is not None:
            sql_parts.append(" AND relation_type = :rt"); params["rt"] = relation_type
        if resolved is not None:
            sql_parts.append(" AND resolved = :r"); params["r"] = resolved
        sql_parts.append(" ORDER BY created_at DESC LIMIT :limit")
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("".join(sql_parts)), params).mappings().all()
        return [self._normalize_row(dict(r)) for r in rows]

    def resolve_relation(
        self,
        item_a_id: str,
        item_b_id: str,
        relation_type: str,
        resolved_by: str,
        resolution: str,
    ) -> int:
        a, b = self._canonical_pair(item_a_id, item_b_id)
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    """UPDATE knowledge_item_relations
                        SET resolved = TRUE, resolved_by = :rb,
                            resolved_at = :now, resolution = :res
                        WHERE item_a_id = :a AND item_b_id = :b AND relation_type = :rt
                        RETURNING 1"""
                ),
                {"rb": resolved_by, "now": now, "res": resolution,
                 "a": a, "b": b, "rt": relation_type},
            ).first()
        return 1 if row else 0

    def get_relation(
        self,
        item_a_id: str,
        item_b_id: str,
        relation_type: str,
    ) -> Optional[Dict[str, Any]]:
        a, b = self._canonical_pair(item_a_id, item_b_id)
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT * FROM knowledge_item_relations
                       WHERE item_a_id = :a AND item_b_id = :b AND relation_type = :rt"""
                ),
                {"a": a, "b": b, "rt": relation_type},
            ).mappings().first()
        return self._normalize_row(dict(row)) if row else None

    # ----- duplicate-candidate helpers -----

    def find_duplicate_candidates_by_entities(
        self,
        new_item_id: str,
        entities: Optional[List[str]],
        domain: Optional[str],
        min_overlap: int,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if not entities or not domain:
            return []
        new_set = set(entities)
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT * FROM knowledge_items
                       WHERE status IN ('approved', 'mandatory', 'pending')
                         AND (is_personal = FALSE OR is_personal IS NULL)
                         AND domain = :d
                         AND id != :id
                         AND entities IS NOT NULL
                       ORDER BY updated_at DESC
                       LIMIT :limit"""
                ),
                {"d": domain, "id": new_item_id, "limit": limit},
            ).mappings().all()
        out: List[Dict[str, Any]] = []
        for r in rows:
            row = self._normalize_row(dict(r))
            cand_entities = row.get("entities")
            if not isinstance(cand_entities, list) or not cand_entities:
                continue
            cand_set = set(cand_entities)
            overlap = new_set & cand_set
            if len(overlap) < min_overlap:
                continue
            union = new_set | cand_set
            jaccard = len(overlap) / len(union) if union else 0.0
            row["overlap_count"] = len(overlap)
            row["jaccard"] = jaccard
            out.append(row)
        return out

    # ----- bulk update + tag/audience aggregations -----

    def bulk_update(
        self,
        item_ids: List[str],
        updates: Dict[str, Any],
    ) -> Dict[str, str]:
        results: Dict[str, str] = {}
        if not item_ids:
            return results
        plain_fields = {
            k: v for k, v in updates.items()
            if k in self._UPDATABLE_FIELDS and k != "tags"
        }
        explicit_tags = updates.get("tags") if "tags" in updates else None
        tags_add = updates.get("tags_add") or []
        tags_remove = updates.get("tags_remove") or []

        for item_id in item_ids:
            try:
                item = self.get_by_id(item_id)
                if not item:
                    results[item_id] = "not_found"
                    continue
                per_item: Dict[str, Any] = dict(plain_fields)
                if explicit_tags is not None:
                    per_item["tags"] = explicit_tags
                elif tags_add or tags_remove:
                    existing = item.get("tags") or []
                    if not isinstance(existing, list):
                        existing = []
                    new_tags = list(existing)
                    for t in tags_add:
                        if t not in new_tags:
                            new_tags.append(t)
                    if tags_remove:
                        rm = set(tags_remove)
                        new_tags = [t for t in new_tags if t not in rm]
                    per_item["tags"] = new_tags
                if not per_item:
                    results[item_id] = "updated"
                    continue
                self.update(item_id, **per_item)
                results[item_id] = "updated"
            except Exception as e:
                results[item_id] = f"error: {e}"
        return results

    def count_by_tag(
        self,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Aggregate item counts per tag. PG ``jsonb_array_elements_text``
        unnests the JSONB array — equivalent to DuckDB's ``json_each``.
        """
        where = ["tags IS NOT NULL"]
        params: Dict[str, Any] = {}
        if exclude_personal:
            where.append("(is_personal = FALSE OR is_personal IS NULL)")
        if user_groups is not None:
            vis = ["audience IS NULL", "audience = 'all'"]
            if user_groups:
                keys = []
                for i, g in enumerate(user_groups):
                    k = f"ug_{i}"; keys.append(f":{k}"); params[k] = g
                vis.append(f"audience IN ({','.join(keys)})")
            if granted_domains:
                keys = []
                for i, d in enumerate(granted_domains):
                    k = f"gd_{i}"; keys.append(f":{k}"); params[k] = d
                vis.append(
                    "EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                    "WHERE kid.item_id = knowledge_items.id "
                    f"AND kid.domain_id IN ({','.join(keys)}))"
                )
            where.append("(" + " OR ".join(vis) + ")")
        where_sql = " WHERE " + " AND ".join(where)
        sql = (
            "SELECT tag, COUNT(*) AS cnt FROM ("
            "  SELECT jsonb_array_elements_text(knowledge_items.tags) AS tag "
            "  FROM knowledge_items "
            f"  {where_sql}"
            ") sub GROUP BY tag ORDER BY cnt DESC"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).all()
        return {str(tag): int(cnt) for tag, cnt in rows}

    def count_by_audience(
        self,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        where: List[str] = []
        params: Dict[str, Any] = {}
        if exclude_personal:
            where.append("(is_personal = FALSE OR is_personal IS NULL)")
        if user_groups is not None:
            vis = ["audience IS NULL", "audience = 'all'"]
            if user_groups:
                keys = []
                for i, g in enumerate(user_groups):
                    k = f"ug_{i}"; keys.append(f":{k}"); params[k] = g
                vis.append(f"audience IN ({','.join(keys)})")
            if granted_domains:
                keys = []
                for i, d in enumerate(granted_domains):
                    k = f"gd_{i}"; keys.append(f":{k}"); params[k] = d
                vis.append(
                    "EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                    "WHERE kid.item_id = knowledge_items.id "
                    f"AND kid.domain_id IN ({','.join(keys)}))"
                )
            where.append("(" + " OR ".join(vis) + ")")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT COALESCE(audience, 'all') AS aud, COUNT(*) AS cnt "
            f"FROM knowledge_items{where_sql} GROUP BY aud ORDER BY cnt DESC"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).all()
        return {r[0]: int(r[1]) for r in rows}

    def stats_breakdown(
        self,
        *,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """PG mirror of ``KnowledgeRepository.stats_breakdown`` — backs
        /api/memory/stats (by_status / categories / by_domain / by_source_type)
        on the active backend."""
        where: List[str] = []
        params: Dict[str, Any] = {}
        if exclude_personal:
            where.append("(is_personal = FALSE OR is_personal IS NULL)")
        if user_groups is not None:
            vis = ["audience IS NULL", "audience = 'all'"]
            if user_groups:
                keys = []
                for i, g in enumerate(user_groups):
                    k = f"ug_{i}"; keys.append(f":{k}"); params[k] = g
                vis.append(f"audience IN ({','.join(keys)})")
            if granted_domains:
                keys = []
                for i, d in enumerate(granted_domains):
                    k = f"gd_{i}"; keys.append(f":{k}"); params[k] = d
                vis.append(
                    "EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                    "WHERE kid.item_id = knowledge_items.id "
                    f"AND kid.domain_id IN ({','.join(keys)}))"
                )
            where.append("(" + " OR ".join(vis) + ")")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        with self._engine.connect() as conn:
            by_status = {
                r[0]: int(r[1]) for r in conn.execute(sa.text(
                    f"SELECT COALESCE(status, 'unknown') AS s, COUNT(*) "
                    f"FROM knowledge_items{where_sql} GROUP BY s"
                ), params).all()
            }
            cat_rows = conn.execute(sa.text(
                f"SELECT DISTINCT category FROM knowledge_items{where_sql} "
                f"{'AND' if where_sql else 'WHERE'} category IS NOT NULL"
            ), params).all()
            categories = sorted(r[0] for r in cat_rows if r[0])
            by_domain = {
                r[0]: int(r[1]) for r in conn.execute(sa.text(
                    "SELECT COALESCE(md.slug, 'unset') AS d, COUNT(*) "
                    "FROM knowledge_items "
                    "LEFT JOIN knowledge_item_domains kid ON kid.item_id = knowledge_items.id "
                    "LEFT JOIN memory_domains md ON md.id = kid.domain_id"
                    + (where_sql or "") + " GROUP BY d"
                ), params).all()
            }
            by_source_type = {
                r[0]: int(r[1]) for r in conn.execute(sa.text(
                    f"SELECT COALESCE(source_type, 'unknown') AS st, COUNT(*) "
                    f"FROM knowledge_items{where_sql} GROUP BY st"
                ), params).all()
            }
        return {
            "by_status": by_status,
            "categories": categories,
            "by_domain": by_domain,
            "by_source_type": by_source_type,
        }

    def find_contradiction_candidates(
        self,
        new_item_id: str,
        domain: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        sql_parts = [
            """SELECT * FROM knowledge_items
               WHERE status IN ('approved', 'mandatory', 'pending')
                 AND (is_personal = FALSE OR is_personal IS NULL)
                 AND id != :id"""
        ]
        params: Dict[str, Any] = {"id": new_item_id, "limit": limit}
        if domain:
            sql_parts.append(" AND domain = :d"); params["d"] = domain
        sql_parts.append(" ORDER BY updated_at DESC LIMIT :limit")
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("".join(sql_parts)), params).mappings().all()
        return self._rows(rows)
