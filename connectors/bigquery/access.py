"""Single entry point for BigQuery access — config resolution, client construction,
DuckDB-extension session, and Google-API error translation.

See docs/superpowers/specs/2026-04-29-issue-134-bq-access-unify-design.md for the
full design rationale.
"""
from __future__ import annotations

import functools
import logging
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BqProjects:
    """Pair of GCP project IDs used by Agnes.

    `billing` is the project the BQ client bills jobs to (also used as quota_project_id).
    `data` is the default data project for FROM-clause construction. Today equal to
    instance.yaml `data_source.bigquery.project`; locked to a single project per instance
    until table_registry grows a per-table source_project column. See spec "Non-goals".
    """
    billing: str
    data: str


class BqAccessError(Exception):
    """Typed error for BQ access failures.

    `kind` is one of HTTP_STATUS keys; endpoint translation maps it to status codes.
    """

    HTTP_STATUS = {
        "not_configured":          500,  # admin/config bug — page on-call
        "bq_lib_missing":          500,  # deployment bug
        "auth_failed":             502,  # GCP metadata server unreachable
        "cross_project_forbidden": 502,  # SA lacks serviceusage.services.use on billing project
        "bq_forbidden":            502,  # other Forbidden from BQ
        "bq_bad_request":          400,  # 400 from BQ when caller flagged it as client-derived
        "bq_upstream_error":       502,  # all other upstream BQ failures
        # `responseTooLarge` is a BQ refusal whose root cause is query shape
        # (the user asked for too many rows back inline), not auth or syntax.
        # 400 with a specific actionable hint instead of the generic
        # bq_bad_request / bq_upstream_error mappings, which surfaced the
        # raw BQ message and gave operators no path forward.
        "bq_response_too_large":   400,
    }

    def __init__(self, kind: str, message: str, details: dict | None = None):
        self.kind = kind
        self.message = message
        self.details = details or {}
        super().__init__(message)


_RESPONSE_TOO_LARGE_HINT = (
    "BigQuery refused to return the result inline; the query exceeded BQ's "
    "response size limit. Narrow the WHERE clause, aggregate further, "
    "select fewer columns, or query a materialized table that's already "
    "been bounded server-side."
)


def _classify_response_too_large(msg: str, projects: BqProjects) -> BqAccessError:
    """Build the `bq_response_too_large` BqAccessError with the canonical
    actionable hint and the original BQ message preserved in details for
    operator debugging."""
    return BqAccessError(
        "bq_response_too_large",
        _RESPONSE_TOO_LARGE_HINT,
        details={
            "original": msg,
            "billing_project": projects.billing,
            "data_project": projects.data,
        },
    )


def _is_response_too_large(msg: str) -> bool:
    """Detect BQ's `responseTooLarge` failure mode by message substring.

    The reason code is stable across HTTP transports (gax.BadRequest from
    google-cloud-bigquery, duckdb.IOException from the BQ extension's own
    HTTP layer); both surface 'Response too large to return' verbatim in
    the message body. Match case-insensitively + tolerate the slight
    variant 'response too large' that some surfaces emit without the
    'to return' suffix.
    """
    ml = msg.lower()
    return "response too large" in ml


