"""Contract for the shared first-login provisioning helper.

Google and Keboola logins must run the SAME four steps: create user,
Everyone membership, v39 system-plugin fanout, deactivated rejection.
"""

import pytest


@pytest.fixture
def sysdb(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from src.db import get_system_db

    conn = get_system_db()
    yield conn
    conn.close()


class TestEnsureUser:
    def test_creates_user_with_everyone_membership(self, sysdb):
        from app.auth.provisioning import ensure_user
        from src.repositories import users_repo

        user = ensure_user("new@example.com", "New User", source="test:first-signin")
        assert user["email"] == "new@example.com"
        stored = users_repo().get_by_email("new@example.com")
        assert stored is not None
        # Everyone membership (auto-membership group) was granted at creation.
        rows = sysdb.execute(
            "SELECT g.name FROM user_group_members m JOIN user_groups g ON g.id = m.group_id WHERE m.user_id = ?",
            [stored["id"]],
        ).fetchall()
        assert ("Everyone",) in rows

    def test_returning_user_is_returned_not_recreated(self, sysdb):
        from app.auth.provisioning import ensure_user

        first = ensure_user("again@example.com", "A", source="test")
        second = ensure_user("again@example.com", "A", source="test")
        assert first["id"] == second["id"]

    def test_deactivated_user_raises(self, sysdb):
        from app.auth.provisioning import UserDeactivatedError, ensure_user
        from src.repositories import users_repo

        user = ensure_user("gone@example.com", "Gone", source="test")
        users_repo().update(id=user["id"], active=False)
        with pytest.raises(UserDeactivatedError):
            ensure_user("gone@example.com", "Gone", source="test")
