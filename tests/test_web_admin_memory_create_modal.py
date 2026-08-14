"""/admin/corporate-memory — Create Memory Domain mini-modal (Task 8.10b).

Smoke-level: render the page as admin and assert that:
- The Create-Memory-Domain modal element is present.
- The JS handlers exist (open/close/submit + chip-create listener).
- The POST target is /api/admin/memory-domains.
- The follow-up RBAC step modal is present (per spec Section 7.4).

The parallel Data Package flow shipped first (Task 8.10a) and has since left
this page's shape behind: it is the shared right-side drawer
(`js/components/package_drawer.js`), opened by both Data lenses, and the one
test here that covered it reads the component instead of `/admin/tables`. The
Memory-Domain variant is still the modal described above.
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


def test_create_data_package_keeps_its_inline_group_access_step():
    """The Data Package side of the same migration, at its new address.

    This used to read `/admin/tables` for `#cdp-rbac-details` and the two
    `_cdp*` helpers, because the create form was that page's markup. The form
    is now the shared drawer component — a package is created from the
    Packages lens as well, and neither lens should own the only copy — so the
    capability this test exists for (grant per-group access WITHOUT a second
    modal, groups fetched lazily) is pinned there instead.
    """
    from pathlib import Path

    src = Path("app/web/static/js/components/package_drawer.js").read_text(encoding="utf-8")

    # The inline section, still collapsed-and-lazy: one request, and only if
    # the admin opens it.
    assert 'id="pdw-access"' in src
    assert "function hydrateGroups()" in src
    assert "st.groupsLoaded" in src
    # …and no modal-on-modal came back.
    assert "createDataPackageRbacModal" not in src
    # Backend wiring unchanged.
    assert "/api/admin/groups" in src
    assert "/api/admin/grants" in src
    assert "'data_package'" in src
    # Both tiers are still writable from the create flow; the drawer says
    # Optional/Automatic and sends the system words.
    assert 'data-tier="available"' in src
    assert 'data-tier="required"' in src