def translate_bq_error(
    e: Exception,
    projects: BqProjects,
    *,
    bad_request_status: Literal["client_error", "upstream_error"],
) -> BqAccessError:
    """Convert Google API exceptions into a typed BqAccessError.

    Mapping (FIRST match wins):
      1. BqAccessError                    -> pass through unchanged (CRITICAL: bq.client()
                                             and bq.duckdb_session() can raise BqAccessError
                                             directly for bq_lib_missing / auth_failed; those
                                             must round-trip without reclassification)
      2. Forbidden + 'serviceusage' in str(e).lower()
                                          -> cross_project_forbidden (with hint)
      3. Forbidden                        -> bq_forbidden
      4. 'response too large' in str(e).lower()
                                          -> bq_response_too_large (HTTP 400, with
                                             actionable hint pointing at WHERE /
                                             aggregate / materialized remediations)
      5. BadRequest, bad_request_status='client_error'
                                          -> bq_bad_request (HTTP 400)
      6. BadRequest, bad_request_status='upstream_error'
                                          -> bq_upstream_error (HTTP 502)
      7. GoogleAPICallError (other)       -> bq_upstream_error
      8. Anything else                    -> RE-RAISED unchanged (don't swallow programmer errors)

    The `responseTooLarge` mapping (4) sits ahead of the generic BadRequest
    cases on purpose: BQ surfaces this failure mode as a 400 with a
    specific reason, but the actionable remediation is "shape your query
    differently" — not "your SQL has a syntax error" (the typical
    bq_bad_request user-facing meaning) and not "BQ is broken"
    (bq_upstream_error). Routing it via its own kind keeps the user-facing
    message tight + correct.
    """
    if isinstance(e, BqAccessError):
        return e

    try:
        from google.api_core import exceptions as gax  # type: ignore
    except ImportError:
        # No google lib installed → can't classify Google errors. Re-raise.
        raise e

    msg = str(e)

    if isinstance(e, gax.Forbidden):
        if "serviceusage" in msg.lower():
            return BqAccessError(
                "cross_project_forbidden",
                msg,
                details={
                    "billing_project": projects.billing,
                    "data_project": projects.data,
                    "hint": (
                        "Set data_source.bigquery.billing_project in instance.yaml to a project "
                        "where the SA has serviceusage.services.use, or grant the SA that role "
                        "on the data project."
                    ),
                },
            )
        return BqAccessError(
            "bq_forbidden",
            msg,
            details={"billing_project": projects.billing, "data_project": projects.data},
        )

    # Special-case: `responseTooLarge` arrives as gax.BadRequest (HTTP 400)
    # but has a unique reason code with a specific, actionable remediation.
    # Catch it BEFORE the generic BadRequest mapping below so it doesn't
    # surface as a confusing "bad request" (which implies bad SQL).
    if _is_response_too_large(msg):
        return _classify_response_too_large(msg, projects)

    if isinstance(e, gax.BadRequest):
        if bad_request_status == "client_error":
            return BqAccessError("bq_bad_request", msg)
        return BqAccessError("bq_upstream_error", msg)

    if isinstance(e, gax.GoogleAPICallError):
        return BqAccessError("bq_upstream_error", msg)

    # Last-resort heuristic: the DuckDB bigquery extension is a C++ plugin that
    # makes its own HTTP calls (not via google-cloud-bigquery), so BQ HTTP errors
    # arrive as DuckDB-native exceptions (e.g. duckdb.IOException) rather than
    # google.api_core types — `bigquery_query()` paths in v2_scan/sample/schema
    # would otherwise fall through to the re-raise below and surface as bare 500
    # in production. String-match common BQ HTTP error patterns. Devin ANALYSIS
    # on PR #138 review.
    msg_lower = msg.lower()
    if "forbidden" in msg_lower or " 403 " in msg_lower or "403:" in msg_lower:
        if "serviceusage" in msg_lower:
            return BqAccessError(
                "cross_project_forbidden",
                msg,
                details={
                    "billing_project": projects.billing,
                    "data_project": projects.data,
                    "hint": (
                        "Set data_source.bigquery.billing_project in instance.yaml to a project "
                        "where the SA has serviceusage.services.use, or grant the SA that role "
                        "on the data project."
                    ),
                },
            )
        return BqAccessError(
            "bq_forbidden",
            msg,
            details={"billing_project": projects.billing, "data_project": projects.data},
        )
    if "bad request" in msg_lower or " 400 " in msg_lower or "400:" in msg_lower:
        if bad_request_status == "client_error":
            return BqAccessError("bq_bad_request", msg)
        return BqAccessError("bq_upstream_error", msg)

    # Don't swallow programmer errors / unknown exceptions
    raise e


