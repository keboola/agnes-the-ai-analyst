"""DuckDB FTS extension helpers for knowledge-item BM25 search (issue #121).

The extension is per-connection: ``INSTALL fts`` is persisted at the engine
level, ``LOAD fts`` must run on every fresh DuckDB connection. The index
over ``knowledge_items`` is a *static snapshot* — DuckDB doesn't track
base-table INSERT / UPDATE / DELETE automatically.

We rebuild on demand inside ``search()`` (cheap at corpus sizes
< a few thousand rows; acceptance bound is sub-100ms) and fall back to
``ILIKE`` when the extension can't be loaded — offline installs and
sandboxed CI runners that block extension downloads must still serve
the search box, just without relevance ranking.

``strip_accents=1`` lets queries like ``cesky`` match documents
containing ``česky`` — the Czech-diacritics acceptance from #121.
"""

from __future__ import annotations

import logging

import duckdb

logger = logging.getLogger(__name__)


def ensure_fts_loaded(conn: duckdb.DuckDBPyConnection) -> bool:
    """``INSTALL`` + ``LOAD`` the DuckDB ``fts`` extension on ``conn``.

    Idempotent: re-running on a connection that already has the extension
    loaded is a no-op. Returns ``True`` on success, ``False`` on any
    DuckDB error (network-blocked install, sandboxed CI runner without
    extension repo access, etc.). Callers should fall back to ``ILIKE``
    on ``False``.
    """
    try:
        conn.execute("INSTALL fts")
        conn.execute("LOAD fts")
        return True
    except duckdb.Error as e:
        logger.warning(
            "DuckDB fts extension unavailable; knowledge search will fall back to ILIKE: %s",
            e,
        )
        return False


def ensure_knowledge_fts_index(conn: duckdb.DuckDBPyConnection) -> bool:
    """Create (or rebuild) the BM25 FTS index over ``knowledge_items``.

    The index covers ``title`` and ``content``, keyed by ``id``.
    ``overwrite=1`` makes the call idempotent: if the index already
    exists it's dropped and rebuilt from the current snapshot — which is
    how we keep it in sync with INSERT/UPDATE/DELETE without per-row
    update hooks (DuckDB FTS doesn't ship those).

    ``strip_accents=1`` + ``lower=1`` give us case- and diacritic-
    insensitive matching out of the box (``cesky`` → ``česky``).

    Returns ``True`` if the index is now usable, ``False`` if either
    the extension or the PRAGMA call failed. Falsy return is the signal
    for ``KnowledgeRepository.search`` to use the ILIKE path.
    """
    if not ensure_fts_loaded(conn):
        return False
    try:
        conn.execute(
            "PRAGMA create_fts_index("
            "'main.knowledge_items', 'id', 'title', 'content', "
            "strip_accents=1, lower=1, overwrite=1)"
        )
        # Flush the FTS DDL into the main DB file immediately. create_fts_index
        # DROPs + CREATEs the multi-table ``fts_main_knowledge_items`` schema,
        # and those ops sit in ``system.duckdb.wal`` until the next checkpoint.
        # If the process is killed (OOM, short deploy grace) before then, the
        # WAL persists and DuckDB's replay fails on the FTS-schema drop
        # ordering ("Cannot drop entry fts_main_knowledge_items ... depends on
        # it"), forcing a destructive recovery. Checkpointing here keeps that
        # DDL out of the WAL. Best-effort: a concurrent write txn or read-only
        # handle can make CHECKPOINT raise — that must not break search (the
        # WAL-discard recovery in src/db.py is the safety net).
        try:
            conn.execute("CHECKPOINT")
        except duckdb.Error as ckpt_err:
            logger.debug(
                "FTS index created but CHECKPOINT failed (%s); WAL flush deferred",
                ckpt_err,
            )
        return True
    except duckdb.Error as e:
        logger.warning(
            "Failed to (re)create FTS index on knowledge_items; falling back to ILIKE: %s",
            e,
        )
        return False
