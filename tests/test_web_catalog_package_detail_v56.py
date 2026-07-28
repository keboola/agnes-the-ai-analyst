"""Template tests for the v56 ``/catalog/p/<slug>`` rewrite.

The Foundry Data team spec calls for a richer per-package detail page:
hero with owner line + tags + badge, "What it is" markdown body,
"Use it when" / "Skip it when" arrays, per-table extended detail
(grain / platforms / partition / history / gotchas) in collapsible
rows, and a package-level example-questions panel.

Each test asserts on rendered HTML substrings rather than DOM
structure — keeps the tests independent of CSS class naming changes
while still pinning the contract.

Empty-field behaviour: sections backed by an unset field MUST be
hidden entirely (no "No X yet" placeholder noise on the public-facing
drilldown — sections are opt-in content, not required slots).
"""

from __future__ import annotations

import uuid

import pytest

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_pkg(**fields) -> str:
    """Insert directly so we can backdate created_at + set creator without
    bouncing through API rate limits."""
    from src.repositories.data_packages import DataPackagesRepository

    slug = fields.pop("slug", f"p{uuid.uuid4().hex[:6]}")
    conn = get_system_db()
    pid = DataPackagesRepository(conn).create(
        name=fields.pop("name", "Sales bundle"),
        slug=slug,
        description=fields.pop("description", "card desc"),
        icon=None, color=None,
        created_by=fields.pop("created_by", "admin1"),
        **fields,
    )
    conn.close()
    return pid, slug


def _grant_everyone(pkg_id: str) -> None:
    """Make the package visible to analyst1 so the detail page can render."""
    conn = get_system_db()
    ev = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()
    conn.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) "
        "VALUES ('analyst1', ?, 'test') ON CONFLICT DO NOTHING",
        [ev[0]],
    )
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, 'data_package', ?, 'available', CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), ev[0], pkg_id],
    )
    conn.close()


class TestOwnerAndTags:
    def test_renders_owner_line(self, seeded_app):
        pid, slug = _seed_pkg(owner_name="Jane Doe", owner_team="Sales Ops")
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.text
        assert "Jane Doe" in body
        assert "Sales Ops" in body

    def test_omits_owner_line_when_unset(self, seeded_app):
        pid, slug = _seed_pkg()
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert "Owned by" not in r.text

    def test_renders_each_tag_as_pill(self, seeded_app):
        pid, slug = _seed_pkg(tags=["Finance", "Revenue", "Margin"])
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        for tag in ("Finance", "Revenue", "Margin"):
            assert tag in body


class TestBadges:
    def test_renders_curated_badge_for_admin_created(self, seeded_app):
        pid, slug = _seed_pkg(created_by="admin1")
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert "Curated" in r.text

    def test_renders_new_badge_for_recent_package(self, seeded_app):
        pid, slug = _seed_pkg(created_by="admin1")
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert "New" in r.text

    def test_omits_new_badge_for_old_package(self, seeded_app):
        from datetime import datetime, timedelta, timezone

        pid, slug = _seed_pkg(created_by="admin1")
        _grant_everyone(pid)
        conn = get_system_db()
        conn.execute(
            "UPDATE data_packages SET created_at = ? WHERE id = ?",
            [datetime.now(timezone.utc) - timedelta(days=120), pid],
        )
        conn.close()
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # Curated badge still present (admin1 is in Admin group), New is gone.
        body = r.text
        assert "Curated" in body
        # Use a class hook so we don't match the literal word "New" in
        # other UI copy (e.g. "New Recipe").
        assert 'data-badge="new"' not in body


class TestContentSections:
    def test_renders_long_description_when_present(self, seeded_app):
        pid, slug = _seed_pkg(
            long_description="The single source of truth for Y.",
        )
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert "The single source of truth for Y." in r.text

    def test_omits_long_description_section_when_empty(self, seeded_app):
        pid, slug = _seed_pkg()
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # Section header should not appear when the body is empty.
        assert "What it is" not in r.text

    def test_renders_use_it_when_list(self, seeded_app):
        pid, slug = _seed_pkg(
            when_to_use=["You need monetary metrics", "You are computing margin"],
        )
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        assert "Use it when" in body
        assert "You need monetary metrics" in body
        # Apostrophes get HTML-escaped by Jinja's autoescape; use plain
        # ASCII text in the assertion fixture to keep this test stable
        # regardless of the renderer's escape policy.
        assert "You are computing margin" in body

    def test_renders_skip_it_when_list(self, seeded_app):
        pid, slug = _seed_pkg(
            when_not_to_use=["You only need session counts"],
        )
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        assert "Skip it when" in body
        assert "You only need session counts" in body

    def test_omits_use_skip_sections_when_empty(self, seeded_app):
        pid, slug = _seed_pkg()
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert "Use it when" not in r.text
        assert "Skip it when" not in r.text

    def test_renders_example_questions_panel(self, seeded_app):
        qs = [
            "What was revenue last week?",
            "Top 10 customers by spend.",
        ]
        pid, slug = _seed_pkg(example_questions=qs)
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        assert "Example questions" in body
        for q in qs:
            assert q in body

    def test_omits_example_questions_panel_when_empty(self, seeded_app):
        pid, slug = _seed_pkg()
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert "Example questions" not in r.text


class TestPerTableExtendedDetail:
    def _seed_table_with_docs(self, pkg_id: str) -> str:
        from src.repositories.data_packages import DataPackagesRepository
        from src.repositories.table_registry import TableRegistryRepository

        tid = f"tbl_{uuid.uuid4().hex[:8]}"
        conn = get_system_db()
        conn.execute(
            "INSERT INTO table_registry(id, name, source_type, query_mode, description) "
            "VALUES (?, ?, 'keboola', 'local', 'orders table description')",
            [tid, "orders"],
        )
        TableRegistryRepository(conn).update_docs(
            tid,
            grain="1 row per order event",
            platforms=["MBNXT", "Legacy"],
            partition_col="event_date",
            history="Full",
            gotchas=[
                {"key": True, "body": "Always filter mbnxt before joining."},
                {"key": False, "body": "Country goes on S1, not on plugin tables."},
            ],
        )
        DataPackagesRepository(conn).add_table(pkg_id, tid, added_by="test")
        conn.close()
        return tid

    def test_renders_extended_per_table_detail(self, seeded_app):
        pid, slug = _seed_pkg()
        _grant_everyone(pid)
        self._seed_table_with_docs(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        assert "1 row per order event" in body
        assert "MBNXT" in body
        assert "event_date" in body
        assert "Full" in body
        assert "Always filter mbnxt before joining." in body

    def test_first_key_gotcha_rendered_distinctly(self, seeded_app):
        pid, slug = _seed_pkg()
        _grant_everyone(pid)
        self._seed_table_with_docs(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # The class hook on the key-gotcha block lets us pin the rendering
        # contract without depending on CSS specifics.
        assert 'data-gotcha="key"' in r.text


class TestAdminAffordances:
    def test_admin_sees_edit_button(self, seeded_app):
        pid, slug = _seed_pkg()
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}?source=test",
            headers=_auth(seeded_app["admin_token"]),
        )
        # Admin sees at least one Edit affordance; the read-only path
        # for non-admin shouldn't surface it.
        assert "Edit" in r.text or "+ Add" in r.text

    def test_non_admin_no_edit_button(self, seeded_app):
        pid, slug = _seed_pkg()
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # No section-level Edit affordance — the page is read-only.
        # (We don't assert on the literal word "Edit" because it might
        # legitimately appear in admin-only nav above the catalog page.)
        assert 'data-section-edit="package"' not in r.text