def _default_client_factory(projects: BqProjects):
    """Real BigQuery client construction. Raises BqAccessError on import / auth / config issues.

    `bigquery.Client(...)` resolves Application Default Credentials at construction
    time; in environments without ADC (CI without service-account key, dev laptop
    that hasn't run `gcloud auth application-default login`) it raises
    `google.auth.exceptions.DefaultCredentialsError` synchronously. Translate to
    typed `BqAccessError(auth_failed)` so endpoints surface a structured 502 with
    a helpful hint instead of a raw stack trace.
    """
    try:
        from google.cloud import bigquery  # type: ignore
        from google.api_core.client_options import ClientOptions  # type: ignore
    except ImportError as e:
        raise BqAccessError(
            "bq_lib_missing",
            "google-cloud-bigquery is not installed",
            details={"original": str(e)},
        )

    try:
        from google.auth import exceptions as gauth_exc  # type: ignore
        auth_error_types: tuple = (gauth_exc.DefaultCredentialsError,)
    except ImportError:
        auth_error_types = ()

    try:
        return bigquery.Client(
            project=projects.billing,
            client_options=ClientOptions(quota_project_id=projects.billing),
        )
    except auth_error_types as e:
        raise BqAccessError(
            "auth_failed",
            f"GCP credentials unavailable: {e}",
            details={
                "original": str(e),
                "hint": (
                    "Run `gcloud auth application-default login` for local dev, or set "
                    "GOOGLE_APPLICATION_CREDENTIALS to a service-account key in the deployment."
                ),
            },
        )


def _default_pool_size() -> int:
    """Resolve the BQ DuckDB-extension session pool size from instance.yaml.

    Reads ``data_source.bigquery.session_pool_size`` (default 4). Sentinel
    ``0`` disables pooling (every acquire builds + closes a fresh session;
    matches pre-pool behavior). Negative / non-numeric values fall back to
    the default — the pool is a perf optimization, not a correctness
    boundary, so an unparseable config shouldn't fail-stop the app.
    """
    try:
        from app.instance_config import get_value
    except Exception:
        return 4
    raw = get_value("data_source", "bigquery", "session_pool_size", default=4)
    try:
        n = int(raw) if raw is not None else 4
    except (TypeError, ValueError):
        logger.warning(
            "BQ session_pool_size=%r is not an int; falling back to default 4",
            raw,
        )
        return 4
    if n < 0:
        return 4
    return n


def _build_fresh_bq_session():
    """Build a single fresh in-memory DuckDB conn with the bigquery extension
    INSTALL/LOAD'd, the auth SECRET created from get_metadata_token(), and
    per-session settings applied. Translates auth / install failures to
    BqAccessError. Caller owns the close.

    Used internally by the pool; also used directly when pooling is disabled.
    """
    import duckdb  # type: ignore
    from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError

    try:
        token = get_metadata_token()
    except BQMetadataAuthError as e:
        raise BqAccessError(
            "auth_failed",
            f"could not fetch GCP metadata token: {e}",
            details={"original": str(e)},
        )

    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(
            f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')"
        )
    except Exception as e:
        # Build failed — must close the half-initialised conn, otherwise it
        # leaks across the pool's lifetime.
        try:
            conn.close()
        except Exception:
            pass
        raise BqAccessError(
            "bq_lib_missing",
            f"failed to install/load BigQuery DuckDB extension: {e}",
            details={"original": str(e)},
        )
    apply_bq_session_settings(conn)
    return conn


def _refresh_bq_secret(conn) -> None:
    """Refresh the auth SECRET on a pooled connection so token rotation
    (default GCE metadata token TTL ~1 hr) doesn't break long-lived
    pooled entries.

    Cheap when the token cache is warm (a few µs). Failures are
    non-fatal here — the pool's liveness probe + per-acquire build
    fallback will catch genuinely-broken entries.
    """
    from connectors.bigquery.auth import get_metadata_token
    try:
        token = get_metadata_token()
        escaped = token.replace("'", "''")
        conn.execute(
            f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')"
        )
    except Exception as e:
        # Bubble up so the pool drops this entry and rebuilds.
        raise BqAccessError(
            "auth_failed",
            f"could not refresh BQ secret on pooled session: {e}",
            details={"original": str(e)},
        )


def _is_pool_entry_alive(conn) -> bool:
    """Cheap liveness probe — `SELECT 1`. Returns False on any error so
    the pool reaper drops the entry and builds a fresh one."""
    try:
        result = conn.execute("SELECT 1").fetchone()
        return result is not None and result[0] == 1
    except Exception:
        return False


# Module-level pool state. Process-cached (mirrors get_bq_access's lifetime).
# Not fork-safe — single uvicorn worker process is the supported deployment
# shape per CLAUDE.md.
_pool: deque = deque()
_pool_lock = threading.Lock()


