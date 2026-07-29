"""/library as the single source of truth for store entities.

The Library lists every store entity the caller is allowed to SEE, using the
store's own visibility rule (the same one `/api/marketplace` browse applies):

  * ``approved`` — readable by every authenticated user, i.e. shared with
    everyone, so it appears in every eligible user's Library; and
  * the caller's OWN entries whatever their review state, so a skill you are
    still drafting is in your Library and nobody else's.

Someone else's ``pending`` / ``hidden`` entry is NOT listed: an admin may be
able to read it, but a submission in review is not shared with anyone
(moderation lives at /admin/store).

Visibility and Stack membership are separate properties. Visibility decides who
can discover the entity; an install row decides whether the default agent
actually uses it. So authoring an entity does not put it in your Stack, and
Add/Remove from stack writes ``POST``/``DELETE
/api/store/entities/{id}/install`` — a different API from an artefact's, which
is why each row carries its own ``data-stack-endpoint``.

AGENTS are deliberately not swept — they keep their own surface at /agents — but
an agent the caller INSTALLED still lists.
"""

from __future__ import annotations

import re
import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _entity(*, owner: str, owner_name: str, etype: str, name: str, status: str) -> str:
    """Seed one store entity and return its id."""
    from src.db import get_system_db
    from src.repositories.store_entities import StoreEntitiesRepository

    eid = uuid.uuid4().hex
    StoreEntitiesRepository(get_system_db()).create(
        id=eid,
        owner_user_id=owner,
        owner_username=owner_name,
        type=etype,
        name=name,
        description=f"{name} description",
        category="Productivity",
        version="1.0.0",
        visibility_status=status,
    )
    return eid


def _install(entity_id: str, user_id: str) -> None:
    from src.db import get_system_db
    from src.repositories.user_store_installs import UserStoreInstallsRepository

    UserStoreInstallsRepository(get_system_db()).install(user_id=user_id, entity_id=entity_id)


def _row(text: str, title: str) -> str | None:
    """The one table row whose data-title is ``title``."""
    m = re.search(r'<tr class="lib-row[^"]*"[^>]*data-title="' + re.escape(title) + r'".*?</tr>', text, re.S)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# What the Library contains
# ---------------------------------------------------------------------------


def test_skill_shared_with_everyone_appears_in_every_library(seeded_app):
    """An approved skill is readable by every authenticated user — that IS
    "shared with everyone" — so it belongs in the Library of someone who neither
    authored nor installed it, attributed to its owner."""
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Shared Skill", status="approved")
    assert eid

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Shared Skill")
    assert row, "an approved skill is missing from another user's Library"
    assert 'data-ownership="shared_with_me"' in row
    assert 'data-visibility="workspace"' in row
    assert 'data-type="skill"' in row


def test_someone_elses_unapproved_skill_stays_out(seeded_app):
    """Pending / hidden entries belonging to others are submissions in review,
    not things shared with anyone."""
    _entity(owner="admin", owner_name="admin", etype="skill", name="Still In Review", status="pending")
    _entity(owner="admin", owner_name="admin", etype="skill", name="Hidden Away", status="hidden")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Still In Review" not in text
    assert "Hidden Away" not in text


def test_your_own_unpublished_skill_is_yours_alone(seeded_app):
    """A private item is visible only to its creator — and it reports its real
    store state rather than pretending to be published."""
    _entity(owner="analyst1", owner_name="analyst1", etype="skill", name="My Draft Skill", status="hidden")

    mine = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(mine, "My Draft Skill")
    assert row, "the caller's own unpublished skill is missing from their Library"
    assert 'data-ownership="mine"' in row
    assert 'data-visibility="private"' in row

    theirs = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "My Draft Skill" not in theirs


def test_plugins_are_swept_too_and_agents_are_not(seeded_app):
    """Skills and plugins are listed whether or not they are installed. Agents
    keep their own surface at /agents, so an approved one is not swept."""
    _entity(owner="admin", owner_name="admin", etype="plugin", name="Shared Plugin", status="approved")
    _entity(owner="admin", owner_name="admin", etype="agent", name="Shared Agent", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert _row(text, "Shared Plugin"), "an approved plugin is missing from the Library"
    assert "Shared Agent" not in text


def test_installed_agent_still_lists(seeded_app):
    """The one way an agent reaches the Library: the caller installed it."""
    eid = _entity(owner="admin", owner_name="admin", etype="agent", name="Installed Agent", status="approved")
    _install(eid, "analyst1")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Installed Agent")
    assert row, "an installed agent is missing from the Library"
    assert 'data-type="agent"' in row


def test_an_entity_is_listed_exactly_once_when_installed(seeded_app):
    """Regression: skills/plugins come from the sweep whether installed or not,
    so the installed-items pass must not list them a second time."""
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Installed Once", status="approved")
    _install(eid, "analyst1")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert text.count('data-title="Installed Once"') == 1


# ---------------------------------------------------------------------------
# Stack membership — a separate property, with its own API
# ---------------------------------------------------------------------------


def test_stack_membership_is_separate_from_visibility(seeded_app):
    """Sharing controls discovery; the Stack controls whether the agent uses it.
    A skill shared with everyone but not installed is therefore visible AND
    out-of-stack, offering Add — wired to the store's install endpoint, not an
    artefact's stack endpoint."""
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Discoverable Only", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Discoverable Only")
    assert row
    assert 'data-visibility="workspace"' in row  # everyone can discover it
    assert 'data-stack="available"' in row  # …but the agent can't use it yet
    assert f'data-add-to-stack="{eid}"' in row
    assert f'data-stack-endpoint="/api/store/entities/{eid}/install"' in row


