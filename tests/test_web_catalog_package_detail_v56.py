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
        icon=None,
        color=None,
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
    def test_org_published_package_shows_the_shared_trust_marker(self, seeded_app, monkeypatch):
        """v113: the amber derived `Curated` badge is replaced by the SAME trust
        marker the Library row and the store-item detail page render, in its
        labelled form (a hero has room for the word).

        Driven by the STORED publisher_kind, not by whether `created_by` happens
        to be in the Admin group right now — which is the whole point: an admin
        leaving that group used to un-curate everything they had created."""
        # Paper only: css/trustmark.css is scoped to it, and `mark()` renders
        # nothing without `paper=True`. Blue keeps the amber badge — asserted by
        # test_default_theme_hero_keeps_its_amber_curated_badge below.
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        pid, slug = _seed_pkg(created_by="admin1", publisher_kind="organization")
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        assert 'class="ds-trust ds-trust--org ds-trust--label"' in body   # icon + word
        assert 'data-tip="Published by your organization."' in body
        assert 'aria-label="Published by your organization."' in body
        # The retired derived badge must not come back. Matched as the rendered
        # element: the page inlines the detail stylesheet, whose comment still
        # NAMES the dead class to explain where the claim went.
        assert 'class="detail-badge detail-badge--curated"' not in body
        assert ">Curated<" not in body

    def test_default_theme_hero_keeps_its_amber_curated_badge(self, seeded_app, monkeypatch):
        """The DEFAULT hero is unchanged. `.ds-trust` is paper-only, so blue keeps
        the amber badge it has always shown — now driven by the stored
        publisher_kind rather than the creator's live Admin-group membership, so
        it no longer disappears when an admin leaves the group."""
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        pid, slug = _seed_pkg(created_by="admin1", publisher_kind="organization")
        _grant_everyone(pid)
        body = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        ).text
        assert 'class="detail-badge detail-badge--curated"' in body
        assert 'class="ds-trust' not in body

    def test_user_published_package_shows_no_trust_marker(self, seeded_app):
        """A package has no verification workflow — there is no reviewer for one to
        earn a Verified from — so 'Community' would assert a process this entity
        type does not have. Organization or nothing."""
        pid, slug = _seed_pkg(created_by="analyst1", publisher_kind="user")
        _grant_everyone(pid)
        r = seeded_app["client"].get(
            f"/catalog/p/{slug}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert 'class="ds-trust ds-trust--org' not in r.text
        assert 'class="ds-trust ds-trust--community' not in r.text

    def test_admin_created_package_is_marked_org_at_creation_not_derived(self, seeded_app):
        """The create endpoint is `require_admin`-gated, so a package made through
        it IS the organization publishing. Writing that at creation is what keeps
        the claim from evaporating when the creating admin's groups change."""
        r = seeded_app["client"].post(
            "/api/admin/data-packages",
            json={"name": "Made By Admin", "slug": "made-by-admin"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text
        # The create response is deliberately just {"id": ...}; read the package
        # back rather than widening that contract for a test.
        pid = r.json()["id"]
        got = seeded_app["client"].get(
            f"/api/admin/data-packages/{pid}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert got.status_code == 200, got.text
        assert got.json()["publisher_kind"] == "organization"

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
        # The long-description block is omitted when the body is empty.
        # Assert on the markup hook, not the literal "What it is" copy —
        # that phrase also appears in the shared detail-hero CSS comments
        # (mirrors the data-badge="new" hook used above).
        assert 'data-section="long-description"' not in r.text

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
