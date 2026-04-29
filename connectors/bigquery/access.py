"""Single entry point for BigQuery access — config resolution, client construction,
DuckDB-extension session, and Google-API error translation.

See docs/superpowers/specs/2026-04-29-issue-134-bq-access-unify-design.md for the
full design rationale.
"""
from __future__ import annotations

import functools
import logging
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
    }

    def __init__(self, kind: str, message: str, details: dict | None = None):
        self.kind = kind
        self.message = message
        self.details = details or {}
        super().__init__(message)


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
      4. BadRequest, bad_request_status='client_error'
                                          -> bq_bad_request (HTTP 400)
      5. BadRequest, bad_request_status='upstream_error'
                                          -> bq_upstream_error (HTTP 502)
      6. GoogleAPICallError (other)       -> bq_upstream_error
      7. Anything else                    -> RE-RAISED unchanged (don't swallow programmer errors)
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

    if isinstance(e, gax.BadRequest):
        if bad_request_status == "client_error":
            return BqAccessError("bq_bad_request", msg)
        return BqAccessError("bq_upstream_error", msg)

    if isinstance(e, gax.GoogleAPICallError):
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


@contextmanager
def _default_duckdb_session_factory(projects: BqProjects):
    """Yield an in-memory DuckDB conn with bigquery extension loaded + SECRET set
    from get_metadata_token(). Auto-cleanup. Translates auth/install failures
    to BqAccessError(kind='auth_failed' or 'bq_lib_missing').

    Note: `projects.billing` is not used by this factory directly — bigquery_query()
    callers pass it themselves as the first positional arg to identify the billing
    project. The factory keeps the parameter for symmetry with _default_client_factory.
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
        try:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            escaped = token.replace("'", "''")
            conn.execute(
                f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')"
            )
        except Exception as e:
            raise BqAccessError(
                "bq_lib_missing",
                f"failed to install/load BigQuery DuckDB extension: {e}",
                details={"original": str(e)},
            )
        yield conn
    finally:
        conn.close()


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
