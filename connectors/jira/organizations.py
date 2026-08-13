"""Jira organizations dimension table (issue #1273).

`issues.organization_ids` (captured in ``transform.py``) gives a ticket a stable
key into an organization. This module resolves the OTHER end of that join: a
current-state ``organizations`` table (``org_id``, ``name``, plus one column per
operator-configured detail field), refreshed on a low-frequency cadence that
piggybacks on the existing ``jira-refresh`` job rather than a new scheduler.

Two Jira Cloud APIs are involved, and their split is awkward enough to restate
here rather than let it get re-derived:

- **Enumeration** — the classic JSM Cloud API,
  ``GET /rest/servicedeskapi/organization`` (paginated via ``start``/``limit``,
  terminated by ``isLastPage``). There is no Customer Service Management (CSM)
  list endpoint: ``GET /organization`` answers 405, and ``POST`` there CREATES
  an organization — never call it from here.
- **Detail fields** — the CSM API, ``GET /organization/{organizationId}``
  (``https://api.atlassian.com/jsm/csm/cloudid/{cloudId}/api/v1/organization/{id}``).
  This is the ONLY endpoint that returns a detail field's ``id`` — the thing that
  survives a rename of the detail field itself. The batched alternative,
  ``POST /organization/profile/fetch``, returns names only and would reintroduce
  the exact name-drift problem this issue is about. The per-organization
  response's ``id`` is an observed property, not documented on the API reference
  pages, so matching falls back to ``name`` when ``id`` does not resolve
  (:func:`resolve_detail_values`).

Both APIs accept the same HTTP Basic (email + API token) credentials the rest of
this connector already uses, despite the CSM reference specifying OAuth 2.0
Bearer.

Failure handling is deliberately asymmetric between the two APIs:

- A failed *enumeration* aborts the WHOLE refresh (:class:`JiraFetchError`) and
  leaves the existing table untouched — a partial organization list is
  indistinguishable from organizations having been deleted.
- A failed *detail* fetch for a single organization is fail-soft
  (:func:`fetch_organization_detail` returns ``None``, never raises): that
  organization's previous detail values are carried forward
  (:func:`build_organization_rows`) rather than blanked, so a transient 429 does
  not destroy a value a previous run resolved correctly.
"""

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pyarrow.parquet as pq

from connectors.jira.service import Config, JiraFetchError, _parse_field_pairs
from src.duckdb_conn import _open_duckdb
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

TABLE_NAME = "organizations"

ORGANIZATIONS_BASE_SCHEMA: dict[str, str] = {
    "org_id": "string",
    "name": "string",
}

# Prefix used when an operator-configured detail column would collide with a
# built-in `ORGANIZATIONS_BASE_SCHEMA` column — mirrors
# `connectors.jira.transform.REFRESH_COLLISION_PREFIX`, kept as an independent
# constant because this is a different table with a different (much smaller)
# set of built-ins.
DETAIL_COLLISION_PREFIX = "detail_"

DEFAULT_ORG_REFRESH_INTERVAL_DAYS = 7


def org_detail_fields() -> list[tuple[str, str]]:
    """``[(token, column_name), ...]`` parsed from ``JIRA_ORG_DETAIL_FIELDS``.

    Same format as ``JIRA_REFRESH_FIELDS``
    (:func:`connectors.jira.service.refresh_fields`): comma-separated ``token`` or
    ``token:column_name``. No defaults — detail-field ids are per-instance. Each
    ``token`` is matched against a fetched detail field's ``id`` first, falling
    back to its ``name`` — see :func:`resolve_detail_values`.
    """
    return _parse_field_pairs(os.environ.get("JIRA_ORG_DETAIL_FIELDS", ""))


def resolved_org_detail_columns() -> list[tuple[str, str]]:
    """``org_detail_fields()``, collision-safe against ``ORGANIZATIONS_BASE_SCHEMA``.

    A column that would collide with a built-in (``org_id``/``name``) is
    prefixed with ``DETAIL_COLLISION_PREFIX`` instead of overwriting the
    built-in; a column already claimed by an earlier entry is skipped. Mirrors
    ``connectors.jira.transform.resolved_refresh_columns`` for the issues table.
    """
    reserved = set(ORGANIZATIONS_BASE_SCHEMA)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for token, column in org_detail_fields():
        if column in reserved:
            safe = f"{DETAIL_COLLISION_PREFIX}{column}"
            logger.warning(
                "Jira org detail field %s: column %r collides with a built-in organizations column; using %r instead",
                token,
                column,
                safe,
            )
            column = safe
        if column in seen:
            logger.warning(
                "Jira org detail field %s: column %r already used by another detail field; skipping",
                token,
                column,
            )
            continue
        seen.add(column)
        out.append((token, column))
    return out


