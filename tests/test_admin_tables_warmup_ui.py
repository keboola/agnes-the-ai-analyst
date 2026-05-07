"""Smoke test that /admin/tables HTML contains the cache toolbar markup,
the EventSource wiring, and the per-row col-status slot."""


def test_cache_toolbar_present(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/admin/tables", headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert 'id="cacheWarmupCard"' in body
    assert "Re-warm all" in body
    assert "/api/admin/cache-warmup/stream" in body
    assert "EventSource" in body


def test_query_mode_doc_link_present(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/admin/tables", headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert "query-modes" in r.text  # link to docs/admin/query-modes.md or rendered URL


def test_col_status_th_present_in_renderer(seeded_app):
    """The renderRegistryListing JS still emits <th class='col-status'>
    so the per-row badge slot exists."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers={"Authorization": f"Bearer {token}"})
    assert 'col-status' in r.text