def test_installing_makes_the_membership_removable(seeded_app):
    """An install IS the membership, and it is the caller's to drop again — so
    the row offers Remove against the same endpoint."""
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Added Skill", status="approved")
    _install(eid, "analyst1")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Added Skill")
    assert row
    assert 'data-stack="in_stack"' in row
    assert f'data-remove-from-stack="{eid}"' in row
    assert f'data-stack-endpoint="/api/store/entities/{eid}/install"' in row


def test_authoring_does_not_add_to_your_stack(seeded_app):
    """Creating a skill puts it in your Library, not in your Stack: the two are
    separate properties, so a fresh one is visible-and-addable."""
    _entity(owner="analyst1", owner_name="analyst1", etype="skill", name="Freshly Authored", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Freshly Authored")
    assert row, "a newly created skill must appear in its creator's Library"
    assert 'data-stack="available"' in row
    assert "data-add-to-stack=" in row


def test_admin_required_grant_is_locked_in_stack(seeded_app):
    """An admin-required resource reads "In Stack" like any other member — it is
    one — and is marked by a lock plus the locked tooltip. There is no control
    beside it: the membership is the admin's, not the caller's."""
    from src.db import get_system_db
    from src.repositories import data_packages_repo
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("req-lock-grp") or groups.create(name="req-lock-grp", description="t", created_by="t")
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    pkg = data_packages_repo().create(
        name="Mandated Data", slug="mandated-data", description="d", icon=None, color=None, created_by="admin"
    )
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="data_package", resource_id=pkg, assigned_by="admin", requirement="required"
    )

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Mandated Data")
    assert row
    assert 'data-stack="in_stack"' in row
    assert "Required by your admin and cannot be removed from your stack." in row
    # No control: neither Add nor Remove is the caller's to press.
    assert "data-add-to-stack=" not in row
    assert "data-remove-from-stack=" not in row


# ---------------------------------------------------------------------------
# Trust chip — opt-in 3-state (org / verified / unverified)
# ---------------------------------------------------------------------------


def _set_verification(entity_id: str, state: str) -> None:
    from src.db import get_system_db
    from src.repositories.store_entities import StoreEntitiesRepository

    StoreEntitiesRepository(get_system_db()).set_verification(entity_id, state, by_user_id="admin")


def _set_publisher_kind(entity_id: str, kind: str) -> None:
    from src.db import get_system_db

    get_system_db().execute(
        "UPDATE store_entities SET publisher_kind = ? WHERE id = ?",
        [kind, entity_id],
    )


def test_unverified_chip_absent_when_flag_off(seeded_app):
    """Default behavior (flag off): no unverified chip ever renders — absence of
    a chip is the neutral default and the existing instance look is unchanged."""
    _entity(owner="admin", owner_name="admin", etype="skill", name="Flag Off Skill", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Flag Off Skill")
    assert row, "approved skill must appear in library"
    assert "lib-trust--unverified" not in row
    assert "Community" not in row


def test_unverified_chip_renders_when_flag_on(seeded_app, monkeypatch):
    """With the flag enabled, a user-authored unverified Store entity renders the
    amber 'Community' chip."""
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "true")
    _entity(owner="admin", owner_name="admin", etype="skill", name="Community Skill", status="approved")
    # verification_state defaults to 'none' on create — unverified by definition.

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Community Skill")
    assert row, "approved skill must appear in library"
    assert "lib-trust--unverified" in row
    assert "Community" in row
    assert "Community-contributed, not yet verified" in row


def test_verified_chip_renders_green_when_flag_on(seeded_app, monkeypatch):
    """A verified Store entity always renders the green chip regardless of the
    unverified flag — the flag only gates the amber branch."""
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "true")
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Verified Skill", status="approved")
    _set_verification(eid, "verified")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Verified Skill")
    assert row, "approved skill must appear in library"
    assert "lib-trust--verified" in row
    assert "lib-trust--unverified" not in row


def test_org_chip_renders_gray_regardless_of_flag(seeded_app, monkeypatch):
    """An organization-published item always renders the gray 'Organization'
    chip — the unverified flag does not affect it."""
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "false")
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Org Skill", status="approved")
    _set_publisher_kind(eid, "organization")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Org Skill")
    assert row, "approved org skill must appear in library"
    assert "lib-trust--org" in row
    assert "lib-trust--unverified" not in row


def test_verified_chip_renders_green_when_flag_off(seeded_app):
    """The verified (green) chip is NOT gated by the unverified flag — it renders
    regardless, as it always has."""
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Always Verified", status="approved")
    _set_verification(eid, "verified")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Always Verified")
    assert row
    assert "lib-trust--verified" in row
