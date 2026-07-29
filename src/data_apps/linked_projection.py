"""Generic projection: a materialized MCP-entity table → Agnes resource rows.

For linked data apps this upserts one ``data_apps`` ``linked`` row per external
app and soft-deletes rows whose app has disappeared upstream — scoped to one
connection so concurrent connections never clobber each other. The Keboola
specifics live in ``keboola_adapter``; this module is entity-agnostic apart from
importing that adapter's record shape, so a future entity reuses the pattern.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from src.data_apps import keboola_adapter as adapter

logger = logging.getLogger(__name__)


@dataclass
class ProjectionResult:
    created: int
    updated: int
    hidden: int


def project(
    connection_id: str,
    records: Sequence[adapter.LinkedAppRecord],
    *,
    repo: Optional[Any] = None,
) -> ProjectionResult:
    """Reconcile ``data_apps`` linked rows for ``connection_id`` to ``records``.

    Upserts each record (keyed by ``source_ref``), then hides any active linked
    row of this connection not present this round. Preserves ``description_override``
    and grants (the repo's ``upsert_linked``/``soft_delete_missing_linked``
    guarantee it). ``repo`` is injectable for tests; defaults to the factory.
    """
    if repo is None:
        from src.repositories import data_apps_repo

        repo = data_apps_repo()

    created = 0
    updated = 0
    keep: List[str] = []
    for rec in records:
        ref = adapter.source_ref(connection_id, rec.external_app_id)
        keep.append(ref)
        existed = repo.get_by_source_ref(ref) is not None
        repo.upsert_linked(
            slug=adapter.slug_for(connection_id, rec.external_app_id),
            source_ref=ref,
            name=rec.name,
            description=rec.description,
            external_url=rec.external_url,
        )
        if existed:
            updated += 1
        else:
            created += 1

    hidden = repo.soft_delete_missing_linked(
        source_ref_prefix=adapter.source_ref_prefix(connection_id),
        keep_source_refs=keep,
    )
    if hidden:
        logger.info(
            "linked projection [%s]: %d created, %d updated, hid %d apps no longer present upstream",
            connection_id,
            created,
            updated,
            hidden,
        )
    return ProjectionResult(created=created, updated=updated, hidden=hidden)


def project_from_extract(
    source_id: str,
    extract_duckdb_path: Optional[str],
    *,
    repo: Optional[Any] = None,
) -> Optional[ProjectionResult]:
    """Project linked apps from an MCP source's freshly-materialized
    ``extract.duckdb``.

    No-op (returns ``None``) unless the extract actually carries the
    ``keboola_data_apps`` materialized table — so MCP sources that don't run the
    data-app lister are unaffected. Rows missing an id or URL (can't be linked)
    are skipped. Called right after ``extract_source_async`` in the materialize
    path; ``source_id`` is the connection id used for provenance/scoping.
    """
    from src.duckdb_conn import _open_duckdb

    if not extract_duckdb_path or not os.path.exists(str(extract_duckdb_path)):
        return None
    with _open_duckdb(str(extract_duckdb_path), read_only=True) as conn:
        tables = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        if adapter.MATERIALIZED_TABLE not in tables:
            return None
        cur = conn.execute(f"SELECT * FROM {adapter.MATERIALIZED_TABLE}")
        cols = [d[0] for d in cur.description]
        raw_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    records = [adapter.map_row(r) for r in raw_rows]
    linkable = [rec for rec in records if rec.external_app_id and rec.external_url]
    skipped = len(records) - len(linkable)
    if skipped:
        logger.info("linked projection [%s]: skipped %d row(s) missing id/url", source_id, skipped)
    return project(source_id, linkable, repo=repo)