def organizations_schema() -> dict[str, str]:
    """``ORGANIZATIONS_BASE_SCHEMA`` extended with one string column per
    configured detail field — resolved at call time so the table always
    reflects the current ``JIRA_ORG_DETAIL_FIELDS``."""
    schema = dict(ORGANIZATIONS_BASE_SCHEMA)
    for _, column in resolved_org_detail_columns():
        schema.setdefault(column, "string")
    return schema


def resolve_detail_values(details: list[dict[str, Any]], configured: list[tuple[str, str]]) -> dict[str, str | None]:
    """Map each configured ``(token, column)`` to a detail-field value.

    ``details`` is the ``details`` array from the CSM ``GET /organization/{id}``
    response — each entry has ``id``, ``name``, and ``values`` (a list of
    strings, possibly empty even for a resolved field). Matching is id-first:
    a ``token`` is looked up against every field's ``id``, and only falls back
    to matching on ``name`` when no field's ``id`` equals it (the ``id`` is an
    observed, undocumented property of this response — see the module
    docstring). The first value is used when there is one; a field with no
    values (or no match at all) resolves to ``None`` rather than raising —
    fail soft per organization, not "drop the row".
    """
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for field in details or []:
        field_id = field.get("id")
        field_name = field.get("name")
        if field_id is not None and field_id not in by_id:
            by_id[field_id] = field
        if field_name is not None and field_name not in by_name:
            by_name[field_name] = field

    out: dict[str, str | None] = {}
    for token, column in configured:
        field = by_id.get(token) or by_name.get(token)
        values = field.get("values") if field else None
        out[column] = values[0] if values else None
    return out


def fetch_organizations(domain: str, auth: tuple[str, str], *, page_size: int = 50) -> list[dict[str, Any]]:
    """Enumerate every JSM organization via the classic
    ``GET /rest/servicedeskapi/organization`` (paginated).

    Returns ``[{"id": ..., "name": ...}, ...]``. There is no CSM list endpoint
    (module docstring), so this classic JSM Cloud endpoint is the only
    enumeration path.

    Raises :class:`JiraFetchError` on any non-200 response, any request error,
    or a page that comes back empty while claiming more pages remain (a
    malformed/inconsistent response — aborting avoids looping forever on an
    unchanged ``start``). A partial enumeration is indistinguishable from
    organizations having been deleted, so callers MUST treat a raised error as
    "skip this refresh entirely, keep the existing table" — never as "these are
    all the organizations that still exist".
    """
    base_url = f"https://{domain}/rest/servicedeskapi"
    organizations: list[dict[str, Any]] = []
    start = 0

    while True:
        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    f"{base_url}/organization",
                    auth=auth,
                    params={"start": start, "limit": page_size},
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError as e:
            raise JiraFetchError(f"Organization enumeration failed at start={start}: connection — {e}") from e

        if response.status_code != 200:
            raise JiraFetchError(f"Organization enumeration failed at start={start}: HTTP {response.status_code}")

        body = response.json()
        values = body.get("values", []) or []
        for value in values:
            org_id = value.get("id")
            if org_id is None:
                continue
            organizations.append({"id": str(org_id), "name": value.get("name")})

        if body.get("isLastPage", True):
            break
        if not values:
            raise JiraFetchError(f"Organization enumeration stalled at start={start}: empty page, isLastPage=False")
        start += len(values)

    return organizations


def fetch_organization_detail(cloud_id: str, org_id: str, auth: tuple[str, str]) -> dict[str, Any] | None:
    """Fetch one organization's detail fields via the CSM
    ``GET /organization/{organizationId}`` endpoint.

    Returns the parsed ``{"id", "name", "details": [...]}`` body on success.
    Returns ``None`` — NEVER raises — on any failure (401/403/404/429/5xx or a
    connection error): per-organization detail lookups are fail-soft, so a
    transient rate limit on one organization must not blank a value a previous
    run resolved correctly. Callers merge a ``None`` result with that
    organization's previous row instead of overwriting it with nulls.
    """
    url = f"https://api.atlassian.com/jsm/csm/cloudid/{cloud_id}/api/v1/organization/{org_id}"
    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(url, auth=auth, headers={"Accept": "application/json"})
    except httpx.RequestError as e:
        logger.warning("Organization detail fetch failed for %s: connection — %s", org_id, e)
        return None

    if response.status_code == 200:
        return response.json()

    logger.warning("Organization detail fetch failed for %s: HTTP %s", org_id, response.status_code)
    return None


