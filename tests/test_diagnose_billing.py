"""Phase K — `da diagnose` warning when BQ billing_project == project.

Surfaces via /api/health/detailed (which `da diagnose` already consumes):
when data_source.type == 'bigquery' and the resolved BqProjects.billing equals
BqProjects.data, the response includes a `services.bq_config` entry with
status='warning' and a hint about the 403 USER_PROJECT_DENIED footgun.
"""

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _patch_instance_config(monkeypatch, cfg: dict) -> None:
    """Replace app.instance_config.load_instance_config + reset caches.

    Also clears connectors.bigquery.access.get_bq_access's @functools.cache
    so each test sees fresh BqProjects.
    """
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: cfg,
        raising=False,
    )
    # DATA_SOURCE env var, if set in the user shell, would override
    # get_data_source_type — strip it for deterministic tests.
    monkeypatch.delenv("DATA_SOURCE", raising=False)
    monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)

    from app.instance_config import reset_cache
    reset_cache()


@pytest.fixture(autouse=True)
def _reset_after(monkeypatch):
    yield
    # Always reset the cache after each test so the next test (or an
    # unrelated suite running afterwards) sees fresh config.
    try:
        from app.instance_config import reset_cache
        reset_cache()
    except Exception:
        pass


def test_diagnose_warns_when_billing_equals_project(seeded_app, monkeypatch):
    """BQ instance with billing_project missing (or equal to project) → warning."""
    _patch_instance_config(monkeypatch, {
        "data_source": {
            "type": "bigquery",
            "bigquery": {
                "project": "shared-data-prod",
                "billing_project": "shared-data-prod",
            },
        },
    })

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/health/detailed", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()

    bq_cfg = body.get("services", {}).get("bq_config")
    assert bq_cfg is not None, body
    assert bq_cfg.get("status") == "warning", bq_cfg
    # Hint mentions the YAML field path so operators know what to fix.
    blob = (str(bq_cfg.get("detail", "")) + " " + str(bq_cfg.get("hint", ""))).lower()
    assert "billing_project" in blob, bq_cfg


def test_diagnose_clean_when_billing_differs(seeded_app, monkeypatch):
    """Distinct billing_project → no warning surfaced."""
    _patch_instance_config(monkeypatch, {
        "data_source": {
            "type": "bigquery",
            "bigquery": {
                "project": "data-prod",
                "billing_project": "billing-dev",
            },
        },
    })

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/health/detailed", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()

    bq_cfg = body.get("services", {}).get("bq_config")
    # If present, it must be ok; absence is also fine (means no warning).
    if bq_cfg is not None:
        assert bq_cfg.get("status") == "ok", bq_cfg


def test_diagnose_no_warning_on_keboola_instance(seeded_app, monkeypatch):
    """Non-BQ instance: BQ billing check shouldn't surface at all."""
    _patch_instance_config(monkeypatch, {"data_source": {"type": "keboola"}})

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/health/detailed", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()

    # Either absent or explicitly status='ok' (n/a). Definitely not 'warning'.
    bq_cfg = body.get("services", {}).get("bq_config")
    if bq_cfg is not None:
        assert bq_cfg.get("status") != "warning", bq_cfg


def test_diagnose_returns_unknown_status_when_bq_resolution_fails(seeded_app, monkeypatch):
    """Devin finding 2026-05-01 (ANALYSIS_pr-review-job-642ff90f_0007):
    if get_bq_access() raises (missing google-cloud-bigquery, auth error,
    malformed config), the bq_config check must NOT report status='ok' —
    automated alerting keyed on `status != 'ok'` would silently miss the
    failure. Use 'unknown' so dashboards surface it without promoting the
    overall check to 'degraded' (which 'warning' would do)."""
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "p"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()

    # Force get_bq_access to raise.
    def _raise(*a, **kw):
        raise RuntimeError("simulated bq lib missing")

    import connectors.bigquery.access as bq_access_mod
    monkeypatch.setattr(bq_access_mod, "get_bq_access", _raise)

    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get(
            "/api/health/detailed",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        bq_check = body.get("services", {}).get("bq_config")
        assert bq_check is not None, body
        # Must NOT be 'ok' — that would mask the failure from alerting.
        assert bq_check.get("status") == "unknown", bq_check
        assert "could not resolve" in bq_check.get("detail", "").lower()
    finally:
        reset_cache()
