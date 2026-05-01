"""End-to-end: register a Keboola materialized row -> trigger sync ->
parquet appears -> manifest serves it -> CLI da sync would download it.

Skipped unless KBC_TEST_URL + KBC_TEST_TOKEN + KBC_TEST_BUCKET +
KBC_TEST_TABLE are present.
"""
import os
from pathlib import Path

import pytest


KBC_URL = os.environ.get("KBC_TEST_URL")
KBC_TOKEN = os.environ.get("KBC_TEST_TOKEN")
KBC_BUCKET = os.environ.get("KBC_TEST_BUCKET")
KBC_TABLE = os.environ.get("KBC_TEST_TABLE")

pytestmark = pytest.mark.skipif(
    not all([KBC_URL, KBC_TOKEN, KBC_BUCKET, KBC_TABLE]),
    reason="Keboola creds not provided",
)


def test_register_trigger_manifest_path(seeded_app, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KEBOOLA_TOKEN", KBC_TOKEN)
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: {
            "data_source": {
                "type": "keboola",
                "keboola": {
                    "url": KBC_URL,
                    "token_env": "KEBOOLA_TOKEN",
                },
            },
        },
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register.
    r = c.post("/api/admin/register-table", headers=auth, json={
        "name": "smoke_subset",
        "source_type": "keboola",
        "query_mode": "materialized",
        "source_query": (
            f'SELECT * FROM kbc."{KBC_BUCKET}"."{KBC_TABLE}" LIMIT 5'
        ),
    })
    assert r.status_code == 201

    # Trigger sync.
    r = c.post("/api/sync/trigger", headers=auth)
    assert r.status_code in (200, 202)

    # Parquet must exist.
    parquet = Path(tmp_path) / "extracts" / "keboola" / "data" / "smoke_subset.parquet"
    assert parquet.exists() and parquet.stat().st_size > 0

    # Manifest serves it.
    r = c.get("/api/sync/manifest", headers=auth)
    rows = r.json()["tables"]
    smoke = next((t for t in rows if t["id"] == "smoke_subset"), None)
    assert smoke is not None
    assert smoke["source_type"] == "keboola"
    assert smoke["query_mode"] == "local"  # materialized parquets surface as local
    assert smoke["md5"]  # has a hash for da sync delta detection
