"""Table access policies — the catalog profile leak (design doc §11's
"sharper leak"; plan Task 13).

``GET /api/catalog/profile/{id}`` and ``POST /api/catalog/profile/{id}/refresh``
served the STORED profile — ``min``/``max``/``sample_values``/``top_values``,
row CONTENT, not merely a schema — completely unfiltered by any policy
attached to the table: a caller who merely passed the table-level
``can_access_table`` check saw every column's statistics, including a
column an ``EXCLUDE``'d policy already hides from every other read surface
(``/api/v2/schema``, the catalog table-detail page, ...). Both endpoints now
route through ``app.api.catalog._profile_restriction``, which withholds the
stats for a non-admin caller on a policied table and leaves the response
byte-identical otherwise.
"""

from __future__ import annotations

import json

from tests.test_access_policy_effective_schema import policied_invoices  # noqa: F401


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_profile(table_id: str, columns: list[dict]) -> None:
    from src.db import get_system_db
    from src.repositories.profiles import ProfileRepository

    conn = get_system_db()
    try:
        ProfileRepository(conn).save(table_id, {"columns": columns, "row_count": 999})
    finally:
        conn.close()


class TestGetTableProfile:
    def test_non_admin_on_a_policied_table_gets_no_stats(self, policied_invoices):  # noqa: F811
        _seed_profile(
            "invoices",
            [
                {"name": "national_id", "type": "VARCHAR", "sample_values": ["N-1", "N-2"]},
                {"name": "cost_center", "type": "VARCHAR", "sample_values": ["Finance"]},
            ],
        )
        c = policied_invoices["client"]
        r = c.get("/api/catalog/profile/invoices", headers=_auth(policied_invoices["finance_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("policy_restricted") is True
        assert "columns" not in body
        assert "N-1" not in r.text
        assert "national_id" not in r.text

    def test_admin_on_a_policied_table_still_sees_the_stats(self, policied_invoices):  # noqa: F811
        """Admin/no-policy unchanged (§12)."""
        _seed_profile(
            "invoices",
            [{"name": "national_id", "type": "VARCHAR", "sample_values": ["N-1", "N-2"]}],
        )
        c = policied_invoices["client"]
        r = c.get("/api/catalog/profile/invoices", headers=_auth(policied_invoices["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("policy_restricted") is not True
        assert body["columns"][0]["name"] == "national_id"
        assert body["columns"][0]["sample_values"] == ["N-1", "N-2"]

    def test_non_policied_sibling_table_is_unaffected(self, policied_invoices):  # noqa: F811
        _seed_profile("products", [{"name": "sku", "type": "VARCHAR", "sample_values": ["A1"]}])
        c = policied_invoices["client"]
        r = c.get("/api/catalog/profile/products", headers=_auth(policied_invoices["finance_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("policy_restricted") is not True
        assert body["columns"][0]["sample_values"] == ["A1"]

    def test_unregistered_table_profiles_json_fallback_is_unaffected(self, policied_invoices):  # noqa: F811
        """A name that only exists in the legacy ``profiles.json`` fallback
        (never in ``table_registry`` at all) can never carry a policy —
        ``_profile_restriction`` must resolve that to "unrestricted", not
        "unresolvable, fail closed". Admin-only reachable in practice: a
        non-admin's ``can_access_table`` check denies BEFORE this endpoint
        ever consults ``profiles.json`` for a name with no registry row
        (no package can contain a table id that doesn't exist), so admin is
        the only caller who can actually reach this branch — and is exactly
        who a naive "unresolvable → restrict" design would have wrongly
        blocked from their own legacy profile data.
        """
        data_dir = policied_invoices["env"]["data_dir"]
        meta_dir = data_dir / "src_data" / "metadata"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "profiles.json").write_text(
            json.dumps({"tables": {"legacy_only": {"columns": [{"name": "x", "sample_values": ["1"]}]}}})
        )
        c = policied_invoices["client"]
        r = c.get("/api/catalog/profile/legacy_only", headers=_auth(policied_invoices["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("policy_restricted") is not True
        assert body["columns"][0]["sample_values"] == ["1"]


class TestRefreshProfile:
    def test_non_admin_refresh_response_has_no_stats(self, policied_invoices, tmp_path, monkeypatch):  # noqa: F811
        """The recompute+store still runs (matches the scheduled sync's own
        unfiltered write) — only the RESPONSE to this caller is gated."""
        from app.utils import resolve_local_parquet

        c = policied_invoices["client"]
        parquet = resolve_local_parquet("invoices")
        assert parquet is not None, "fixture must have synced a local parquet for 'invoices'"

        r = c.post(
            "/api/catalog/profile/invoices/refresh",
            headers=_auth(policied_invoices["finance_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body.get("policy_restricted") is True
        assert "columns" not in body

        # The store itself was NOT skipped — an admin reading the profile
        # right after sees a freshly-computed, unrestricted result.
        admin_r = c.get("/api/catalog/profile/invoices", headers=_auth(policied_invoices["admin_token"]))
        assert admin_r.status_code == 200, admin_r.text
        assert admin_r.json().get("policy_restricted") is not True

    def test_admin_refresh_response_is_unchanged(self, policied_invoices):  # noqa: F811
        c = policied_invoices["client"]
        r = c.post(
            "/api/catalog/profile/invoices/refresh",
            headers=_auth(policied_invoices["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert "columns" in body  # the pre-existing shape: a column COUNT, unrestricted

    def test_non_policied_sibling_refresh_is_unaffected(self, policied_invoices):  # noqa: F811
        c = policied_invoices["client"]
        r = c.post(
            "/api/catalog/profile/products/refresh",
            headers=_auth(policied_invoices["finance_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert "columns" in body