def _reset_session_pool_for_tests() -> None:
    """Drop and close every pooled entry. Test helper — production code
    should not call this. Exposed so test fixtures + the existing
    test_bq_access tests can pin pre-test pool state to empty."""
    with _pool_lock:
        while _pool:
            entry = _pool.popleft()
            try:
                entry.close()
            except Exception:
                pass


@contextmanager
def _default_duckdb_session_factory(projects: BqProjects):
    """Yield a pooled in-memory DuckDB conn with bigquery extension loaded
    + SECRET set from get_metadata_token(). Translates auth / install
    failures to BqAccessError(kind='auth_failed' or 'bq_lib_missing').

    Pooling: amortizes the ~0.5 s INSTALL/LOAD/ATTACH cost across requests
    by keeping pre-warmed connections in a bounded deque. Acquire reuses
    an existing entry when available (refreshing its auth SECRET so
    token rotation doesn't break long-lived entries) and probes liveness
    cheaply via ``SELECT 1`` before handing it to the caller. On normal
    exit the connection returns to the pool; on exception it's closed
    instead (the underlying session may carry dirty state).

    Pool size is ``data_source.bigquery.session_pool_size`` (default 4;
    sentinel ``0`` disables pooling entirely, matching pre-pool
    behavior). Process-cached, not fork-safe.

    Note: `projects.billing` is not used by this factory directly — bigquery_query()
    callers pass it themselves as the first positional arg to identify the billing
    project. The factory keeps the parameter for symmetry with _default_client_factory.
    """
    pool_size = _default_pool_size()

    # Acquire: prefer a warm entry, fall back to fresh build.
    conn = None
    if pool_size > 0:
        while True:
            with _pool_lock:
                entry = _pool.popleft() if _pool else None
            if entry is None:
                break
            if not _is_pool_entry_alive(entry):
                # Reaper: drop broken entries.
                try:
                    entry.close()
                except Exception:
                    pass
                continue
            try:
                # Refresh the auth SECRET so a long-lived pool entry
                # doesn't keep a stale token past its TTL. Cheap when
                # the token cache is warm.
                _refresh_bq_secret(entry)
            except BqAccessError:
                try:
                    entry.close()
                except Exception:
                    pass
                continue
            # Re-apply session settings (`bq_query_timeout_ms`, …) on
            # every reuse so an operator's `/admin/server-config` change
            # propagates to pooled entries without requiring container
            # restart. Without this, a long-lived pool entry keeps the
            # value baked in at first build forever (devil's-advocate
            # R1 finding #3). `apply_bq_session_settings` is idempotent
            # and fail-soft — re-running on every acquire is cheap.
            try:
                apply_bq_session_settings(entry)
            except Exception:
                # apply_bq_session_settings is documented as never
                # raising for legitimate "extension doesn't recognise
                # setting" cases (it only logs). Defensive guard for
                # any unforeseen failure mode — keep the entry, the
                # caller's actual query may still succeed.
                pass
            conn = entry
            break

    if conn is None:
        conn = _build_fresh_bq_session()

    try:
        yield conn
    except Exception:
        # Caller saw an exception — the conn may be in a dirty state.
        # Don't return to pool; close to release native resources.
        try:
            conn.close()
        except Exception:
            pass
        raise
    else:
        # Normal exit — return to pool if there's room.
        if pool_size > 0:
            with _pool_lock:
                if len(_pool) < pool_size:
                    _pool.append(conn)
                    return
        # Pool disabled or full — close.
        try:
            conn.close()
        except Exception:
            pass


