"""Both stack-membership modes, side by side (spec 2026-08-07).

`features.stack_auto_membership` picks the semantics; the CLASSIC default is
the pre-redesign subscribe model, byte-for-byte:

| concern            | classic (default)                      | auto (flag on)              |
|--------------------|----------------------------------------|-----------------------------|
| stack()            | required ∪ (subscribed ∩ available)    | required ∪ available        |
| browse().in_stack  | id ∈ required ∪ subscribed             | always True                 |
| materialized       | == membership (all members local)      | required ∪ subscribed       |

The formulas are asserted per mode through the SAME seeded fixture so a
drift in either direction fails loudly. The grant-downgrade fan-out half of
the contract lives with the access API tests.
"""

import duckdb
import pytest

from app.resource_types import ResourceType
from app.services.stack_resolver import StackResolver
from src.db import _ensure_schema


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    _ensure_schema(c)
    c.execute("INSERT INTO user_groups(id, name) VALUES ('g1', 'Analysts')")
    c.execute("INSERT INTO user_group_members(user_id, group_id, source) VALUES ('u1', 'g1', 'admin')")
    for pid, slug in (("pkg_req", "req"), ("pkg_avail", "avail"), ("pkg_sub", "sub")):
        c.execute(
            "INSERT INTO data_packages(id, slug, name, description, icon, color) VALUES (?, ?, ?, 'd', 'x', '#abc')",
            [pid, slug, slug.title()],
        )
    return c


def _grant(conn, resource_id, requirement):
    import uuid

    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, 'g1', 'data_package', ?, ?, CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), resource_id, requirement],
    )


def _subscribe(conn, resource_id):
    conn.execute(
        "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id, subscribed_at) "
        "VALUES ('u1', 'data_package', ?, CURRENT_TIMESTAMP)",
        [resource_id],
    )


@pytest.fixture
def seeded(conn):
    """One of each: required, available-unsubscribed, available-subscribed."""
    _grant(conn, "pkg_req", "required")
    _grant(conn, "pkg_avail", "available")
    _grant(conn, "pkg_sub", "available")
    _subscribe(conn, "pkg_sub")
    return conn


class TestClassicMode:
    """Default — no flag, no preset: the pre-redesign subscribe model."""

    @pytest.fixture(autouse=True)
    def _default_env(self, monkeypatch):
        monkeypatch.delenv("AGNES_STACK_AUTO_MEMBERSHIP", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)

    def test_stack_is_required_plus_subscribed(self, seeded):
        entries = StackResolver(seeded).stack("u1", ResourceType.DATA_PACKAGE)
        ids = {e.id for e in entries}
        assert ids == {"pkg_req", "pkg_sub"}, "an unsubscribed available grant must NOT be a stack member"
        assert all(e.in_stack for e in entries)
        assert all(e.materialized for e in entries), "classic members are always local"

    def test_browse_marks_in_stack_by_subscription(self, seeded):
        by_id = {e.id: e for e in StackResolver(seeded).browse("u1", ResourceType.DATA_PACKAGE)}
        assert set(by_id) == {"pkg_req", "pkg_avail", "pkg_sub"}, "browse still lists every grant"
        assert by_id["pkg_req"].in_stack is True
        assert by_id["pkg_sub"].in_stack is True
        assert by_id["pkg_avail"].in_stack is False, "unsubscribed available reads as addable"

    def test_subscribing_joins_the_stack(self, seeded):
        _subscribe(seeded, "pkg_avail")
        ids = {e.id for e in StackResolver(seeded).stack("u1", ResourceType.DATA_PACKAGE)}
        assert "pkg_avail" in ids

    def test_browse_admin_uses_the_classic_formula(self, seeded):
        """The admin god-mode view has its own classic/auto fork, and until
        now nothing asserted the classic half on either backend: the two web
        suites that render /catalog and /corporate-memory pin
        AGNES_STACK_AUTO_MEMBERSHIP=1 in autouse fixtures, so a silent
        inversion of the two branches would have passed CI
        (/agnes-review parity reviewer on #1199).
        """
        by_id = {e.id: e for e in StackResolver(seeded).browse_admin("u1", ResourceType.DATA_PACKAGE)}
        assert set(by_id) == {"pkg_req", "pkg_avail", "pkg_sub"}, "admin Browse lists every package"
        assert by_id["pkg_req"].in_stack is True
        assert by_id["pkg_sub"].in_stack is True
        assert by_id["pkg_avail"].in_stack is False, (
            "an available grant the admin never subscribed to must read as addable in classic mode"
        )
        # Classic membership is always local, so the two flags agree.
        for e in by_id.values():
            assert e.materialized is e.in_stack

    def test_browse_admin_lists_a_package_with_no_grant_at_all(self, seeded):
        """god-mode is "everything", not "everything I am granted"."""
        seeded.execute(
            "INSERT INTO data_packages(id, slug, name, description, icon, color) "
            "VALUES ('pkg_none', 'none', 'None', 'd', 'x', '#abc')"
        )
        by_id = {e.id: e for e in StackResolver(seeded).browse_admin("u1", ResourceType.DATA_PACKAGE)}
        assert "pkg_none" in by_id
        assert by_id["pkg_none"].in_stack is False


class TestAutoMembershipMode:
    """Flag on: the 0.82.0 auto-membership semantics, unchanged."""

    @pytest.fixture(autouse=True)
    def _auto_env(self, monkeypatch):
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")

    def test_stack_is_every_grant(self, seeded):
        by_id = {e.id: e for e in StackResolver(seeded).stack("u1", ResourceType.DATA_PACKAGE)}
        assert set(by_id) == {"pkg_req", "pkg_avail", "pkg_sub"}
        assert by_id["pkg_avail"].materialized is False, "unsubscribed → listed but not local"
        assert by_id["pkg_sub"].materialized is True

    def test_browse_marks_everything_in_stack(self, seeded):
        entries = StackResolver(seeded).browse("u1", ResourceType.DATA_PACKAGE)
        assert all(e.in_stack for e in entries)

    def test_browse_admin_counts_available_grants_as_in_stack(self, seeded):
        """The other half of the fork: auto-membership additionally counts the
        admin's own `available` grants, with `materialized` still reflecting
        only required-or-subscribed."""
        by_id = {e.id: e for e in StackResolver(seeded).browse_admin("u1", ResourceType.DATA_PACKAGE)}
        assert by_id["pkg_avail"].in_stack is True, "auto-membership counts an unsubscribed available grant"
        assert by_id["pkg_avail"].materialized is False, "…but it is not a local copy"
        assert by_id["pkg_req"].materialized is True
        assert by_id["pkg_sub"].materialized is True

    def test_preset_redesign_implies_auto(self, seeded, monkeypatch):
        monkeypatch.delenv("AGNES_STACK_AUTO_MEMBERSHIP", raising=False)
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "redesign")
        ids = {e.id for e in StackResolver(seeded).stack("u1", ResourceType.DATA_PACKAGE)}
        assert ids == {"pkg_req", "pkg_avail", "pkg_sub"}
