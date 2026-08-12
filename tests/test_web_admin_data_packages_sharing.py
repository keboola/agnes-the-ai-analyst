"""The Packages workspace (`GET /admin/data-packages`) — sharing rendered and
editable from the package's side, plus the unpackaged-tables tray.

Before this page grew the editor, "who can use this package?" had no answer
anywhere in the product: grants were written only from a group's Access tab,
and this page hid its footer buttons to stay read-only. What this suite pins:

  * the sharing strip renders per package, from the same `resource_grants`
    rows the group-side editor writes — including the tier, worded
    Optional/Automatic with the API's own word carried in a title;
  * an ungranted package says so out loud ("shared with nobody"), because
    that state was previously invisible and is the one that strands analysts;
  * the unpackaged tray applies the same distributable fold as the /admin
    gap card (blank → local; `remote` excluded);
  * the audit contract this page already had is untouched — every package
    renders regardless of grant.
"""

from __future__ import annotations

import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_pkg(slug_prefix: str, name: str) -> str:
    from src.repositories import data_packages_repo

    return data_packages_repo().create(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:6]}",
        name=name,
        description="",
        icon=None,
        color=None,
        created_by="test",
    )


class TestSharingStrip:
    def test_ungranted_package_says_shared_with_nobody(self, seeded_app):
        pkg_id = _mk_pkg("share-none", "Share None Pkg")
        c = seeded_app["client"]
        body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert f'data-share-strip="{pkg_id}"' in body
        assert "Shared with nobody" in body

    def test_granted_package_names_the_group_and_tier(self, seeded_app):
        from src.repositories import resource_grants_repo, user_groups_repo

        pkg_id = _mk_pkg("share-tier", "Share Tier Pkg")
        everyone = next(g for g in user_groups_repo().list_all() if g.get("is_system") and g["name"] == "Everyone")
        grants = resource_grants_repo()
        gid = grants.create(
            group_id=everyone["id"],
            resource_type="data_package",
            resource_id=pkg_id,
            requirement="required",
        )
        try:
            c = seeded_app["client"]
            body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
            # The strip for THIS package carries the group and the automatic
            # marker; the API word rides in the title attribute.
            strip_start = body.index(f'data-share-strip="{pkg_id}"')
            strip = body[strip_start : strip_start + 2000]
            assert "Everyone" in strip
            assert "· auto" in strip
            assert "required" in strip  # title attribute keeps the system word
        finally:
            grants.delete(gid)

    def test_share_editor_scaffolding_is_served(self, seeded_app):
        """The modal + the two datasets it renders from. The editor itself is
        client-side against /api/admin/grants — the same endpoint the
        group-side matrix uses, which is what keeps the two in agreement."""
        c = seeded_app["client"]
        body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert 'id="adp-share-modal"' in body
        assert "/api/admin/grants" in body
        assert "Optional" in body and "Automatic" in body


class TestUnpackagedTray:
    def test_distributable_unpackaged_table_lands_in_the_tray(self, seeded_app):
        from src.repositories import table_registry_repo

        repo = table_registry_repo()
        tid = f"tray-{uuid.uuid4().hex[:6]}"
        repo.register(
            id=tid,
            name=f"tray_table_{tid[-6:]}",
            source_type="keboola",
            bucket="in.c-test",
            source_table="tray",
            query_mode="local",
        )
        try:
            c = seeded_app["client"]
            body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
            assert "in no package" in body
            assert f"tray_table_{tid[-6:]}" in body
        finally:
            repo.unregister(tid)

    def test_remote_tables_do_not_raise_the_tray_alarm(self, seeded_app):
        """`remote` rows answer server-side without a package — counting them
        as 'nobody can pull them' would be a standing false alarm."""
        from src.repositories import table_registry_repo

        repo = table_registry_repo()
        tid = f"tray-remote-{uuid.uuid4().hex[:6]}"
        repo.register(
            id=tid,
            name=f"tray_remote_{tid[-6:]}",
            source_type="bigquery",
            bucket="ds",
            source_table="remote",
            query_mode="remote",
        )
        try:
            c = seeded_app["client"]
            body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
            assert f"tray_remote_{tid[-6:]}" not in body
        finally:
            repo.unregister(tid)
