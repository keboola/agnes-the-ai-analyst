"""/admin/corporate-memory — Domains chip-input wiring (Task 8.9)."""


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_memory_renders_chip_input_for_domains(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/corporate-memory", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text
    # Chip-input mounted in the item edit modal.
    assert 'data-source-url="/api/memory/domains"' in body
    assert 'data-name="domain_ids"' in body
    assert 'data-chip-input="memory_domain"' in body
    assert "/static/js/components/chip-input.js" in body
