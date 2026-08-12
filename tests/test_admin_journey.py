"""The `/admin` journey layer — the setup path + the People → Data → Access
gap cards (`app/services/admin_dashboard.py::resolve_journey`).

A different contract from the signal zones, so a different suite:

  * the setup path SELF-RETIRES (renders until all four stages are done),
    where the zones render only when something needs an admin;
  * a gap card ALWAYS renders — the healthy chain is information — so the
    thing to pin is that each card's gap count derives from the same reads
    the pages it links to are built on, and that a broken resolver degrades
    to a visible "could not be checked" rather than a healthy-looking card;
  * every href lands on a page where the named work can actually be done —
    the same rule the signal registry enforces.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import admin_dashboard
from app.services.admin_dashboard import resolve_journey

_SERVICE_SRC = Path("app/services/admin_dashboard.py").read_text(encoding="utf-8")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _Raises:
    """A repo factory stand-in whose every method raises — the 'one broken
    backend' the isolation rule exists for."""

    def __getattr__(self, name):  # pragma: no cover - trivial
        raise RuntimeError("repo unavailable")


class TestJourneyShape:
    def test_setup_has_the_four_stages_in_order(self, seeded_app):
        journey = resolve_journey()
        assert journey["setup"] is not None
        keys = [s["key"] for s in journey["setup"]["steps"]]
        assert keys == ["connect", "tables", "package", "share"]

    def test_exactly_one_step_is_current_until_complete(self, seeded_app):
        setup = resolve_journey()["setup"]
        current = [s for s in setup["steps"] if s["current"]]
        if setup["complete"]:
            assert current == []
        else:
            assert len(current) == 1
            # ...and it is the FIRST not-done step: the path is a sequence,
            # not four alarms.
            first_undone = next(s for s in setup["steps"] if not s["done"])
            assert current[0] is first_undone

    def test_gap_cards_are_the_three_chain_links(self, seeded_app):
        gaps = resolve_journey()["gaps"]
        assert [g["key"] for g in gaps] == ["people", "data", "access"]
        for g in gaps:
            assert g["href"].startswith("/admin")
            assert not g["failed"]

    def test_every_journey_href_is_a_real_admin_route(self):
        """Same dead-link rule as the signal registry: these URLs are
        hardcoded in a module the router does not import, so nothing else
        notices when a destination moves."""
        router_src = Path("app/web/router.py").read_text(encoding="utf-8")
        declared = set(re.findall(r'"(/admin[^"?]*)"', _SERVICE_SRC))
        routes = set(re.findall(r'@router\.get\("(/admin[^"]*)"', router_src))
        prefixes = tuple(routes)
        missing = [d for d in declared if d not in routes and not d.startswith(prefixes)]
        assert not missing, f"journey hrefs with no matching /admin route: {missing}"


class TestGapIsolation:
    def test_a_raising_repo_degrades_to_a_failed_card(self, seeded_app, monkeypatch):
        import src.repositories as repos

        monkeypatch.setattr(repos, "users_repo", lambda: _Raises())
        journey = resolve_journey()
        people = next(g for g in journey["gaps"] if g["key"] == "people")
        assert people["failed"] is True
        # The other two cards still resolved — one broken backend must not
        # blank the whole chain.
        assert [g["key"] for g in journey["gaps"]] == ["people", "data", "access"]
        assert not journey["gaps"][1]["failed"]

    def test_a_raising_repo_never_500s_the_admin_page(self, seeded_app, monkeypatch):
        import src.repositories as repos

        monkeypatch.setattr(repos, "data_packages_repo", lambda: _Raises())
        c = seeded_app["client"]
        resp = c.get("/admin", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Could not be checked" in resp.text


class TestGapSemantics:
    def test_everyone_grant_covers_every_account(self, seeded_app, monkeypatch):
        """Auto-membership: a data-package grant to the system Everyone group
        means nobody is uncovered, without enumerating a single membership."""
        from src.repositories import resource_grants_repo, user_groups_repo

        everyone = next(g for g in user_groups_repo().list_all() if g.get("is_system") and g["name"] == "Everyone")
        grants = resource_grants_repo()
        grant_id = grants.create(group_id=everyone["id"], resource_type="data_package", resource_id="pkg-x")
        try:
            people = next(g for g in resolve_journey()["gaps"] if g["key"] == "people")
            assert people["gap_count"] == 0
        finally:
            grants.delete(grant_id)

    def test_unpackaged_counts_only_distributable_modes(self):
        """`remote` tables are reachable without a package (server-side
        execution), so counting them as 'analysts cannot pull them' would be
        a false alarm — while a blank query_mode folds to `local`, the same
        rule the manifest and the distribution gate apply."""
        assert admin_dashboard._is_distributable({"query_mode": ""})
        assert admin_dashboard._is_distributable({"query_mode": None})
        assert admin_dashboard._is_distributable({"query_mode": "local"})
        assert admin_dashboard._is_distributable({"query_mode": "materialized"})
        assert not admin_dashboard._is_distributable({"query_mode": "remote"})

    def test_gap_and_ok_text_never_render_together(self, seeded_app):
        for g in resolve_journey()["gaps"]:
            # The template branches on gap_count; both texts existing is fine,
            # but a card must always carry the one its branch will need.
            if g["gap_count"] > 0:
                assert g["gap_text"]
            else:
                assert g["ok_text"]


class TestHubRendersTheJourney:
    def test_gap_cards_render_on_admin(self, seeded_app):
        c = seeded_app["client"]
        body = c.get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert 'data-gap="people"' in body
        assert 'data-gap="data"' in body
        assert 'data-gap="access"' in body

    def test_setup_path_renders_until_complete(self, seeded_app, monkeypatch):
        c = seeded_app["client"]
        body = c.get("/admin", headers=_auth(seeded_app["admin_token"])).text
        journey = resolve_journey()
        if journey["setup"]["complete"]:
            assert "Set up this instance" not in body
        else:
            assert "Set up this instance" in body

    def test_setup_path_retires_itself_when_complete(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            admin_dashboard,
            "_resolve_setup",
            lambda: {"complete": True, "steps": []},
        )
        c = seeded_app["client"]
        body = c.get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert "Set up this instance" not in body


@pytest.fixture(autouse=True)
def _fresh_cache():
    admin_dashboard.invalidate_cache()
    yield
    admin_dashboard.invalidate_cache()
