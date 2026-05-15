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
    """RBAC step 2 modal (spec 7.4): per-group requirement matrix."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/corporate-memory", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # Step 2 modal element + Skip / Save buttons wired up.
    assert 'id="createMemoryDomainRbacModal"' in body
    assert "skipCreateMemoryDomainRbac" in body
    assert "submitCreateMemoryDomainRbac" in body
    # Loads groups from /api/admin/groups and POSTs grants to /api/admin/grants.
    assert "/api/admin/groups" in body
    assert "/api/admin/grants" in body
    # Resource-type constant in payload.
    assert "memory_domain" in body
    # Available | Required tiers — the requirement enum from the v49 spec.
    assert "available" in body
    assert "required" in body


def test_admin_tables_renders_create_data_package_rbac_step(seeded_app):
    """Mirror coverage: /admin/tables Create-Data-Package RBAC step 2."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/tables", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # Step 2 modal element + Skip / Save buttons wired up.
    assert 'id="createDataPackageRbacModal"' in body
    assert "skipCreateDataPackageRbac" in body
    assert "submitCreateDataPackageRbac" in body
    # Loads groups + POSTs grants with resource_type=data_package.
    assert "/api/admin/groups" in body
    assert "/api/admin/grants" in body
    assert "data_package" in body
