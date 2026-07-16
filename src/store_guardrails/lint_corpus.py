"""Lexical duplicate recall for the skill linter (SL012).

The LLM-based duplicate review (Task 4's ``CraftCaller``) is the primary
signal, but it can only compare a new skill against candidates it's handed
— it doesn't itself search the marketplace. This module is that search: a
disposable, in-memory DuckDB FTS index built fresh on every call over the
current published-skills corpus, queried with BM25 over
``name + description + body``.

Building the index over the *body* (not just name/description) is the
whole point — an AI-authored resubmission with a fresh name and
description but a copy-pasted ``SKILL.md`` body is exactly the case a
name/description-only search misses and BM25-over-body catches.

In-memory only (a fresh ``:memory:`` handle via ``_open_duckdb`` per call):
no file-backed WAL/checkpoint concerns apply (that hazard, documented in
``src/fts.py``, is specific to the persisted ``system.duckdb``). Every failure mode —
missing extension, empty corpus, a bad PRAGMA — degrades to an empty
result. This is best-effort recall feeding an ``info``-severity finding,
never a hard requirement the linter must satisfy.
"""

from __future__ import annotations

import logging
from typing import TypedDict

import duckdb

from src.duckdb_conn import _open_duckdb

from src.fts import ensure_fts_loaded

logger = logging.getLogger(__name__)


class CorpusDoc(TypedDict):
    """One published-skill document in the duplicate-recall corpus."""

    id: str
    name: str
    description: str
    body: str  # SKILL.md text; "" if unreadable


def top_candidates(
    name: str,
    description: str,
    body: str,
    corpus: list[CorpusDoc],
    *,
    n: int,
    exclude_id: str | None = None,
) -> list[tuple[CorpusDoc, float]]:
    """Return the top ``n`` BM25-ranked lexical near-duplicates of a skill.

    Builds a throwaway in-memory FTS index over ``corpus`` and scores it
    against ``name + description + body`` (body truncated to 2000 chars —
    plenty for BM25's term-frequency signal, keeps the query cheap).

    ``exclude_id`` drops a document (typically the skill being linted, if
    it's already in the corpus, e.g. re-linting after edits) from the
    results without needing a smaller ``n``.

    Never raises: any DuckDB error (extension unavailable, index build
    failure, …) degrades to ``[]``. Best-effort, not a hard dependency.
    """
    if not corpus:
        return []

    by_id = {doc["id"]: doc for doc in corpus}
    query = f"{name} {description} {body[:2000]}"
    # At most one row is dropped by exclude_id (ids are unique), so
    # over-fetching by one covers the worst case where the excluded row
    # would otherwise have occupied a top-n slot.
    fetch_limit = n + (1 if exclude_id is not None else 0)

    conn: duckdb.DuckDBPyConnection | None = None
    try:
        # Route through the sanctioned helper (pins the session to UTC) rather
        # than a bare ``duckdb.connect`` — enforced by the tz regression guard.
        conn = _open_duckdb(":memory:")
        conn.execute("CREATE TABLE corpus(id VARCHAR, name VARCHAR, description VARCHAR, body VARCHAR)")
        conn.executemany(
            "INSERT INTO corpus VALUES (?, ?, ?, ?)",
            [(doc["id"], doc["name"], doc["description"], doc["body"]) for doc in corpus],
        )
        if not ensure_fts_loaded(conn):
            return []
        conn.execute(
            "PRAGMA create_fts_index("
            "'main.corpus', 'id', 'name', 'description', 'body', "
            "strip_accents=1, lower=1, overwrite=1)"
        )
        rows = conn.execute(
            "SELECT id, fts_main_corpus.match_bm25(id, ?) AS score "
            "FROM corpus "
            "WHERE fts_main_corpus.match_bm25(id, ?) IS NOT NULL "
            "ORDER BY score DESC "
            "LIMIT ?",
            [query, query, fetch_limit],
        ).fetchall()
    except duckdb.Error as e:
        logger.warning("Lexical duplicate recall failed; degrading to empty: %s", e)
        return []
    finally:
        if conn is not None:
            conn.close()

    results: list[tuple[CorpusDoc, float]] = []
    for doc_id, score in rows:
        if exclude_id is not None and doc_id == exclude_id:
            continue
        doc = by_id.get(doc_id)
        if doc is None:
            continue
        results.append((doc, float(score)))
        if len(results) >= n:
            break
    return results


def load_corpus() -> list[CorpusDoc]:
    """Build the duplicate-recall corpus from currently published skills.

    Pulls every ``approved`` ``type='skill'`` Store entity through the
    backend-agnostic repo factory and reads each one's baked ``SKILL.md``
    off disk via the Store's own plugin-dir + file-finder helpers (never
    forks that path logic). A missing/unreadable body degrades to ``""``
    — that skill still gets a corpus row (name + description can still
    match), it just can't contribute to the body-similarity signal.

    Never raises: any failure listing entities returns ``[]``; any
    failure reading one skill's body degrades that document's ``body``
    to ``""`` without dropping the whole load.
    """
    from app.api.store import _find_skill_md, _plugin_dir
    from src.repositories import store_entities_repo

    try:
        items, _total = store_entities_repo().list(
            type="skill",
            visibility_status=["approved"],
            limit=100_000,
        )
    except Exception:
        logger.exception("load_corpus: failed to list published skills")
        return []

    corpus: list[CorpusDoc] = []
    for item in items:
        entity_id = item.get("id")
        if not entity_id:
            continue

        body = ""
        try:
            skill_md = _find_skill_md(_plugin_dir(entity_id))
            if skill_md is not None:
                body = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.debug("load_corpus: could not read SKILL.md for %s", entity_id, exc_info=True)

        corpus.append(
            CorpusDoc(
                id=str(entity_id),
                name=item.get("name") or "",
                description=item.get("description") or "",
                body=body,
            )
        )
    return corpus


__all__ = ["CorpusDoc", "top_candidates", "load_corpus"]