def apply_bq_session_settings(conn) -> None:
    """Apply per-session DuckDB BigQuery-extension settings from instance config.

    Currently sets ``bq_query_timeout_ms`` from
    ``data_source.bigquery.query_timeout_ms``. The extension default is 90 s,
    which is too tight for analyst-scale queries against view-backed BQ
    datasets — bumping the default to 600 s here. Sentinel ``0`` (or a
    non-numeric / unparseable value) leaves the extension default in place.

    Call AFTER ``LOAD bigquery`` on every DuckDB session that touches BQ:
    BqAccess's session factory, the standalone extractor in
    ``connectors/bigquery/extractor.py``, the orchestrator's
    ``_remote_attach`` path in ``src/orchestrator.py``, and ``src/db.py``'s
    read-only analytics-DB factory (called from ``_reattach_remote_extensions``
    plus a belt-and-suspenders call from ``get_analytics_db_readonly`` itself).

    SET failures are logged at WARNING level (previously silent) so operators
    can diagnose timeouts that surface as the extension default 90 s when the
    intended value was higher. The applied value is verified via
    ``current_setting('bq_query_timeout_ms')``; a mismatch is also logged.
    """
    try:
        from app.instance_config import get_value
    except Exception as e:
        logger.warning(
            "apply_bq_session_settings: instance_config unavailable (%s); "
            "extension default bq_query_timeout_ms (90 s) will apply",
            e,
        )
        return
    raw = get_value(
        "data_source", "bigquery", "query_timeout_ms", default=600_000,
    )
    try:
        ms = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        logger.warning(
            "apply_bq_session_settings: query_timeout_ms=%r is not an int; "
            "extension default (90 s) will apply",
            raw,
        )
        return
    if ms <= 0:
        # Operator opt-out: leave extension default in place. Log INFO so the
        # choice shows up in startup logs without being noisy.
        logger.info(
            "apply_bq_session_settings: query_timeout_ms=%d (≤0); extension "
            "default bq_query_timeout_ms (90 s) will apply",
            ms,
        )
        return
    try:
        conn.execute(f"SET bq_query_timeout_ms = {int(ms)}")
    except Exception as e:
        # Most common cause: the BigQuery extension is not loaded on this
        # connection yet (caller forgot the `LOAD bigquery` step), or the
        # installed extension version pre-dates the setting. Either way the
        # 90 s default sticks and remote queries time out unexpectedly.
        # Surface this — silent fallback was the bug behind real outages.
        logger.warning(
            "apply_bq_session_settings: SET bq_query_timeout_ms=%d failed (%s); "
            "extension default (90 s) will apply. Likely cause: BigQuery "
            "extension not loaded on this connection, or the installed "
            "extension version does not support this setting.",
            ms, e,
        )
        return
    # Verify the setting actually landed — protects against silent ignores
    # the extension might do in some failure modes.
    try:
        result = conn.execute(
            "SELECT current_setting('bq_query_timeout_ms')"
        ).fetchone()
        actual = int(result[0]) if result and result[0] is not None else None
    except Exception as e:
        logger.warning(
            "apply_bq_session_settings: could not read back "
            "bq_query_timeout_ms (%s); cannot verify setting was applied",
            e,
        )
        return
    if actual != ms:
        logger.warning(
            "apply_bq_session_settings: requested bq_query_timeout_ms=%d but "
            "current_setting reports %r — extension may have ignored the SET",
            ms, actual,
        )
    else:
        logger.debug(
            "apply_bq_session_settings: bq_query_timeout_ms=%d applied", ms,
        )


class BqAccess:
    """Single entry point for BigQuery access. Stateless after construction.

    Factories are injectable for tests:
        bq = BqAccess(
            BqProjects(billing="test-billing", data="test-data"),
            client_factory=lambda projects: mock_client,
        )
    """

    def __init__(
        self,
        projects: BqProjects,
        *,
        client_factory: Callable[[BqProjects], object] | None = None,
        duckdb_session_factory: Callable[[BqProjects], object] | None = None,
    ):
        self._projects = projects
        self._client_factory = client_factory or _default_client_factory
        self._duckdb_session_factory = duckdb_session_factory or _default_duckdb_session_factory

    @property
    def projects(self) -> BqProjects:
        return self._projects

    def client(self):
        """Construct (or retrieve from injected factory) a BigQuery client."""
        return self._client_factory(self._projects)

    @contextmanager
    def duckdb_session(self) -> Iterator[object]:
        """Yield in-memory DuckDB conn with bigquery extension loaded + SECRET set."""
        with self._duckdb_session_factory(self._projects) as conn:
            yield conn


