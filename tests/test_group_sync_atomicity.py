"""DuckDB-specific concurrency guarantees for google_sync group rebuild.

The cross-engine *functional* contract lives in
``tests/db_pg/test_rbac_contract.py``; these tests pin the DuckDB-only
concurrency behavior that the contract harness can't express:

  1. The rebuild is transaction-isolated — a concurrent reader on a separate
     ``get_system_db()`` cursor never observes the empty post-DELETE /
     pre-INSERT window (the marketplace-drop bug this fix targets).
  2. A DuckDB write-write conflict (two concurrent same-user logins) is
     retried instead of being silently swallowed by the fail-soft OAuth
     caller.
"""

from __future__ import annotations

import duckdb
import pytest

from src.db import get_system_db
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository
from src.repositories.users import UserRepository


@pytest.fixture()
def fresh_system_db(tmp_path, monkeypatch):
    """Point get_system_db() at an isolated DATA_DIR for this test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Reset the module-level singleton so the new DATA_DIR takes effect.
    import src.db as db

    monkeypatch.setattr(db, "_system_db_conn", None, raising=False)
    monkeypatch.setattr(db, "_system_db_path", None, raising=False)
    return get_system_db()


def _seed(conn):
    UserRepository(conn).create(id="u1", email="e@x.com", name="E")
    groups = UserGroupsRepository(conn)
    g1 = groups.create(name="finance", created_by="a")
    g2 = groups.create(name="legal", created_by="a")
    return g1["id"], g2["id"]


def test_reader_never_sees_empty_intermediate(fresh_system_db):
    """While the rebuild's transaction is open with the DELETE applied, a
    separate cursor still sees the previously-committed group set — not the
    empty window that dropped marketplace plugins."""
    g1, g2 = _seed(fresh_system_db)
    UserGroupMembersRepository(get_system_db()).replace_google_sync_groups("u1", [g1, g2])

    writer = get_system_db()
    reader = get_system_db()

    # Manually drive the same DELETE the rebuild does, leaving the txn open.
    writer.execute("BEGIN")
    writer.execute(
        "DELETE FROM user_group_members WHERE user_id = ? AND source = 'google_sync'",
        ["u1"],
    )
    try:
        # Reader is isolated from the uncommitted delete.
        during = set(UserGroupMembersRepository(reader).list_groups_for_user("u1"))
        assert during == {g1, g2}, f"reader saw empty intermediate: {during}"
    finally:
        writer.execute("ROLLBACK")

    after = set(UserGroupMembersRepository(get_system_db()).list_groups_for_user("u1"))
    assert after == {g1, g2}


def test_transaction_conflict_is_retried(fresh_system_db):
    """A TransactionException on the first attempt is retried, not surfaced
    to the fail-soft caller — the refresh still lands."""
    g1, g2 = _seed(fresh_system_db)
    base = get_system_db()

    class FlakyConn:
        """Raises a conflict on the first DELETE, then delegates."""

        def __init__(self, inner):
            self._inner = inner
            self._delete_calls = 0

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("DELETE FROM USER_GROUP_MEMBERS"):
                self._delete_calls += 1
                if self._delete_calls == 1:
                    # Mimic DuckDB's "Conflict on tuple deletion!" — the txn
                    # opened by the preceding BEGIN must be unwound first so
                    # the retry's BEGIN doesn't nest.
                    self._inner.execute("ROLLBACK")
                    raise duckdb.TransactionException("Conflict on tuple deletion!")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    repo = UserGroupMembersRepository(FlakyConn(base))
    repo.replace_google_sync_groups("u1", [g1, g2])  # must not raise

    final = set(UserGroupMembersRepository(get_system_db()).list_groups_for_user("u1"))
    assert final == {g1, g2}


def test_conflict_exhaustion_raises(fresh_system_db):
    """If every attempt conflicts, the last exception escapes (so the caller
    can log it) rather than silently reporting success."""
    g1, _ = _seed(fresh_system_db)
    base = get_system_db()

    class AlwaysConflict:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("DELETE FROM USER_GROUP_MEMBERS"):
                self._inner.execute("ROLLBACK")
                raise duckdb.TransactionException("Conflict on tuple deletion!")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    repo = UserGroupMembersRepository(AlwaysConflict(base))
    with pytest.raises(duckdb.TransactionException):
        repo.replace_google_sync_groups("u1", [g1])
