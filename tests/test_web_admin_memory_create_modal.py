"""/admin/corporate-memory — Create Memory Domain mini-modal (Task 8.10b).

Smoke-level: render the page as admin and assert that:
- The Create-Memory-Domain modal element is present.
- The JS handlers exist (open/close/submit + chip-create listener).
- The POST target is /api/admin/memory-domains.
- The follow-up RBAC step modal is present (per spec Section 7.4).

The parallel /admin/tables Create Data Package modal already shipped in
Task 8.10a; this test pins the symmetric Memory-Domain variant on the
admin corporate-memory page.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_memory_renders_create_domain_modal(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/corporate-memory", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # Modal markup is present with the documented id.
    assert 'id="createMemoryDomainModal"' in body
    # Input fields: name, slug, description, icon, color.
    for field_id in ("cmd-name", "cmd-slug", "cmd-desc", "cmd-icon", "cmd-color"):
        assert f'id="{field_id}"' in body, f"missing input id={field_id}"
    # JS handlers wired up.
    assert "openCreateMemoryDomainModal" in body
    assert "closeCreateMemoryDomainModal" in body
    assert "submitCreateMemoryDomain" in body
    # POSTs to the admin memory-domains endpoint.
    assert "/api/admin/memory-domains" in body
    # chip-create listener dispatches when the host carries
    # data-chip-input="memory_domain".
    assert "chip-create" in body
    assert "data-chip-input" not in body or "memory_domain" in body
    # Calls .addChip on the chip-input host so the freshly-created chip
    # lands back in the field that triggered the create.
    assert ".addChip(" in body


def test_admin_memory_renders_create_domain_rbac_step(seeded_app):
    """RBAC matrix now lives INLINE inside the Create modal as a
    collapsible section — the standalone step-2 modal was removed for
    the modal-on-modal UX complaint. This test asserts the inline
    plumbing still exists: groups fetch, grants POST with the right
    resource_type, the Available|Required enum, and the lazy-load
    hook into the <details> toggle."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/corporate-memory", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # Inline matrix container + lazy-load helper.
    assert 'id="cmd-rbac-details"' in body
    assert "_cmdHydrateRbacMatrix" in body
    assert "_submitCmdGrantsInline" in body
    # The removed step-2 modal should NOT be present.
    assert 'id="createMemoryDomainRbacModal"' not in body
    # Backend wiring unchanged: groups + grants endpoints + resource_type.
    assert "/api/admin/groups" in body
    assert "/api/admin/grants" in body
    assert "memory_domain" in body
    assert "available" in body
    assert "required" in body


def test_admin_tables_renders_create_data_package_rbac_step(seeded_app):
    """Mirror coverage for /admin/tables — same inline-matrix migration
    as the Memory Domain modal."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/tables", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # Inline matrix container + lazy-load helper.
    assert 'id="cdp-rbac-details"' in body
    assert "_cdpHydrateRbacMatrix" in body
    assert "_submitCdpGrantsInline" in body
    # Removed step-2 modal should NOT be present.
    assert 'id="createDataPackageRbacModal"' not in body
    # Backend wiring unchanged.
    assert "/api/admin/groups" in body
    assert "/api/admin/grants" in body
    assert "data_package" in body