def fetch_bq_columns_full(bq, dataset: str, table: str) -> list[dict] | None:
    """Single round-trip to INFORMATION_SCHEMA.COLUMNS pulling everything
    both v2_schema and the metadata provider need.

    Returns one dict per column with the keys ``name``, ``type``,
    ``nullable``, ``is_partitioning_column``, ``clustering_ordinal_position``.
    Consumers project the fields they care about.

    Best-effort: returns ``None`` on any failure (sentinel-unconfigured,
    unsafe identifier, BQ query exception). Does NOT raise. Mirrors the
    failure posture of `app/api/v2_schema.py:_fetch_bq_table_options`,
    which it replaces.

    Replaces two BQ jobs (one for column list + one for partition/cluster)
    with one — half the on-demand cost on each `/api/v2/schema/{id}`
    cache miss.
    """
    from src.identifier_validation import validate_quoted_identifier

    if not bq.projects.data:
        return None

    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        return None

    bq_sql = (
        f"SELECT column_name, data_type, is_nullable, "
        f"       is_partitioning_column, clustering_ordinal_position "
        f"FROM `{bq.projects.data}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
        f"WHERE table_name = ? ORDER BY ordinal_position"
    )
    try:
        with bq.duckdb_session() as conn:
            rows = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, table],
            ).fetchall()
    except Exception as e:
        logger.warning(
            "BQ COLUMNS fetch failed for %s.%s.%s: %s",
            bq.projects.data, dataset, table, e,
        )
        return None

    return [
        {
            "name": r[0],
            "type": r[1],
            "nullable": (r[2] or "").upper() == "YES",
            "is_partitioning_column": (r[3] or "").upper() == "YES",
            "clustering_ordinal_position": r[4],
        }
        for r in rows
    ]


@functools.cache
def get_bq_access() -> BqAccess:
    """Module-level FastAPI Depends target. Resolves projects from config and returns
    a BqAccess instance with default factories.

    Resolution order:
      1. BIGQUERY_PROJECT env var → both billing + data (legacy override)
      2. instance.yaml data_source.bigquery.billing_project → billing
      3. instance.yaml data_source.bigquery.project → data, and billing if (2) is unset

    Process-cached. Hot-reload of instance.yaml is out of scope; restart the container
    on config change. functools.cache does NOT cache exceptions, so a failed call is
    retried on the next invocation.

    Tests inject via `app.dependency_overrides[get_bq_access] = lambda: bq` for
    endpoints, or construct `BqAccess(...)` directly for non-endpoint code.

    Module-level (not a classmethod) to avoid the @classmethod + @functools.cache
    stacking footgun and to give FastAPI's dependency introspection a clean signature.
    """
    import os

    env_project = os.environ.get("BIGQUERY_PROJECT", "").strip()
    if env_project:
        return BqAccess(BqProjects(billing=env_project, data=env_project))

    from app.instance_config import get_value
    billing = (get_value("data_source", "bigquery", "billing_project", default="") or "").strip()
    data = (get_value("data_source", "bigquery", "project", default="") or "").strip()

    if not data:
        # Return a "not configured" sentinel BqAccess. Construction succeeds so FastAPI
        # Depends(get_bq_access) resolves cleanly on non-BQ instances (Keboola-only,
        # CSV-only) where every v2 endpoint would otherwise 500 during dep-injection
        # — even for local-source tables that never touch BigQuery.
        # The error is deferred to bq.client() / bq.duckdb_session() so the endpoint's
        # try/except BqAccessError catches it normally if (and only if) the endpoint
        # actually tries to query BQ. Devin BUG_0001 on PR #138 review.
        def _raise_not_configured(_projects):
            raise BqAccessError(
                "not_configured",
                "BigQuery project not configured",
                details={
                    "hint": (
                        "Set data_source.bigquery.project in instance.yaml "
                        "(and optionally data_source.bigquery.billing_project for "
                        "cross-project deployments). BIGQUERY_PROJECT env var also "
                        "accepted as legacy override."
                    ),
                },
            )

        @contextmanager
        def _raise_not_configured_session(_projects):
            _raise_not_configured(_projects)
            yield  # unreachable; keeps generator protocol

        return BqAccess(
            BqProjects(billing="", data=""),
            client_factory=_raise_not_configured,
            duckdb_session_factory=_raise_not_configured_session,
        )

    if not billing:
        billing = data

    return BqAccess(BqProjects(billing=billing, data=data))