def build_organization_rows(
    orgs: list[dict[str, Any]],
    detail_by_org: dict[str, dict[str, Any] | None],
    previous_by_org: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per organization: ``org_id``, ``name``, and the configured detail
    columns.

    ``detail_by_org[org_id]`` is the parsed CSM detail response for that
    organization, or ``None`` when the fetch failed/was skipped
    (:func:`fetch_organization_detail`). On ``None``, the row falls back to
    ``previous_by_org[org_id]``'s values for the same columns rather than
    nulling them out — fail soft per organization (issue #1273). An
    organization with no detail ever resolved (fresh, never in
    ``previous_by_org``) still gets a row, with ``None`` detail columns — the
    row is never dropped.
    """
    configured = resolved_org_detail_columns()
    rows: list[dict[str, Any]] = []
    for org in orgs:
        org_id = org["id"]
        row: dict[str, Any] = {"org_id": org_id, "name": org.get("name")}
        detail = detail_by_org.get(org_id)
        if detail is not None:
            row.update(resolve_detail_values(detail.get("details", []), configured))
        else:
            previous = previous_by_org.get(org_id, {})
            for _, column in configured:
                row[column] = previous.get(column)
        rows.append(row)
    return rows


def _read_existing_organizations(output_dir: Path) -> dict[str, dict[str, Any]]:
    """Previous ``organizations`` rows keyed by ``org_id``, or ``{}`` if there is
    no previous parquet (first sync) or it can't be read."""
    pq_path = output_dir / "data" / f"{TABLE_NAME}.parquet"
    if not pq_path.exists():
        return {}
    try:
        df = pd.read_parquet(pq_path)
    except Exception as e:
        logger.warning("Could not read previous organizations parquet, treating as empty: %s", e)
        return {}
    return {str(row["org_id"]): row.to_dict() for _, row in df.iterrows()}


def _write_organizations_table(output_dir: Path | str, rows: list[dict[str, Any]]) -> None:
    """Write ``rows`` as the ``organizations`` table: a single (non
    month-partitioned — an organization has one current state, not a history)
    parquet file, plus the matching ``_meta`` row and view in
    ``extract.duckdb``. Atomic (tmp file + ``os.replace``)."""
    from connectors.jira.extract_init import init_extract
    from connectors.jira.transform import PARQUET_WRITE_OPTIONS, apply_schema

    output_path = Path(output_dir)
    data_dir = output_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pq_path = data_dir / f"{TABLE_NAME}.parquet"

    schema = organizations_schema()
    df = pd.DataFrame(rows, columns=list(schema)) if rows else pd.DataFrame(columns=list(schema))
    table = apply_schema(df, schema)

    fd, tmp_path = tempfile.mkstemp(dir=str(data_dir), suffix=".tmp")
    os.close(fd)
    try:
        pq.write_table(table, tmp_path, **PARQUET_WRITE_OPTIONS)
        os.replace(tmp_path, pq_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    db_path = output_path / "extract.duckdb"
    if not db_path.exists():
        init_extract(output_path)

    conn = _open_duckdb(str(db_path))
    try:
        safe_path = str(pq_path).replace("'", "''")
        conn.execute(f"CREATE OR REPLACE VIEW {quote_ident(TABLE_NAME)} AS SELECT * FROM read_parquet('{safe_path}')")
        rows_count = conn.execute(f"SELECT count(*) FROM {quote_ident(TABLE_NAME)}").fetchone()[0]
        size_bytes = pq_path.stat().st_size
        now = datetime.now(timezone.utc)
        existing = conn.execute("SELECT count(*) FROM _meta WHERE table_name = ?", [TABLE_NAME]).fetchone()[0]
        if existing:
            conn.execute(
                "UPDATE _meta SET rows = ?, size_bytes = ?, extracted_at = ? WHERE table_name = ?",
                [rows_count, size_bytes, now, TABLE_NAME],
            )
        else:
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                [TABLE_NAME, "Jira organizations (current state)", rows_count, size_bytes, now],
            )
        conn.execute("CHECKPOINT")
    finally:
        conn.close()


def sync_organizations(output_dir: Path | str) -> dict[str, Any]:
    """Fetch the current organizations + configured detail fields and (re)write
    the ``organizations`` table into ``<output_dir>/extract.duckdb``.

    Unconditional — see :func:`refresh_organizations_if_stale` for the
    cadence-gated entry point actually wired into the connector's refresh job
    (``app.worker.kinds._run_jira_refresh``).

    Fail-soft contract (issue #1273, restated from the module docstring):
      - Enumeration failure -> the WHOLE refresh is skipped, existing table
        untouched.
      - A single organization's detail-fetch failure -> that organization
        keeps its previous detail values instead of being blanked.
    """
    output_path = Path(output_dir)
    domain = Config.JIRA_DOMAIN
    email = Config.JIRA_EMAIL
    token = Config.JIRA_API_TOKEN
    cloud_id = Config.JIRA_CLOUD_ID

    if not (domain and email and token):
        logger.warning("Jira organizations sync skipped: Jira service not configured")
        return {"skipped": "not_configured"}

    auth = (email, token)
    try:
        orgs = fetch_organizations(domain, auth)
    except JiraFetchError as e:
        logger.warning("Jira organizations sync skipped: %s", e)
        return {"skipped": "enumeration_failed"}

    configured = resolved_org_detail_columns()
    previous_by_org = _read_existing_organizations(output_path)

    detail_by_org: dict[str, dict[str, Any] | None] = {}
    if configured:
        if not cloud_id:
            logger.warning(
                "JIRA_ORG_DETAIL_FIELDS is configured but JIRA_CLOUD_ID is not set — "
                "organization detail fields require the CSM API's cloud-scoped gateway; "
                "skipping detail fetch this run (org_id/name only)."
            )
        else:
            for org in orgs:
                detail_by_org[org["id"]] = fetch_organization_detail(cloud_id, org["id"], auth)

    rows = build_organization_rows(orgs, detail_by_org, previous_by_org)
    _write_organizations_table(output_path, rows)

    return {
        "organizations": len(rows),
        "detail_fetch_failed": sum(1 for v in detail_by_org.values() if v is None),
    }


def org_refresh_interval_days() -> int:
    """Days between ``organizations`` table refreshes (``JIRA_ORG_REFRESH_INTERVAL_DAYS``,
    default :data:`DEFAULT_ORG_REFRESH_INTERVAL_DAYS`). Organization membership and
    detail values change on a scale of weeks (issue #1273), not per webhook event —
    this is what keeps :func:`refresh_organizations_if_stale` from calling the Jira
    Organization API on every ``jira-refresh`` run."""
    raw = os.environ.get("JIRA_ORG_REFRESH_INTERVAL_DAYS", "").strip()
    if not raw:
        return DEFAULT_ORG_REFRESH_INTERVAL_DAYS
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning(
            "Invalid JIRA_ORG_REFRESH_INTERVAL_DAYS=%r, using default %d days",
            raw,
            DEFAULT_ORG_REFRESH_INTERVAL_DAYS,
        )
        return DEFAULT_ORG_REFRESH_INTERVAL_DAYS


def organizations_stale(output_dir: Path | str, *, now: datetime | None = None) -> bool:
    """Has the ``organizations`` table gone stale (does it need a refresh)?

    True when ``extract.duckdb`` doesn't exist yet, the ``organizations`` row is
    missing from ``_meta`` (never synced), or its ``extracted_at`` is older than
    ``org_refresh_interval_days()``. This is the gate
    :func:`refresh_organizations_if_stale` checks so the (network-heavy: one HTTP
    call per organization) Jira Organization API sync piggybacks on the existing
    ``jira-refresh`` job — fired per webhook burst, per SLA poll cycle, and per
    consistency check (see ``app.worker.kinds._run_jira_refresh`` and
    ``tests/test_jira_meta_refresh_cadence.py``) — without running on every one of
    those. Any error reading ``_meta`` is treated as stale (safer default: an
    extra sync costs an API call, a skipped one costs correctness).
    """
    output_path = Path(output_dir)
    db_path = output_path / "extract.duckdb"
    if not db_path.exists():
        return True

    try:
        conn = _open_duckdb(str(db_path))
        try:
            row = conn.execute("SELECT extracted_at FROM _meta WHERE table_name = ?", [TABLE_NAME]).fetchone()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Could not read organizations _meta staleness, treating as stale: %s", e)
        return True

    if row is None or row[0] is None:
        return True

    extracted_at = row[0]
    if extracted_at.tzinfo is None:
        extracted_at = extracted_at.replace(tzinfo=timezone.utc)
    effective_now = now or datetime.now(timezone.utc)
    return (effective_now - extracted_at) >= timedelta(days=org_refresh_interval_days())


def refresh_organizations_if_stale(output_dir: Path | str) -> dict[str, Any] | None:
    """Cadence-gated entry point wired into ``app.worker.kinds._run_jira_refresh``.

    Runs :func:`sync_organizations` only when :func:`organizations_stale` — does
    no network I/O and returns ``None`` otherwise. This is what bounds the
    (network-heavy) Jira Organization API sync to ``org_refresh_interval_days()``
    even though the ``jira-refresh`` job it rides runs far more often — piggy-
    backing on the existing cadence mechanism rather than inventing a new
    scheduler (issue #1273).
    """
    output_path = Path(output_dir)
    if not organizations_stale(output_path):
        logger.debug("Jira organizations table is fresh, skipping refresh")
        return None
    return sync_organizations(output_path)
