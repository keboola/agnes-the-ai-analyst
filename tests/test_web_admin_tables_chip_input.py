"""/admin/tables — Data Packages chip-input wiring (Task 8.8).

Smoke-level: assert the chip-input host element appears inside the
BigQuery Register modal and points at the right source endpoint. Full
chip-input + form-submit wiring needs Playwright; documented as a
follow-up.
"""


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_tables_renders_chip_input_for_data_packages(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/tables", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text
    # Chip-input mount + source endpoint inside the BQ Register modal.
    assert 'data-source-url="/api/admin/data-packages"' in body
    assert 'data-allow-create="true"' in body
    assert 'data-name="bq_package_ids"' in body
    # chip-input.js loaded globally via base.html.
    assert "/static/js/components/chip-input.js" in body
