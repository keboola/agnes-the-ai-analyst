"""POST /api/query gates direct `bq."<dataset>"."<source_table>"` references.

Pre-existing RBAC hole: the forbidden-table check at `app/api/query.py`
only blocks names matching an existing master view. Direct `bq.*` syntax
doesn't match any master view → bypasses the check entirely. Closed in
#160 via the BQ_PATH regex + find_by_bq_path lookup: every `bq.*` in user
SQL must point at a registered query_mode='remote' BigQuery row.

Tests cover: unregistered path → 403; registered + caller has grant →
allowed (request reaches BQ — we only check that RBAC didn't block it
before BQ runs); admin → bypasses per-name grant but still requires
registration.
"""
from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_bq_remote_row(name: str, bucket: str, source_table: str) -> None:
    """Insert a query_mode='remote' BQ row directly via the system DB."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    sys_conn = get_system_db()
    try:
        TableRegistryRepository(sys_conn).register(
            id=f"bq.{bucket}.{source_table}",
            name=name,
            source_type="bigquery",
            bucket=bucket,
            source_table=source_table,
            query_mode="remote",
        )
    finally:
        sys_conn.close()


def test_quoted_bq_catalog_token_rejected_403(seeded_app):
    """Phase 3 review evasion: `SELECT * FROM "bq"."ds"."tbl"` (catalog
    token quoted) must be caught by the same RBAC check as the unquoted
    form. DuckDB resolves `"bq"` to the same ATTACHed BQ catalog, so the
    quoted variant is a real bypass we have to close."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM "bq"."secret_ds"."secret_tbl"'},
        headers=_auth(token),
    )
    assert r.status_code == 403, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("reason") == "bq_path_not_registered", detail


def test_unregistered_bq_path_rejected_403(seeded_app):
    """Direct reference to a `bq.<ds>.<tbl>` that no registry row points at:
    403 with `bq_path_not_registered`. Caller has admin token (no per-name
    RBAC); the registration check still fires because admin must register
    first to query a new dataset."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM bq."secret_ds"."secret_tbl"'},
        headers=_auth(token),
    )
    assert r.status_code == 403, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("reason") == "bq_path_not_registered", detail
        assert "secret_ds" in detail.get("path", "")
        assert "secret_tbl" in detail.get("path", "")
    else:
        # Fallback: detail must at least mention the unregistered path.
        assert "bq_path_not_registered" in str(detail) or "secret_ds" in str(detail)


def test_registered_bq_path_admin_passes_rbac(seeded_app):
    """When the path IS registered, an admin caller sails past the RBAC
    check. The query may still fail downstream (e.g. cost guardrail or
    actual BQ execution) — but NOT with `bq_path_not_registered` or
    `bq_path_access_denied`."""
    _register_bq_remote_row("ue", "finance", "ue")
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM bq."finance"."ue"'},
        headers=_auth(token),
    )
    # Either allowed (200/400/etc on downstream paths) but NOT 403 from RBAC.
    if r.status_code == 403:
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("reason") not in (
                "bq_path_not_registered",
                "bq_path_access_denied",
            ), f"admin with registered path should not be RBAC-rejected: {detail}"


def test_unregistered_bq_path_case_insensitive_match(seeded_app):
    """`bq."Finance"."UE"` resolves to the registered `(finance, ue)` row
    via case-insensitive lookup — admin sails through, no 403."""
    _register_bq_remote_row("ue", "finance", "ue")
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM bq."Finance"."UE"'},
        headers=_auth(token),
    )
    if r.status_code == 403:
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("reason") not in (
                "bq_path_not_registered",
                "bq_path_access_denied",
            ), f"case-insensitive lookup should match: {detail}"


def test_string_literal_matching_bq_path_rejected_403(seeded_app):
    """Documented false-positive: `WHERE c = 'bq.unreg.tbl'` regex-matches
    the literal. Strict-deny: 403 if the path isn't registered. Operator
    can rephrase or register the path."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue WHERE c = 'bq.unreg.tbl'"},
        headers=_auth(token),
    )
    # The literal contains a `bq.<unreg>.<tbl>` pattern that the regex
    # matches; the RBAC patch must reject 403 (strict-deny on a security
    # boundary) — pre-existing master-view check would only return 400 if
    # `ue` weren't registered. Test: explicitly verify bq_path_not_registered.
    assert r.status_code == 403, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("reason") == "bq_path_not_registered", detail
