"""DuckLake session management — reader singleton, writer singleton, lifecycle.

``src/analytics_backend.py`` (task 1) resolves *which* analytics backend is
active and *where* the DuckLake catalog/data live; this module is where the
DuckDB sessions actually get opened. Two long-lived singletons, mirroring
the singleton pattern ``get_system_db`` / ``get_analytics_db()`` use in ``src/db.py``:

- :func:`get_ducklake_read` — the api-role reader: one process-wide attach,
  cursor-per-caller, so a caller's ``.close()`` only drops the cursor
  (task 4 wires this into ``src.db.get_analytics_db_readonly()`` as the
  ``analytics.backend=ducklake`` dispatch target, documenting there why a
  long-lived attach + per-request cursor beats a per-request open for
  this backend). Local/materialized tables need no extra wiring per call
  — they live in the lake itself, so a fresh cursor already sees them via
  DuckLake's MVCC snapshot. Remote-mode (``query_mode='remote'``) tables
  are different: they have no lake-resident data, so on every call this
  ATTACHes (only) the extract sources that ``table_registry`` says carry
  a remote-mode row, re-runs ``_reattach_remote_extensions`` (refreshing
  short-lived BQ tokens), and creates a ``lake."main"."<name>"`` wrapper
  view per remote row — see :func:`_sync_remote_views`. Deliberately
  scoped to remote-only sources: attaching every extract source
  persistently (as this function did pre-task-4) collides with a
  connector that rewrites its extract.duckdb *in place* rather than via
  temp-file swap (Jira) — see :func:`_ensure_remote_extract_attach`.
- :func:`get_ducklake_write` — the worker-role writer (task 3's copy-ingest
  rebuild lands through this). Logically a separate singleton from the
  reader — different roles in the three-plane topology.

Both wrap the same underlying attach mechanics (``_attach_ducklake``):
``INSTALL/LOAD ducklake`` (+``postgres`` when the catalog target is a
Postgres DSN), then ``ATTACH 'ducklake:...' AS lake (DATA_PATH '...')``.
Memory caps + thread count reuse ``src.db._apply_memory_caps`` verbatim —
same per-connection budgeting rationale as the legacy analytics
connections (see the ``_*_MEMORY_LIMIT`` block in ``src/db.py``).

**Same-process file-catalog constraint (verified directly against DuckDB
1.5.2, not documented upstream):** DuckDB refuses to ``ATTACH`` the same
DuckLake *file*-catalog target twice from two different top-level
connections in one process — ``Binder Error: ... Unique file handle
conflict: Cannot attach "__ducklake_metadata_<alias>" - the database file
"<path>" is already attached`` — regardless of the alias used. A Postgres
catalog target does **not** hit this: two independent connections each
get their own libpq connection with no conflict (matches the spec's "one
connection per ATTACH" sizing). Since a file catalog is only ever valid
in single-process ``all`` mode anyway (multi-process requires an explicit
Postgres DSN — see ``app.startup_guards.validate_deployment``), the reader
and writer singletons **share one physical connection** when the
resolved catalog is a file path, and only diverge into independent
connections when it is a Postgres DSN. See :func:`_get_shared_file_catalog_conn`.

See docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md
§3.4 for the architecture this implements, and
docs/superpowers/plans/2026-07-19-three-plane-wave2g-ducklake.md task 2.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import duckdb

from src.analytics_backend import ducklake_catalog_dsn, ducklake_data_path, is_postgres_dsn
from src.db import (
    _apply_memory_caps,
    _get_data_dir,
    _maybe_instrument,
    _reattach_remote_extensions,
    _SAFE_IDENTIFIER,
)
from src.duckdb_conn import _open_duckdb
from src.orchestrator_security import escape_sql_string_literal

logger = logging.getLogger(__name__)

# The ATTACHed catalog is always exposed under this fixed alias — it is
# never user/config-controlled, so it needs no runtime identifier
# validation (unlike the extract-source aliases below, which come from
# directory names on disk).
_LAKE_ALIAS = "lake"

# Per-connection memory budgets, mirroring the ``_ANALYTICS_DB_MEMORY_LIMIT``
# / ``_ANALYTICS_RO_MEMORY_LIMIT`` split in ``src/db.py``: the writer is a
# single worker-side singleton (comparable to ``get_analytics_db()``), the
# reader is the long-lived per-api-replica attach (comparable to
# ``get_analytics_db_readonly()``, except long-lived rather than per-request
# — one budgeted connection per api replica instead of one per request).
_DUCKLAKE_WRITE_MEMORY_LIMIT = "1500MB"
_DUCKLAKE_READ_MEMORY_LIMIT = "1GB"

_read_lock = threading.Lock()
_read_conn: duckdb.DuckDBPyConnection | None = None
_read_key: tuple[str, str] | None = None

_write_lock = threading.Lock()
_write_conn: duckdb.DuckDBPyConnection | None = None
_write_key: tuple[str, str] | None = None

# The single physical connection shared by reader + writer when the
# resolved catalog is a DuckDB-file target — see the module docstring's
# "same-process file-catalog constraint" note.
_shared_file_lock = threading.Lock()
_shared_file_conn: duckdb.DuckDBPyConnection | None = None
_shared_file_key: tuple[str, str] | None = None

_available_lock = threading.Lock()
_available_cache: bool | None = None


# Re-exported under the original private name: the predicate itself now
# lives in ``src.analytics_backend.is_postgres_dsn`` so
# ``app.startup_guards.validate_deployment`` shares the exact same
# postgres-DSN check as the attach path below, instead of the narrower
# ``str.startswith(("postgresql://", "postgres://"))`` it used to run,
# which silently rejected the SQLAlchemy ``+driver`` form
# (``postgresql+psycopg://...``) that a copied ``DATABASE_URL`` uses.
# Kept as a module-level alias (rather than inlining ``is_postgres_dsn``
# at each call site) so existing internal call sites and
# ``tests/test_ducklake_session.py::test_is_postgres_dsn_detects_url_forms_and_rejects_file_paths``
# keep importing ``src.ducklake_session._is_postgres_dsn`` unchanged.
_is_postgres_dsn = is_postgres_dsn


def _libpq_escape(value: str) -> str:
    """Quote+escape a single libpq keyword/value pair's value.

    libpq's keyword/value DSN syntax accepts a bare token OR a
    single-quoted one (backslash-escaping ``\\`` and ``'`` inside the
    quotes); always quoting sidesteps having to special-case empty,
    whitespace-containing, or otherwise-special values.
    """
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def pg_dsn_to_libpq(dsn: str) -> str:
    """Convert a URL-form Postgres DSN into libpq keyword form.

    ``ducklake_catalog_dsn()`` (task 1) returns an explicit Postgres
    target in URL form — the same shape ``DATABASE_URL`` uses elsewhere
    in this codebase: ``postgresql://[user[:pass]@][host][:port]/dbname[?param=value...]``,
    optionally with a SQLAlchemy ``+driver`` suffix (``postgresql+psycopg://``).
    DuckDB's ``ducklake:postgres:<dsn>`` ATTACH form expects libpq keyword
    form (``dbname=... host=... user=...``) — the POC found the URL form
    is not reliably accepted through ducklake's ATTACH path — so this is
    the converter, applied unconditionally whenever the catalog DSN is a
    Postgres URL.

    Handles the pgserver test-fixture's unix-socket URL shape, where the
    socket directory rides in the ``host`` *query* parameter rather than
    the netloc (``postgresql://user:@/db?host=/path/to/socket/dir``) —
    the ``host`` query param wins over any netloc hostname when both are
    present, since a netloc-less unix-socket URL still has to route
    somewhere. Any other query parameter (``sslmode``, ``options``, ...)
    passes through verbatim as an additional libpq keyword.

    ``urlparse().username`` / ``.password`` return the RAW (still
    percent-encoded) netloc substrings — unlike ``parse_qs``, which
    unquotes query values automatically. A password containing ``@``,
    ``:``, or a space is percent-encoded in any spec-compliant DSN
    (SQLAlchemy always renders it that way), so username/password/
    hostname are explicitly ``unquote()``'d here before libpq-escaping;
    skipping that step would hand libpq the literal percent-escapes
    instead of the real credential.
    """
    parsed = urlparse(dsn)
    scheme = parsed.scheme.split("+", 1)[0]
    if scheme not in ("postgresql", "postgres"):
        raise ValueError(f"not a postgres DSN: {dsn!r}")

    query = parse_qs(parsed.query)
    parts: list[str] = []

    dbname = unquote(parsed.path.lstrip("/"))
    if dbname:
        parts.append(f"dbname={_libpq_escape(dbname)}")
    if parsed.username:
        parts.append(f"user={_libpq_escape(unquote(parsed.username))}")
    if parsed.password:
        parts.append(f"password={_libpq_escape(unquote(parsed.password))}")

    host = (query.get("host") or [None])[0] or (unquote(parsed.hostname) if parsed.hostname else None)
    if host:
        parts.append(f"host={_libpq_escape(host)}")
    if parsed.port:
        parts.append(f"port={parsed.port}")

    for key, values in query.items():
        if key == "host":
            continue
        parts.append(f"{key}={_libpq_escape(values[0])}")

    return " ".join(parts)


def _attach_target(catalog_dsn: str) -> str:
    """Build the ``ducklake:...`` ATTACH target string for *catalog_dsn*.

    A bare filesystem path (the single-process default from
    ``ducklake_catalog_dsn()``) becomes ``ducklake:<path>``; an explicit
    Postgres DSN becomes ``ducklake:postgres:<libpq-keyword-dsn>`` via
    :func:`pg_dsn_to_libpq`.
    """
    if _is_postgres_dsn(catalog_dsn):
        return f"ducklake:postgres:{pg_dsn_to_libpq(catalog_dsn)}"
    return f"ducklake:{catalog_dsn}"


def _attach_ducklake(conn: duckdb.DuckDBPyConnection, *, catalog_dsn: str, data_path: str) -> None:
    """INSTALL/LOAD the ``ducklake`` extension (+ ``postgres`` for a PG
    catalog) and ``ATTACH`` the catalog under :data:`_LAKE_ALIAS` on *conn*.

    Shared by both the reader and writer singleton openers below —
    the attach mechanics are identical; only the memory budget and the
    extra reader-side extract/remote-extension wiring differ.
    """
    conn.execute("INSTALL ducklake")
    conn.execute("LOAD ducklake")
    if _is_postgres_dsn(catalog_dsn):
        conn.execute("INSTALL postgres")
        conn.execute("LOAD postgres")
    Path(data_path).mkdir(parents=True, exist_ok=True)
    target = escape_sql_string_literal(_attach_target(catalog_dsn))
    data_path_escaped = escape_sql_string_literal(data_path)
    conn.execute(f"ATTACH '{target}' AS {_LAKE_ALIAS} (DATA_PATH '{data_path_escaped}')")


def _get_shared_file_catalog_conn(catalog_dsn: str, data_path: str) -> duckdb.DuckDBPyConnection:
    """Return the single process-wide connection attached to a DuckDB-file
    DuckLake catalog, opening/re-opening it on first use or config change.

    See the module docstring's "same-process file-catalog constraint" —
    DuckDB refuses a second same-process ATTACH of the same catalog file
    even under a different connection/alias, so both
    :func:`get_ducklake_read` and :func:`get_ducklake_write` route through
    this single physical connection whenever the resolved catalog is a
    file path (never for a Postgres DSN, which does not hit the
    restriction). The writer's memory budget is applied since the shared
    connection carries whichever role(s) are active in this process.
    """
    global _shared_file_conn, _shared_file_key
    key = (catalog_dsn, data_path)
    with _shared_file_lock:
        if _shared_file_conn is None or _shared_file_key != key:
            if _shared_file_conn is not None:
                try:
                    _shared_file_conn.close()
                except Exception:
                    pass
            conn = _open_duckdb(":memory:")
            _apply_memory_caps(conn, _DUCKLAKE_WRITE_MEMORY_LIMIT, label="ducklake_shared_file_catalog")
            _attach_ducklake(conn, catalog_dsn=catalog_dsn, data_path=data_path)
            _shared_file_conn = conn
            _shared_file_key = key
        return _shared_file_conn


def _extracts_dir() -> Path:
    return _get_data_dir() / "extracts"


def _remote_registry_rows_by_source() -> dict[str, list[dict]]:
    """Group ``table_registry`` rows with ``query_mode='remote'`` by
    ``source_type`` (== the extract source's directory name under
    ``{DATA_DIR}/extracts`` — every connector writes its extract to
    ``extracts/<source_type>/``, one directory per connector, never
    per-instance; see ``app/api/sync.py``'s ``bq_output_dir`` /
    ``kb_output_dir`` constants).

    ``table_registry`` (system.duckdb/PG) is the task-4-decided source of
    truth for "which sources need a remote attach" — not a filesystem
    probe of each extract.duckdb's own ``_remote_attach`` table. This
    matters: it lets the reader skip touching a purely local-mode
    source's extract.duckdb *entirely*, which is the fix for the
    single-process collision described in :func:`_ensure_remote_extract_attach`.
    """
    from src.repositories import table_registry_repo

    try:
        rows = table_registry_repo().list_all()
    except Exception as e:
        logger.debug("could not list table_registry for ducklake remote-view sync: %s", e)
        return {}

    by_source: dict[str, list[dict]] = {}
    for r in rows:
        if (r.get("query_mode") or "") != "remote":
            continue
        source_type = r.get("source_type") or ""
        name = r.get("name") or ""
        if not source_type or not name:
            continue
        by_source.setdefault(source_type, []).append(r)
    return by_source


def _ensure_remote_extract_attach(conn: duckdb.DuckDBPyConnection, remote_by_source: dict[str, list[dict]]) -> None:
    """ATTACH, read-only, only the extract sources ``remote_by_source``
    names — i.e. only sources with at least one ``query_mode='remote'``
    table_registry row — and only if not already attached.

    **Why not attach every extract source (what this function did before
    task 4), and why not even a filesystem probe of every source:** a
    local-mode-only source's extract.duckdb can be rewritten *in place*
    by its connector — e.g. ``connectors/jira/extract_init.py::update_meta``
    opens ``extract.duckdb`` read-write (no temp-file swap) on every
    webhook. DuckDB refuses a read-write open while *any* other
    connection — even read-only, even same-process — already has that
    file open ("Conflicting lock is held"). The legacy per-request
    ``get_analytics_db_readonly()`` gets away with attaching every source
    because its attach is closed at the end of the same request; this
    reader's attach is process-lifetime, so holding it on a source Jira
    (or any future live-in-place connector) mutates in-place would wedge
    every subsequent webhook. The batch extractors that *do* need a
    persistent attach here (Keboola, BigQuery) never write in place —
    they always build ``extract.duckdb.tmp`` and atomically swap it in
    (see ``connectors/keboola/extractor.py::run`` /
    ``connectors/bigquery/extractor.py::_init_extract_locked``), which
    never conflicts with an existing reader holding the old inode open —
    so it is safe to hold their attach indefinitely. Restricting the
    persistent-attach set to exactly "sources with a remote-mode table"
    (via table_registry, see :func:`_remote_registry_rows_by_source`)
    happens to select exactly this safe subset, since only batch
    connectors (Keboola direct-bucket, BigQuery) ever emit
    ``query_mode='remote'`` rows.
    """
    extracts_dir = _extracts_dir()
    if not extracts_dir.exists() or not remote_by_source:
        return
    try:
        attached_dbs = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    except Exception:
        return
    for source_name in sorted(remote_by_source):
        if not _SAFE_IDENTIFIER.match(source_name) or source_name in attached_dbs:
            continue
        db_file = extracts_dir / source_name / "extract.duckdb"
        if not db_file.exists():
            continue
        try:
            conn.execute(f"ATTACH '{db_file}' AS {source_name} (READ_ONLY)")
        except Exception as e:
            logger.debug("Could not attach remote-mode extract source %s: %s", source_name, e)


def _ensure_remote_views(conn: duckdb.DuckDBPyConnection, remote_by_source: dict[str, list[dict]]) -> None:
    """Ensure a ``lake."main"."<name>"`` view exists for every
    ``query_mode='remote'`` table_registry row, pointing at the extract
    source's own already-correct inner view
    (``"<source>"."<name>"``, i.e. exactly what the legacy master view
    references — ``{source}."{table}"`` — per ``src.db.get_analytics_db_readonly``).

    Deliberately does NOT re-derive BigQuery/Keboola addressing (bq_fqn
    overrides, cross-project routing, BASE TABLE vs VIEW/MATERIALIZED_VIEW
    ``bigquery_query()`` wrapping — see ``connectors/bigquery/extractor.py``)
    here. That logic already lives in the extractor and stays there; this
    just re-exposes its already-built inner view under ``lake.main`` via a
    thin wrapper, the same way task 3's writer wraps copy-ingested local
    tables (``CREATE OR REPLACE VIEW lake."main"."<table>" AS SELECT * FROM
    lake."<source>"."<table>"``).

    Idempotent (``CREATE OR REPLACE VIEW``) and cheap (no data movement) —
    safe to call on every :func:`get_ducklake_read` cursor fetch, which is
    what keeps a newly-registered remote table visible on the very next
    query without waiting for the reader session to fully reopen.
    """
    for source_name, rows in remote_by_source.items():
        if not _SAFE_IDENTIFIER.match(source_name):
            continue
        for row in rows:
            name = row.get("name") or ""
            if not _SAFE_IDENTIFIER.match(name):
                continue
            try:
                conn.execute(
                    f'CREATE OR REPLACE VIEW {_LAKE_ALIAS}."main"."{name}" AS SELECT * FROM {source_name}."{name}"'
                )
            except Exception as e:
                logger.debug("Could not create lake.main remote view for %s.%s: %s", source_name, name, e)


def _sync_remote_views(conn: duckdb.DuckDBPyConnection) -> None:
    """Full per-request remote-table sync: attach any not-yet-attached
    remote-mode extract source, refresh remote extensions/tokens
    (``src.db._reattach_remote_extensions``), and (re)create each
    remote-mode table's ``lake.main`` wrapper view.

    Called on every :func:`get_ducklake_read` cursor fetch — see that
    function's docstring for why this needs to run per-request rather
    than only at session (re)open (BQ token TTL, and newly registered
    remote tables should not need a full reader restart to appear).
    """
    remote_by_source = _remote_registry_rows_by_source()
    if not remote_by_source:
        return
    _ensure_remote_extract_attach(conn, remote_by_source)
    _reattach_remote_extensions(conn, _extracts_dir())
    _ensure_remote_views(conn, remote_by_source)


def get_ducklake_read() -> duckdb.DuckDBPyConnection:
    """Return a cursor on the shared, long-lived DuckLake reader singleton.

    Mirrors ``src.db.get_analytics_db()``'s singleton + cursor-per-caller
    shape: one attach per (catalog_dsn, data_path) configuration, callers
    get a ``.cursor()`` they can ``.close()`` without tearing down the
    underlying attach. Re-opens transparently when the effective catalog
    DSN or data path changes (e.g. a test fixture flips ``DATA_DIR`` or
    ``AGNES_DUCKLAKE_CATALOG_DSN`` across cases) — same contract
    ``get_analytics_db()`` has for a changed ``DATA_DIR``.

    **Remote-table (``query_mode='remote'``) wiring — refreshed on
    EVERY call, not just (re)open:** unlike local/materialized tables
    (copy-ingested straight into the lake by task 3's writer, so a fresh
    cursor sees them via DuckLake's own MVCC snapshot with no extra
    work), remote tables have no lake-resident data — this function
    builds a ``lake."main"."<name>"`` wrapper view for each
    ``table_registry`` row with ``query_mode='remote'`` on every call
    (see :func:`_sync_remote_views`). Three reasons this runs per-call
    rather than only at session (re)open:

    1. **BQ token TTL.** The GCE metadata token backing the BigQuery
       ATTACH's ``ACCESS_TOKEN`` secret expires (~1h). This reader's
       physical connection lives for the process's lifetime, so a
       (re)open-only refresh would silently start failing BQ queries
       once the first token expired.
    2. **Newly-registered remote tables should not need a process
       restart to appear** — matches the legacy ``get_analytics_db_readonly()``
       behavior of re-attaching everything (and thus picking up registry
       changes) on every single request.
    3. **Single-process Jira-class collision avoidance.** Only extract
       sources that table_registry says have a remote-mode table are
       ever ATTACHed here (see :func:`_ensure_remote_extract_attach`) —
       a purely local-mode source (e.g. Jira, whose
       ``connectors/jira/extract_init.py::update_meta`` rewrites its
       ``extract.duckdb`` *in place*, not via temp-file swap) is never
       attached by this long-lived reader, so it never wedges that
       connector's live webhook writes with a "Conflicting lock" error.
       Remote-mode sources (Keboola direct-bucket, BigQuery) are safe to
       hold attached indefinitely — their extractors always write a
       ``.tmp`` file and atomically swap it in.

    This per-call work is cheap: it only touches the (typically very
    small) set of sources with at least one remote-mode row, and the
    view/attach mutations are idempotent (``CREATE OR REPLACE`` / skip if
    already attached).

    Every returned cursor also runs ``USE lake`` so an unqualified
    ``SELECT ... FROM "<table>"`` resolves against ``lake.main`` — the
    schema task 3's writer creates master views in — the same way a
    query against the legacy ``server.duckdb`` file resolves unqualified
    names against its own default catalog/schema.

    When the resolved catalog is a DuckDB-file target, the underlying
    physical connection is shared with :func:`get_ducklake_write` (see the
    module docstring) — closing *this* cursor never affects that shared
    connection; only :func:`close_ducklake_sessions` tears it down.
    """
    global _read_conn, _read_key
    catalog_dsn = ducklake_catalog_dsn()
    data_path = ducklake_data_path()
    key = (catalog_dsn, data_path)

    with _read_lock:
        if _read_conn is None or _read_key != key:
            old = _read_conn
            if old is not None and old is not _shared_file_conn:
                try:
                    old.close()
                except Exception:
                    pass
            if _is_postgres_dsn(catalog_dsn):
                conn = _open_duckdb(":memory:")
                _apply_memory_caps(conn, _DUCKLAKE_READ_MEMORY_LIMIT, label="get_ducklake_read")
                _attach_ducklake(conn, catalog_dsn=catalog_dsn, data_path=data_path)
            else:
                conn = _get_shared_file_catalog_conn(catalog_dsn, data_path)
            _read_conn = conn
            _read_key = key
        _sync_remote_views(_read_conn)
        cursor = _read_conn.cursor()
        cursor.execute(f"USE {_LAKE_ALIAS}")
        return _maybe_instrument(cursor, "ducklake_read")


def get_ducklake_write() -> duckdb.DuckDBPyConnection:
    """Return a cursor on the shared, long-lived DuckLake writer singleton.

    Separate singleton from :func:`get_ducklake_read` — the worker's
    copy-ingest rebuild path (task 3) is the only writer, distinct from
    the api-role reader(s). No extract-source attach or remote-extension
    re-attach here: the writer's job is
    ``CREATE OR REPLACE TABLE lake."<source>"."<table>" AS SELECT * FROM
    read_parquet(...)`` against the source's own extract parquet files
    (resolved directly by the caller), not multi-source catalog reads.

    Same re-open-on-config-change contract as :func:`get_ducklake_read`.
    When the resolved catalog is a DuckDB-file target, this shares its
    underlying physical connection with :func:`get_ducklake_read` — see
    the module docstring's "same-process file-catalog constraint".
    """
    global _write_conn, _write_key
    catalog_dsn = ducklake_catalog_dsn()
    data_path = ducklake_data_path()
    key = (catalog_dsn, data_path)

    with _write_lock:
        if _write_conn is None or _write_key != key:
            old = _write_conn
            if old is not None and old is not _shared_file_conn:
                try:
                    old.close()
                except Exception:
                    pass
            if _is_postgres_dsn(catalog_dsn):
                conn = _open_duckdb(":memory:")
                _apply_memory_caps(conn, _DUCKLAKE_WRITE_MEMORY_LIMIT, label="get_ducklake_write")
                _attach_ducklake(conn, catalog_dsn=catalog_dsn, data_path=data_path)
            else:
                conn = _get_shared_file_catalog_conn(catalog_dsn, data_path)
            _write_conn = conn
            _write_key = key
        return _maybe_instrument(_write_conn.cursor(), "ducklake_write")


# The wave-2G plan text refers to these as ``open_ducklake_read()`` /
# ``open_ducklake_write()`` (the "open a session" verb); ``get_*`` matches
# the naming convention every other singleton accessor in ``src/db.py``
# uses (``get_system_db``, ``get_analytics_db``, ...). Both names are kept
# as the public surface — plain aliases, not separate implementations —
# so either wave-2G task or callsite that reaches for either spelling works.
open_ducklake_read = get_ducklake_read
open_ducklake_write = get_ducklake_write


def close_ducklake_sessions() -> None:
    """Close both DuckLake singletons (reader + writer).

    Mirrors ``src.db.close_singleton_connections()`` — used for lifecycle
    handoff (e.g. before a subprocess needs the catalog attach fresh, or a
    role/process shutdown) and by tests that need a clean re-open under a
    changed config. Idempotent; the next :func:`get_ducklake_read` /
    :func:`get_ducklake_write` call lazily re-opens.

    Reader and writer may (file-catalog case) point at the *same*
    physical connection object — collected by identity into a dict first
    so that connection is closed exactly once instead of twice.
    """
    global _read_conn, _read_key, _write_conn, _write_key, _shared_file_conn, _shared_file_key
    to_close: dict[int, duckdb.DuckDBPyConnection] = {}

    with _read_lock:
        if _read_conn is not None:
            to_close[id(_read_conn)] = _read_conn
        _read_conn = None
        _read_key = None
    with _write_lock:
        if _write_conn is not None:
            to_close[id(_write_conn)] = _write_conn
        _write_conn = None
        _write_key = None
    with _shared_file_lock:
        if _shared_file_conn is not None:
            to_close[id(_shared_file_conn)] = _shared_file_conn
        _shared_file_conn = None
        _shared_file_key = None

    for conn in to_close.values():
        try:
            conn.close()
        except Exception:
            pass


def ducklake_available() -> bool:
    """Probe whether the ``ducklake`` DuckDB extension can be installed
    and loaded in this environment.

    A throwaway in-memory connection tries ``INSTALL ducklake; LOAD
    ducklake;``. Used by startup guards / the migration command (later
    wave-2G tasks) to fail loud, before an operator flips
    ``analytics.backend: ducklake``, in an offline/air-gapped deployment
    where the extension can't be fetched.

    A successful probe is cached for the rest of the process lifetime —
    extension availability is a property of the DuckDB build/environment,
    not something that flips mid-process. A *failed* probe is deliberately
    NOT cached, so a transient failure (extension repo unreachable at
    startup) doesn't wedge an operator retry after connectivity returns;
    see :func:`reset_ducklake_available_cache` for tests that need to
    force a re-probe regardless.
    """
    global _available_cache
    with _available_lock:
        if _available_cache:
            return True
        try:
            # Route through _open_duckdb (not a bare duckdb.connect) so this
            # production call site honors the UTC-timezone pin like every
            # other — enforced by tests/test_duckdb_session_tz.py. The probe
            # writes nothing tz-sensitive, but the guard is blanket by design.
            probe = _open_duckdb(":memory:")
            try:
                probe.execute("INSTALL ducklake")
                probe.execute("LOAD ducklake")
            finally:
                probe.close()
        except Exception as e:
            logger.warning("ducklake extension probe failed: %s", e)
            return False
        _available_cache = True
        return True


def reset_ducklake_available_cache() -> None:
    """Drop the cached :func:`ducklake_available` probe result. Test-only."""
    global _available_cache
    with _available_lock:
        _available_cache = None
