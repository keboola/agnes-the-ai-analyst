"""DuckLake session management — reader singleton, writer singleton, lifecycle.

``src/analytics_backend.py`` (task 1) resolves *which* analytics backend is
active and *where* the DuckLake catalog/data live; this module is where the
DuckDB sessions actually get opened. Two long-lived singletons, mirroring
the singleton pattern ``get_system_db`` / ``get_analytics_db()`` use in ``src/db.py``:

- :func:`get_ducklake_read` — the api-role reader: one process-wide attach,
  cursor-per-caller, so a caller's ``.close()`` only drops the cursor. Also
  ATTACHes each extract source (mirroring ``get_analytics_db_readonly()``)
  and re-runs the existing ``_reattach_remote_extensions`` seam so
  ``_remote_attach`` rows (BigQuery et al.) keep resolving the same way
  they do against the legacy backend.
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


def _attach_extract_sources(conn: duckdb.DuckDBPyConnection) -> None:
    """ATTACH each ``extract.duckdb`` under ``{DATA_DIR}/extracts`` as a
    read-only alias, mirroring ``src.db.get_analytics_db_readonly()``'s
    attach loop.

    Needed so :func:`src.db._reattach_remote_extensions` — reused as-is
    below — can read each source's ``_remote_attach`` table the same way
    it already does against the legacy backend (task 2 keeps that seam
    exactly as today; relocating ``_remote_attach`` into the control
    plane is out of scope here — see the wave-2G plan's task 3/4 notes).
    """
    extracts_dir = _extracts_dir()
    if not extracts_dir.exists():
        return
    for ext_dir in sorted(extracts_dir.iterdir()):
        if not ext_dir.is_dir() or not _SAFE_IDENTIFIER.match(ext_dir.name):
            continue
        db_file = ext_dir / "extract.duckdb"
        if not db_file.exists():
            continue
        try:
            conn.execute(f"ATTACH '{db_file}' AS {ext_dir.name} (READ_ONLY)")
        except Exception:
            pass


def get_ducklake_read() -> duckdb.DuckDBPyConnection:
    """Return a cursor on the shared, long-lived DuckLake reader singleton.

    Mirrors ``src.db.get_analytics_db()``'s singleton + cursor-per-caller
    shape: one attach per (catalog_dsn, data_path) configuration, callers
    get a ``.cursor()`` they can ``.close()`` without tearing down the
    underlying attach. Re-opens transparently when the effective catalog
    DSN or data path changes (e.g. a test fixture flips ``DATA_DIR`` or
    ``AGNES_DUCKLAKE_CATALOG_DSN`` across cases) — same contract
    ``get_analytics_db()`` has for a changed ``DATA_DIR``.

    On (re)open, also ATTACHes every extract source directory and runs
    the existing remote-extension re-attach hook (see
    :func:`_attach_extract_sources` / ``src.db._reattach_remote_extensions``)
    so ``_remote_attach``-backed remote-mode tables resolve. Newly added
    extract sources become visible only on the *next* (re)open — call
    :func:`close_ducklake_sessions` to force a refresh. Task 4 (reader
    path wiring into ``get_analytics_db_readonly()``) decides how/when
    that refresh is triggered in practice; this function only owns the
    session lifecycle.

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
            _attach_extract_sources(conn)
            _reattach_remote_extensions(conn, _extracts_dir())
            _read_conn = conn
            _read_key = key
        return _maybe_instrument(_read_conn.cursor(), "ducklake_read")


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
