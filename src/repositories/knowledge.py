"""Repository for corporate memory knowledge items, votes, and contradictions."""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb

logger = logging.getLogger(__name__)

# Sentinel for distinguishing "caller passed explicit None to clear domain"
# from "caller did not pass domain at all" in update() (v49 junction-routed
# domain writes).
_UNSET: Any = object()


class KnowledgeRepository:
    # Columns persisted as JSON-encoded strings (see `create` / `update` /
    # `bulk_update` — they pass values through ``json.dumps``). Decode on the
    # read path so callers — templates, the JSON API, e2e flows — see real
    # lists. Without this, Jinja's ``{% for tag in item.tags %}`` happily
    # iterates the characters of the JSON string and renders each one in its
    # own <span>.
    _JSON_LIST_COLUMNS = ("tags", "entities")

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

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

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        d = self._normalize_row(dict(zip(columns, row)))
        self._hydrate_domain(d)
        return d

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        out = [self._normalize_row(dict(zip(columns, row))) for row in rows]
        self._hydrate_domains_bulk(out)
        return out

    # -- Domain hydration (v49 back-compat) --------------------------------

    def _hydrate_domain(self, item: Dict[str, Any]) -> None:
        """Inject ``domain`` slug + ``domains`` list into a single item.

        v49 dropped the scalar ``knowledge_items.domain`` column. Callers
        (templates, /api/memory/{id}/provenance, services.contradiction)
        still expect ``item["domain"]`` to resolve to a single slug; the
        admin queue UI also reads ``item["domains"]`` to render all of
        them. Both surfaces are populated here from the junction.
        """
        item_id = item.get("id")
        if not item_id:
            item["domain"] = None
            item["domains"] = []
            return
        rows = self.conn.execute(
            "SELECT md.slug FROM knowledge_item_domains kid "
            "JOIN memory_domains md ON md.id = kid.domain_id "
            "WHERE kid.item_id = ? "
            "ORDER BY md.slug",
            [item_id],
        ).fetchall()
        slugs = [r[0] for r in rows]
        item["domain"] = slugs[0] if slugs else None
        item["domains"] = slugs

    def _hydrate_domains_bulk(self, items: List[Dict[str, Any]]) -> None:
        """Single-query domain hydration across a result set (N+1 avoidance).

        Mutates each item in place with two fields:
          * ``domain``  — alphabetically-first slug (legacy single-slug
            surface kept for back-compat — templates / contradiction
            services / older clients still read this).
          * ``domains`` — full list of slugs (sorted) so multi-domain
            items render all their chips in the admin queue without a
            second roundtrip.

        None / [] are set for items with no junction rows.
        """
        if not items:
            return
        ids = [it["id"] for it in items if it.get("id")]
        if not ids:
            for it in items:
                it["domain"] = None
                it["domains"] = []
            return
        placeholders = ",".join(["?"] * len(ids))
        rows = self.conn.execute(
            f"SELECT kid.item_id, md.slug "
            f"FROM knowledge_item_domains kid "
            f"JOIN memory_domains md ON md.id = kid.domain_id "
            f"WHERE kid.item_id IN ({placeholders}) "
            f"ORDER BY md.slug",
            ids,
        ).fetchall()
        mapping: Dict[str, List[str]] = {}
        for item_id, slug in rows:
            mapping.setdefault(item_id, []).append(slug)
        for it in items:
            slugs = mapping.get(it.get("id"), [])
            it["domain"] = slugs[0] if slugs else None
            it["domains"] = slugs

    def get_by_id(self, item_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM knowledge_items WHERE id = ?", [item_id]).fetchone()
        return self._row_to_dict(result)

    def get_by_ids(self, item_ids: List[str]) -> Dict[str, Any]:
        """Fetch multiple items by ID in one query. Returns dict keyed by id."""
        if not item_ids:
            return {}
        placeholders = ", ".join("?" for _ in item_ids)
        rows = self.conn.execute(
            f"SELECT * FROM knowledge_items WHERE id IN ({placeholders})",
            item_ids,
        ).fetchall()
        items = self._rows_to_dicts(rows)
        return {item["id"]: item for item in items}

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
        added_by: Optional[str] = None,
    ) -> None:
        """Insert a new item; ``domain`` (if provided) writes a row into
        the ``knowledge_item_domains`` junction.

        v49: the scalar ``knowledge_items.domain`` column was dropped. The
        kwarg is preserved for caller BC; values are resolved through
        ``memory_domains`` (slug → id) and routed to the junction.
        Unknown slug raises ``ValueError`` — admin pre-creates domains via
        the dedicated CRUD endpoint.
        """
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO knowledge_items (
                id, title, content, category, source_user, tags, status,
                confidence, entities, source_type, source_ref,
                valid_from, valid_until, supersedes, sensitivity, is_personal,
                is_required, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                id, title, content, category, source_user,
                json.dumps(tags) if tags else None, status,
                confidence,
                json.dumps(entities) if entities else None,
                source_type, source_ref,
                valid_from, valid_until, supersedes, sensitivity, is_personal,
                is_required, now, now,
            ],
        )
        if domain:
            self._set_item_domain_by_slug(id, domain, added_by=added_by or source_user or "system")
        self._refresh_fts_index()

    # -- Domain junction helpers (v49) -------------------------------------

    def _set_item_domain_by_slug(
        self, item_id: str, slug: str, *, added_by: str
    ) -> None:
        """Resolve ``slug`` to ``memory_domains.id`` and write one junction row.

        Single-domain helper used by the create/update path for back-compat
        with the old scalar-column callers. Callers that need multi-domain
        membership should use ``MemoryDomainsRepository.replace_domains_for_item``
        directly.
        """
        row = self.conn.execute(
            "SELECT id FROM memory_domains WHERE slug = ?", [slug]
        ).fetchone()
        if not row:
            raise ValueError(f"Unknown memory domain slug: {slug}")
        domain_id = row[0]
        # Replace semantics — match the pre-v49 scalar column.
        self.conn.execute(
            "DELETE FROM knowledge_item_domains WHERE item_id = ?", [item_id]
        )
        self.conn.execute(
            "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [item_id, domain_id, added_by],
        )

    _UPDATABLE_FIELDS = {
        "title", "content", "category", "tags", "entities",
        "source_type", "source_ref", "source_user", "audience",
        "confidence", "status", "sensitivity", "is_personal",
        "is_required",
        "valid_from", "valid_until", "supersedes",
    }

    def update(self, item_id: str, **fields) -> None:
        """Partial update. ``domain`` (if passed) routes to the junction;
        scalar columns are SET in one UPDATE.

        v49: ``knowledge_items.domain`` is gone; the kwarg is preserved
        and routed to ``knowledge_item_domains`` via slug→id resolution.
        Unknown slug raises ``ValueError``.
        """
        domain_value = fields.pop("domain", None) if "domain" in fields else _UNSET
        safe = {k: v for k, v in fields.items() if k in self._UPDATABLE_FIELDS}
        # Scalar UPDATE + domain-junction rewrite run in one transaction so a
        # concurrent reader never sees a half-applied edit — notably the
        # domain DELETE+INSERT below, whose empty intermediate would make the
        # item momentarily domain-less to RBAC reads. The FTS rebuild runs
        # AFTER commit: `PRAGMA create_fts_index` is catalog DDL and cannot
        # run inside an explicit transaction.
        refresh_fts = bool(safe and ("title" in safe or "content" in safe))
        self.conn.execute("BEGIN")
        try:
            # Scalar-column UPDATE path
            if safe:
                now = datetime.now(timezone.utc)
                set_clause = ", ".join(f"{k} = ?" for k in safe)
                values = list(safe.values()) + [now, item_id]
                self.conn.execute(
                    f"UPDATE knowledge_items SET {set_clause}, updated_at = ? WHERE id = ?",
                    values,
                )
            # Domain junction path (only if caller passed domain explicitly).
            if domain_value is not _UNSET:
                if domain_value is None or domain_value == "":
                    # Clear all junction rows — caller asked to drop the domain.
                    self.conn.execute(
                        "DELETE FROM knowledge_item_domains WHERE item_id = ?", [item_id]
                    )
                else:
                    self._set_item_domain_by_slug(item_id, domain_value, added_by="system")
                # Bump updated_at even if no scalar field changed.
                if not safe:
                    self.conn.execute(
                        "UPDATE knowledge_items SET updated_at = ? WHERE id = ?",
                        [datetime.now(timezone.utc), item_id],
                    )
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        # FTS index rebuilds only when the indexed text changed — status / tag
        # / is_required flips don't affect the BM25 token stream.
        if refresh_fts:
            self._refresh_fts_index()

    def set_is_required(self, item_id: str, value: bool) -> None:
        """Toggle the global ``is_required`` flag without touching ``status``.

        v49: replaces the old ``status='mandatory'`` overload. ``status``
        is reserved for lifecycle (pending / approved / rejected / revoked
        / expired); the Required tier rides on this boolean.
        """
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE knowledge_items SET is_required = ?, updated_at = ? WHERE id = ?",
            [value, now, item_id],
        )

    def update_status(self, item_id: str, status: str) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE knowledge_items SET status = ?, updated_at = ? WHERE id = ?",
            [status, now, item_id],
        )
        # Status flips don't touch the indexed title/content — no rebuild.

    def _refresh_fts_index(self) -> None:
        """Rebuild the BM25 index after a mutation that changed indexed text.

        Soft helper — failure is logged inside ``ensure_knowledge_fts_index``
        and the repo's search path falls back to ILIKE on the next call.
        Kept as a private method so the create / update sites stay free of
        try/except clutter.
        """
        from src.fts import ensure_knowledge_fts_index

        ensure_knowledge_fts_index(self.conn)

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
        is_required: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """v49 contract:

        * ``domain`` is a slug; we resolve through ``memory_domains`` and
          EXISTS-join against ``knowledge_item_domains``. Unknown slug →
          empty result (matches the "no rows match" semantic of the old
          scalar filter).
        * ``granted_domains`` is now a list of ``memory_domains.id`` (NOT
          slugs) per the v49 migration's grant re-point.
        * ``is_required`` filters on the new ``knowledge_items.is_required``
          boolean. ``status='mandatory'`` is gone — callers wanting "mandatory
          tier" pass ``is_required=True``.
        """
        query = "SELECT * FROM knowledge_items WHERE 1=1"
        params: List[Any] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if category:
            query += " AND category = ?"
            params.append(category)
        if is_required is not None:
            query += " AND is_required = ?"
            params.append(bool(is_required))
        if upvoted_by_user:
            # "My Upvotes" filter — items the caller has explicitly upvoted
            # (vote > 0). Replaces the old dead "My Rules" category sentinel
            # which never matched any row. Subquery rather than JOIN keeps
            # this orthogonal to the other filters (no row duplication).
            query += (
                " AND id IN (SELECT item_id FROM knowledge_votes "
                "WHERE user_id = ? AND vote > 0)"
            )
            params.append(upvoted_by_user)
        if domain:
            # v49: scalar column is gone — resolve slug → id and EXISTS-join.
            domain_id = self._resolve_domain_slug(domain)
            if domain_id is None:
                return []  # unknown slug → empty, mirror old WHERE domain = ? semantics
            query += (
                " AND EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                "WHERE kid.item_id = knowledge_items.id AND kid.domain_id = ?)"
            )
            params.append(domain_id)
        if source_type:
            query += " AND source_type = ?"
            params.append(source_type)
        if exclude_personal:
            query += " AND (is_personal = FALSE OR is_personal IS NULL)"
        if user_groups is not None:
            # Visibility: audience-string match (null/all/group:X) OR
            # caller has a MEMORY_DOMAIN grant on one of the item's domains.
            # Falsy ``granted_domains`` collapses the EXISTS sub-clause,
            # preserving pre-RBAC behaviour (audience-only).
            visibility_clauses = ["audience IS NULL", "audience = 'all'"]
            if user_groups:
                audience_placeholders = ", ".join("?" for _ in user_groups)
                visibility_clauses.append(f"audience IN ({audience_placeholders})")
                params.extend(user_groups)
            if granted_domains:
                domain_placeholders = ", ".join("?" for _ in granted_domains)
                visibility_clauses.append(
                    "EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                    "WHERE kid.item_id = knowledge_items.id "
                    f"AND kid.domain_id IN ({domain_placeholders}))"
                )
                params.extend(granted_domains)
            query += " AND (" + " OR ".join(visibility_clauses) + ")"
        if hide_dismissed and dismissed_by_user:
            # v46: per-user opt-out. Exclude items the caller has dismissed,
            # but never hide Required items — the governance hard rule
            # is enforced here as well as in the API layer so a stale row
            # in knowledge_item_user_dismissed (left over from before an
            # item was required) can't accidentally hide a required item.
            query += (
                " AND NOT EXISTS ("
                " SELECT 1 FROM knowledge_item_user_dismissed d"
                " WHERE d.item_id = knowledge_items.id"
                "   AND d.user_id = ?"
                "   AND knowledge_items.is_required = FALSE"
                ")"
            )
            params.append(dismissed_by_user)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self._rows_to_dicts(self.conn.execute(query, params).fetchall())

    def _resolve_domain_slug(self, slug: str) -> Optional[str]:
        """slug → ``memory_domains.id`` (v49). Returns None for unknown slug."""
        row = self.conn.execute(
            "SELECT id FROM memory_domains WHERE slug = ?", [slug]
        ).fetchone()
        return row[0] if row else None

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
        is_required: Optional[bool] = None,
        dismissed_by_user: Optional[str] = None,
        hide_dismissed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Relevance-ranked search across ``title`` + ``content`` (#121).

        Uses DuckDB FTS BM25 ranking when the extension is available;
        falls back to a non-ranked ``ILIKE`` query (ORDER BY
        ``updated_at`` DESC) otherwise. Same filter surface either way.
        The fallback is the same code path that ran before #121, so
        existing installs without ``fts`` available regress only on
        result *ordering*, not on result set membership.

        Index rebuilds are *not* triggered here — too expensive at
        search QPS. ``create`` / ``update`` / ``update_status`` rebuild
        on mutation; ``app/main.py`` lifespan rebuilds once on startup
        as a safety net. We only need ``LOAD fts`` here so the
        ``match_bm25`` UDF resolves on this cursor.
        """
        from src.fts import ensure_fts_loaded

        # Build a closure that appends the (identical-across-paths) filter
        # clauses + LIMIT/OFFSET onto a base SELECT + WHERE prefix, then
        # executes. Lets the FTS-or-ILIKE choice stay in one place while
        # preserving the "wrap FTS execute in try/except, fall through on
        # any duckdb.Error" contract — extension loadable but index
        # missing, or a concurrent rebuild's drop-then-create window
        # opening between the loaded check and our query, both raise here
        # and we transparently retry against ILIKE.
        # Resolve domain slug → id once (mirror list_items semantics):
        # unknown slug → return [] before running any SQL.
        domain_id = self._resolve_domain_slug(domain) if domain else None
        if domain and domain_id is None:
            return []

        def _run(base_sql: str, base_params: List[Any], order_clause: str) -> List[Any]:
            sql = base_sql
            params: List[Any] = list(base_params)
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                sql += f" AND status IN ({placeholders})"
                params.extend(statuses)
            if category:
                sql += " AND category = ?"
                params.append(category)
            if domain_id:
                sql += (
                    " AND EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                    "WHERE kid.item_id = knowledge_items.id AND kid.domain_id = ?)"
                )
                params.append(domain_id)
            if source_type:
                sql += " AND source_type = ?"
                params.append(source_type)
            if is_required is not None:
                sql += " AND is_required = ?"
                params.append(bool(is_required))
            if exclude_personal:
                sql += " AND (is_personal = FALSE OR is_personal IS NULL)"
            if user_groups is not None:
                visibility_clauses = ["audience IS NULL", "audience = 'all'"]
                if user_groups:
                    audience_placeholders = ", ".join("?" for _ in user_groups)
                    visibility_clauses.append(f"audience IN ({audience_placeholders})")
                    params.extend(user_groups)
                if granted_domains:
                    domain_placeholders = ", ".join("?" for _ in granted_domains)
                    visibility_clauses.append(
                        "EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                        "WHERE kid.item_id = knowledge_items.id "
                        f"AND kid.domain_id IN ({domain_placeholders}))"
                    )
                    params.extend(granted_domains)
                sql += " AND (" + " OR ".join(visibility_clauses) + ")"
            if hide_dismissed and dismissed_by_user:
                sql += (
                    " AND NOT EXISTS ("
                    " SELECT 1 FROM knowledge_item_user_dismissed d"
                    " WHERE d.item_id = knowledge_items.id"
                    "   AND d.user_id = ?"
                    "   AND knowledge_items.is_required = FALSE"
                    ")"
                )
                params.append(dismissed_by_user)
            sql += order_clause + " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            return self.conn.execute(sql, params).fetchall()

        # ILIKE fallback path — preserves the pre-#121 query shape byte-
        # for-byte (plus an explicit ``NULL AS bm25_score`` so the result
        # column set matches the FTS path: consumers can read the score
        # uniformly without having to know which tier produced the row).
        ilike_pattern = f"%{query}%"
        ilike_sql = (
            "SELECT *, NULL AS bm25_score FROM knowledge_items "
            "WHERE (title ILIKE ? OR content ILIKE ?)"
        )
        ilike_params: List[Any] = [ilike_pattern, ilike_pattern]
        ilike_order = " ORDER BY updated_at DESC"

        if ensure_fts_loaded(self.conn):
            # BM25 path. ``match_bm25`` is evaluated once in the SELECT
            # and reused as the WHERE / ORDER BY filter via the alias.
            fts_sql = (
                "SELECT *, fts_main_knowledge_items.match_bm25(id, ?) AS bm25_score "
                "FROM knowledge_items "
                "WHERE fts_main_knowledge_items.match_bm25(id, ?) IS NOT NULL"
            )
            fts_params: List[Any] = [query, query]
            fts_order = " ORDER BY bm25_score DESC, updated_at DESC"
            try:
                results = _run(fts_sql, fts_params, fts_order)
            except duckdb.Error as e:
                # Extension loaded but index missing (migration soft-fail
                # or a concurrent ``overwrite=1`` rebuild caught us in the
                # drop-then-create window). Fall through to ILIKE rather
                # than 500 the /api/memory?search= endpoint.
                logger.warning(
                    "FTS BM25 search failed (%s); falling back to ILIKE", e,
                )
                results = _run(ilike_sql, ilike_params, ilike_order)
        else:
            results = _run(ilike_sql, ilike_params, ilike_order)
        return self._rows_to_dicts(results)

    def count_items(
        self,
        search: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        category: Optional[str] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        is_required: Optional[bool] = None,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
        dismissed_by_user: Optional[str] = None,
        hide_dismissed: bool = False,
    ) -> int:
        # Closure mirrors search(): the filter clauses are identical
        # whether the base prefix is FTS or ILIKE, so we build once and
        # apply twice if the FTS execute raises (extension loadable but
        # index missing / overwrite=1 drop-then-create window). Counts
        # MUST match the paginated result set in search() — same
        # decision tree, same predicate shape, same fallback semantics.
        # v49: domain slug → id resolution (unknown slug → count = 0).
        domain_id = self._resolve_domain_slug(domain) if domain else None
        if domain and domain_id is None:
            return 0

        def _run(base_sql: str, base_params: List[Any]) -> int:
            sql = base_sql
            params: List[Any] = list(base_params)
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                sql += f" AND status IN ({placeholders})"
                params.extend(statuses)
            if category:
                sql += " AND category = ?"
                params.append(category)
            if domain_id:
                sql += (
                    " AND EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                    "WHERE kid.item_id = knowledge_items.id AND kid.domain_id = ?)"
                )
                params.append(domain_id)
            if source_type:
                sql += " AND source_type = ?"
                params.append(source_type)
            if is_required is not None:
                sql += " AND is_required = ?"
                params.append(bool(is_required))
            if exclude_personal:
                sql += " AND (is_personal = FALSE OR is_personal IS NULL)"
            if user_groups is not None:
                visibility_clauses = ["audience IS NULL", "audience = 'all'"]
                if user_groups:
                    audience_placeholders = ", ".join("?" for _ in user_groups)
                    visibility_clauses.append(f"audience IN ({audience_placeholders})")
                    params.extend(user_groups)
                if granted_domains:
                    domain_placeholders = ", ".join("?" for _ in granted_domains)
                    visibility_clauses.append(
                        "EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                        "WHERE kid.item_id = knowledge_items.id "
                        f"AND kid.domain_id IN ({domain_placeholders}))"
                    )
                    params.extend(granted_domains)
                sql += " AND (" + " OR ".join(visibility_clauses) + ")"
            if hide_dismissed and dismissed_by_user:
                sql += (
                    " AND NOT EXISTS ("
                    " SELECT 1 FROM knowledge_item_user_dismissed d"
                    " WHERE d.item_id = knowledge_items.id"
                    "   AND d.user_id = ?"
                    "   AND knowledge_items.is_required = FALSE"
                    ")"
                )
                params.append(dismissed_by_user)
            return self.conn.execute(sql, params).fetchone()[0]

        if not search:
            return _run("SELECT COUNT(*) FROM knowledge_items WHERE 1=1", [])

        from src.fts import ensure_fts_loaded

        ilike_pattern = f"%{search}%"
        ilike_sql = "SELECT COUNT(*) FROM knowledge_items WHERE (title ILIKE ? OR content ILIKE ?)"
        ilike_params: List[Any] = [ilike_pattern, ilike_pattern]

        if ensure_fts_loaded(self.conn):
            fts_sql = (
                "SELECT COUNT(*) FROM knowledge_items "
                "WHERE fts_main_knowledge_items.match_bm25(id, ?) IS NOT NULL"
            )
            try:
                return _run(fts_sql, [search])
            except duckdb.Error as e:
                logger.warning(
                    "FTS BM25 count failed (%s); falling back to ILIKE", e,
                )
                return _run(ilike_sql, ilike_params)
        return _run(ilike_sql, ilike_params)

    def list_by_domain(
        self,
        domain: str,
        statuses: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """v49: resolves slug through ``memory_domains`` + EXISTS-joins the
        junction. Unknown slug → empty (matches old WHERE domain = ? semantic)."""
        domain_id = self._resolve_domain_slug(domain)
        if domain_id is None:
            return []
        query = (
            "SELECT * FROM knowledge_items "
            "WHERE EXISTS (SELECT 1 FROM knowledge_item_domains kid "
            "WHERE kid.item_id = knowledge_items.id AND kid.domain_id = ?)"
        )
        params: List[Any] = [domain_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return self._rows_to_dicts(self.conn.execute(query, params).fetchall())

    def get_user_contributions(self, source_user: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM knowledge_items WHERE source_user = ? ORDER BY updated_at DESC",
            [source_user],
        ).fetchall()
        return self._rows_to_dicts(results)

    def set_personal(self, item_id: str, is_personal: bool) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE knowledge_items SET is_personal = ?, updated_at = ? WHERE id = ?",
            [is_personal, now, item_id],
        )

    # --- Votes ---

    def vote(self, item_id: str, user_id: str, vote: int) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO knowledge_votes (item_id, user_id, vote, voted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (item_id, user_id) DO UPDATE SET vote = excluded.vote, voted_at = excluded.voted_at""",
            [item_id, user_id, vote, now],
        )

    def unvote(self, item_id: str, user_id: str) -> None:
        self.conn.execute(
            "DELETE FROM knowledge_votes WHERE item_id = ? AND user_id = ?",
            [item_id, user_id],
        )

    def get_votes(self, item_id: str) -> Dict[str, int]:
        result = self.conn.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN vote > 0 THEN 1 ELSE 0 END), 0) as upvotes,
                COALESCE(SUM(CASE WHEN vote < 0 THEN 1 ELSE 0 END), 0) as downvotes
            FROM knowledge_votes WHERE item_id = ?""",
            [item_id],
        ).fetchone()
        return {"upvotes": result[0], "downvotes": result[1]}

    # --- Dismissals (v46 — per-user opt-out) ---
    #
    # Mandatory items are never dismissible. The API layer rejects POSTs
    # against mandatory items with a 400; the SQL filters in list_items /
    # search / count_items and the bundle endpoint also exclude
    # ``status = 'mandatory'`` from the dismissal subquery, so a stale row
    # left over from before an item was mandated cannot accidentally hide
    # a mandatory item.

    def dismiss(self, user_id: str, item_id: str) -> None:
        """Idempotent INSERT — re-dismissing is a no-op."""
        self.conn.execute(
            """INSERT INTO knowledge_item_user_dismissed (user_id, item_id)
            VALUES (?, ?)
            ON CONFLICT (user_id, item_id) DO NOTHING""",
            [user_id, item_id],
        )

    def undismiss(self, user_id: str, item_id: str) -> None:
        """Idempotent DELETE — un-dismissing an item that was never dismissed is a no-op."""
        self.conn.execute(
            "DELETE FROM knowledge_item_user_dismissed WHERE user_id = ? AND item_id = ?",
            [user_id, item_id],
        )

    def is_dismissed(self, user_id: str, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM knowledge_item_user_dismissed WHERE user_id = ? AND item_id = ?",
            [user_id, item_id],
        ).fetchone()
        return row is not None

    def list_dismissed_ids(self, user_id: str) -> List[str]:
        """Return all item ids this user has dismissed.

        Callers typically materialize the result as a ``set`` to power per-
        item ``dismissed_by_me`` flags on the listing response.
        """
        rows = self.conn.execute(
            "SELECT item_id FROM knowledge_item_user_dismissed WHERE user_id = ?",
            [user_id],
        ).fetchall()
        return [r[0] for r in rows]

    # --- Contradictions ---

    def create_contradiction(
        self,
        item_a_id: str,
        item_b_id: str,
        explanation: str,
        severity: Optional[str] = None,
        suggested_resolution: Optional[Any] = None,
    ) -> str:
        """Persist a contradiction.

        ``suggested_resolution`` may be either a free-form string (legacy
        callers) or a dict (the structured shape produced by Haiku — see ADR
        Decision 4). Dicts are JSON-encoded into the existing TEXT column so
        no schema migration is needed; the read side decodes back to dict.
        """
        if isinstance(suggested_resolution, dict):
            suggested_resolution_db: Optional[str] = json.dumps(suggested_resolution)
        else:
            suggested_resolution_db = suggested_resolution
        contradiction_id = f"kc_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """INSERT INTO knowledge_contradictions (
                id, item_a_id, item_b_id, explanation, severity, suggested_resolution
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            [contradiction_id, item_a_id, item_b_id, explanation, severity, suggested_resolution_db],
        )
        return contradiction_id

    @staticmethod
    def _decode_suggested_resolution(row: Dict[str, Any]) -> Dict[str, Any]:
        """If the stored suggested_resolution is JSON, decode it to a dict.
        Plain strings (legacy rows) are returned unchanged.
        """
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
        query = "SELECT * FROM knowledge_contradictions WHERE 1=1"
        params: List[Any] = []
        if resolved is not None:
            query += " AND resolved = ?"
            params.append(resolved)
        query += " ORDER BY detected_at DESC LIMIT ?"
        params.append(limit)
        rows = self._rows_to_dicts(self.conn.execute(query, params).fetchall())
        return [self._decode_suggested_resolution(r) for r in rows]

    def resolve_contradiction(
        self,
        contradiction_id: str,
        resolved_by: str,
        resolution: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """UPDATE knowledge_contradictions
            SET resolved = TRUE, resolved_by = ?, resolved_at = ?, resolution = ?
            WHERE id = ?""",
            [resolved_by, now, resolution, contradiction_id],
        )

    def get_contradiction(self, contradiction_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM knowledge_contradictions WHERE id = ?",
            [contradiction_id],
        ).fetchone()
        row = self._row_to_dict(result)
        if row is not None:
            self._decode_suggested_resolution(row)
        return row

    # --- Verification Evidence ---

    def create_evidence(
        self,
        item_id: str,
        source_user: Optional[str] = None,
        source_ref: Optional[str] = None,
        detection_type: Optional[str] = None,
        user_quote: Optional[str] = None,
    ) -> str:
        """Persist one verification evidence row for a knowledge item.

        Multiple evidence rows per item are expected — each new analyst
        confirmation/correction adds one. user_quote and detection_type are the
        raw signal future Bayesian re-calibration consumes.
        """
        evidence_id = f"ev_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """INSERT INTO verification_evidence (
                id, item_id, source_user, source_ref, detection_type, user_quote
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            [evidence_id, item_id, source_user, source_ref, detection_type, user_quote],
        )
        return evidence_id

    def list_evidence(self, item_id: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            """SELECT * FROM verification_evidence
            WHERE item_id = ?
            ORDER BY created_at ASC""",
            [item_id],
        ).fetchall()
        return self._rows_to_dicts(results)

    # --- Item relations (duplicate-candidate hints, etc.) ---

    @staticmethod
    def _canonical_pair(a: str, b: str) -> tuple[str, str]:
        """Return (min(a,b), max(a,b)) — every unordered pair maps to one row."""
        return (a, b) if a <= b else (b, a)

    def create_relation(
        self,
        item_a_id: str,
        item_b_id: str,
        relation_type: str,
        score: Optional[float] = None,
    ) -> None:
        """Persist a relation row. Idempotent on (item_a_id, item_b_id, relation_type).

        The PK is canonicalized to (min, max) so duplicate calls with reversed
        arguments don't create a second row. Self-relations (a == b) are
        rejected — a pair must reference two distinct items.
        """
        if item_a_id == item_b_id:
            raise ValueError("Cannot create relation between an item and itself")
        a, b = self._canonical_pair(item_a_id, item_b_id)
        self.conn.execute(
            """INSERT INTO knowledge_item_relations
                (item_a_id, item_b_id, relation_type, score)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (item_a_id, item_b_id, relation_type) DO NOTHING""",
            [a, b, relation_type, score],
        )

    def list_relations(
        self,
        relation_type: Optional[str] = None,
        resolved: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM knowledge_item_relations WHERE 1=1"
        params: List[Any] = []
        if relation_type is not None:
            sql += " AND relation_type = ?"
            params.append(relation_type)
        if resolved is not None:
            sql += " AND resolved = ?"
            params.append(resolved)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self._rows_to_dicts(self.conn.execute(sql, params).fetchall())

    def count_relations(
        self,
        relation_type: Optional[str] = None,
        resolved: Optional[bool] = None,
    ) -> int:
        """Count relation rows matching the given filters.

        Used for badge counts (e.g. unresolved duplicate candidates) where
        callers only need the total, not the rows themselves.
        """
        sql = "SELECT COUNT(*) FROM knowledge_item_relations WHERE 1=1"
        params: List[Any] = []
        if relation_type is not None:
            sql += " AND relation_type = ?"
            params.append(relation_type)
        if resolved is not None:
            sql += " AND resolved = ?"
            params.append(resolved)
        return self.conn.execute(sql, params).fetchone()[0]

    def resolve_relation(
        self,
        item_a_id: str,
        item_b_id: str,
        relation_type: str,
        resolved_by: str,
        resolution: str,
    ) -> int:
        """Mark a relation row resolved. Returns rowcount (0 if not found)."""
        a, b = self._canonical_pair(item_a_id, item_b_id)
        now = datetime.now(timezone.utc)
        # DuckDB doesn't expose UPDATE rowcount via the cursor API uniformly;
        # do an existence check first so callers can report 404 vs success.
        existing = self.conn.execute(
            """SELECT 1 FROM knowledge_item_relations
                WHERE item_a_id = ? AND item_b_id = ? AND relation_type = ?""",
            [a, b, relation_type],
        ).fetchone()
        if not existing:
            return 0
        self.conn.execute(
            """UPDATE knowledge_item_relations
                SET resolved = TRUE,
                    resolved_by = ?,
                    resolved_at = ?,
                    resolution = ?
                WHERE item_a_id = ? AND item_b_id = ? AND relation_type = ?""",
            [resolved_by, now, resolution, a, b, relation_type],
        )
        return 1

    def get_relation(
        self,
        item_a_id: str,
        item_b_id: str,
        relation_type: str,
    ) -> Optional[Dict[str, Any]]:
        a, b = self._canonical_pair(item_a_id, item_b_id)
        result = self.conn.execute(
            """SELECT * FROM knowledge_item_relations
                WHERE item_a_id = ? AND item_b_id = ? AND relation_type = ?""",
            [a, b, relation_type],
        ).fetchone()
        return self._row_to_dict(result)

    def find_duplicate_candidates_by_entities(
        self,
        new_item_id: str,
        entities: Optional[List[str]],
        domain: Optional[str],
        min_overlap: int,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Same-domain candidates whose ``entities`` set shares >= ``min_overlap``
        members with ``entities``.

        Personal items are excluded (privacy boundary — see ADR Decision 1
        precedent in ``find_contradiction_candidates``). Self-id is excluded.
        Domain is a hard SQL conjunct: a NULL-domain item produces no
        candidates (matches the verification-detector skip-empty contract).
        Jaccard is computed in Python because DuckDB lacks a portable JSON
        intersection helper; the SQL layer trims the candidate set to the
        same domain so the Python loop scales linearly with that.
        """
        if not entities or not domain:
            return []
        # v49: resolve slug → id and EXISTS-join via junction.
        domain_id = self._resolve_domain_slug(domain)
        if domain_id is None:
            return []
        new_set = set(entities)
        sql = """
            SELECT * FROM knowledge_items
            WHERE status IN ('approved', 'pending')
              AND (is_personal = FALSE OR is_personal IS NULL)
              AND EXISTS (
                SELECT 1 FROM knowledge_item_domains kid
                 WHERE kid.item_id = knowledge_items.id AND kid.domain_id = ?
              )
              AND id != ?
              AND entities IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT ?
        """
        rows = self._rows_to_dicts(
            self.conn.execute(sql, [domain_id, new_item_id, limit]).fetchall()
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            cand_entities = row.get("entities")
            if isinstance(cand_entities, str):
                try:
                    cand_entities = json.loads(cand_entities)
                except json.JSONDecodeError:
                    continue
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

    # --- Bulk update + tag/audience aggregations (issue #62) ---

    def bulk_update(
        self,
        item_ids: List[str],
        updates: Dict[str, Any],
    ) -> Dict[str, str]:
        """Apply ``updates`` to each item id; partial-failure tolerant.

        ``updates`` may include the standard ``_UPDATABLE_FIELDS`` keys plus
        the bulk-only ``tags_add`` / ``tags_remove`` lists. Tag mutations are
        merged with the item's existing tags so callers don't have to fetch
        first. Returns a per-id status map: ``"updated"`` / ``"not_found"`` /
        an error message.
        """
        results: Dict[str, str] = {}
        if not item_ids:
            return results

        plain_fields = {
            k: v for k, v in updates.items()
            if k in self._UPDATABLE_FIELDS and k != "tags"
        }
        # ``domain`` is routed through the v49 junction, NOT a scalar column —
        # it's not in _UPDATABLE_FIELDS but ``update()`` accepts it. Pass it
        # through explicitly so bulk_update preserves the same kwarg surface
        # as the per-item update path.
        if "domain" in updates:
            plain_fields["domain"] = updates["domain"]
        # If the caller passed an explicit ``tags`` list, treat it as a hard
        # set (same semantics as repo.update). Add/remove are applied per item.
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
                    if isinstance(existing, str):
                        try:
                            existing = json.loads(existing)
                        except json.JSONDecodeError:
                            existing = []
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
                    results[item_id] = "updated"  # nothing to do, treat as success
                    continue

                # JSON-encode tags before passing to .update (mirrors create()).
                if "tags" in per_item:
                    per_item["tags"] = (
                        json.dumps(per_item["tags"]) if per_item["tags"] else None
                    )
                if "entities" in per_item and isinstance(per_item["entities"], list):
                    per_item["entities"] = json.dumps(per_item["entities"]) if per_item["entities"] else None

                self.update(item_id, **per_item)
                results[item_id] = "updated"
            except Exception as e:  # pragma: no cover - defensive
                results[item_id] = f"error: {e}"
        return results

    def count_by_tag(
        self,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Aggregate item counts per tag (one tag may belong to many items).

        Uses DuckDB ``json_each`` to unnest the JSON tag list. Items with no
        tags don't contribute. Visibility filter mirrors ``count_items``
        (audience OR MEMORY_DOMAIN grant).
        """
        where = ["tags IS NOT NULL"]
        params: List[Any] = []
        if exclude_personal:
            where.append("(is_personal = FALSE OR is_personal IS NULL)")
        if user_groups is not None:
            visibility = ["audience IS NULL", "audience = 'all'"]
            if user_groups:
                ph = ",".join(["?"] * len(user_groups))
                visibility.append(f"audience IN ({ph})")
                params.extend(user_groups)
            if granted_domains:
                dph = ",".join(["?"] * len(granted_domains))
                # v49: granted_domains are now memory_domains.id (not slugs);
                # EXISTS-join via the junction.
                visibility.append(
                    "EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                    "WHERE kid.item_id = knowledge_items.id "
                    f"AND kid.domain_id IN ({dph}))"
                )
                params.extend(granted_domains)
            where.append("(" + " OR ".join(visibility) + ")")
        where_sql = " WHERE " + " AND ".join(where)
        sql = (
            "SELECT t.value AS tag, COUNT(*) AS cnt "
            "FROM knowledge_items, json_each(knowledge_items.tags) AS t "
            f"{where_sql} "
            "GROUP BY t.value ORDER BY cnt DESC"
        )
        rows = self.conn.execute(sql, params).fetchall()
        out: Dict[str, int] = {}
        for tag, cnt in rows:
            # json_each returns the raw scalar; strip wrapping quotes if needed.
            key = tag if isinstance(tag, str) else str(tag)
            if key.startswith('"') and key.endswith('"'):
                key = key[1:-1]
            out[key] = cnt
        return out

    def count_by_audience(
        self,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Aggregate item counts per audience bucket.

        ``audience`` is a free-form column whose canonical values are
        ``NULL`` / ``'all'`` / ``'group:<name>'``. NULL is bucketed as
        ``'all'`` so the chip-filter UI doesn't need a separate "no audience"
        affordance. Visibility filter mirrors ``count_items`` (audience OR
        MEMORY_DOMAIN grant).
        """
        where: List[str] = []
        params: List[Any] = []
        if exclude_personal:
            where.append("(is_personal = FALSE OR is_personal IS NULL)")
        if user_groups is not None:
            visibility = ["audience IS NULL", "audience = 'all'"]
            if user_groups:
                ph = ",".join(["?"] * len(user_groups))
                visibility.append(f"audience IN ({ph})")
                params.extend(user_groups)
            if granted_domains:
                dph = ",".join(["?"] * len(granted_domains))
                # v49: granted_domains are now memory_domains.id (not slugs);
                # EXISTS-join via the junction.
                visibility.append(
                    "EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                    "WHERE kid.item_id = knowledge_items.id "
                    f"AND kid.domain_id IN ({dph}))"
                )
                params.extend(granted_domains)
            where.append("(" + " OR ".join(visibility) + ")")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT COALESCE(audience, 'all') AS aud, COUNT(*) AS cnt "
            f"FROM knowledge_items{where_sql} "
            "GROUP BY aud ORDER BY cnt DESC"
        )
        rows = self.conn.execute(sql, params).fetchall()
        return {r[0]: r[1] for r in rows}

    def stats_breakdown(
        self,
        *,
        exclude_personal: bool = False,
        user_groups: Optional[List[str]] = None,
        granted_domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Return ``{by_status, categories, by_domain, by_source_type}`` for the
        /api/memory/stats endpoint, honoring the same audience/MEMORY_DOMAIN
        visibility filter as ``count_items`` / ``count_by_tag``. (``total``,
        ``by_tag`` and ``by_audience`` have their own repo methods.) Moved off a
        raw ``_get_db`` connection so the aggregates hit the active backend."""
        where: List[str] = []
        params: List[Any] = []
        if exclude_personal:
            where.append("(is_personal = FALSE OR is_personal IS NULL)")
        if user_groups is not None:
            visibility = ["audience IS NULL", "audience = 'all'"]
            if user_groups:
                ph = ",".join(["?"] * len(user_groups))
                visibility.append(f"audience IN ({ph})")
                params.extend(user_groups)
            if granted_domains:
                dph = ",".join(["?"] * len(granted_domains))
                visibility.append(
                    "EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                    "WHERE kid.item_id = knowledge_items.id "
                    f"AND kid.domain_id IN ({dph}))"
                )
                params.extend(granted_domains)
            where.append("(" + " OR ".join(visibility) + ")")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        by_status = {
            r[0]: r[1] for r in self.conn.execute(
                f"SELECT COALESCE(status, 'unknown') AS s, COUNT(*) "
                f"FROM knowledge_items{where_sql} GROUP BY s", params,
            ).fetchall()
        }
        cat_rows = self.conn.execute(
            f"SELECT DISTINCT category FROM knowledge_items{where_sql} "
            f"{'AND' if where_sql else 'WHERE'} category IS NOT NULL", params,
        ).fetchall()
        categories = sorted(r[0] for r in cat_rows if r[0])
        by_domain = {
            r[0]: r[1] for r in self.conn.execute(
                "SELECT COALESCE(md.slug, 'unset') AS d, COUNT(*) "
                "FROM knowledge_items "
                "LEFT JOIN knowledge_item_domains kid ON kid.item_id = knowledge_items.id "
                "LEFT JOIN memory_domains md ON md.id = kid.domain_id"
                + (where_sql or "") + " GROUP BY d", params,
            ).fetchall()
        }
        by_source_type = {
            r[0]: r[1] for r in self.conn.execute(
                f"SELECT COALESCE(source_type, 'unknown') AS st, COUNT(*) "
                f"FROM knowledge_items{where_sql} GROUP BY st", params,
            ).fetchall()
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
        """Same-domain candidates for LLM-based contradiction judgment.

        Domain is the only narrowing applied at the SQL layer. Topic / content
        matching is delegated to the LLM judge in
        services.corporate_memory.contradiction.find_and_judge() — see ADR
        Decision 4. The brittle keyword-substring layer that used to live here
        was removed; it had recall holes (synonyms, paraphrases) and the
        domain conjunct alone is enough as a hard ACL.

        Personal items (`is_personal = TRUE`) are excluded unconditionally —
        the LLM call is a read site (and exfiltrates content to the external
        API), so ADR Decision 1 ("hard privacy boundary, not a UI hint")
        applies. Without this filter, personal item content would be
        serialized into every contradiction prompt and could be paraphrased
        into `knowledge_contradictions.suggested_resolution.merged_content`
        — bypassing the contributor-only visibility rule.
        """
        sql = """
            SELECT * FROM knowledge_items
            WHERE status IN ('approved', 'pending')
              AND (is_personal = FALSE OR is_personal IS NULL)
              AND id != ?
        """
        params: List[Any] = [new_item_id]
        if domain:
            domain_id = self._resolve_domain_slug(domain)
            if domain_id is None:
                # Unknown slug → no candidates (matches old WHERE domain=? semantic).
                return []
            sql += (
                " AND EXISTS (SELECT 1 FROM knowledge_item_domains kid "
                "WHERE kid.item_id = knowledge_items.id AND kid.domain_id = ?)"
            )
            params.append(domain_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return self._rows_to_dicts(self.conn.execute(sql, params).fetchall())
