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

    def test_email_case_does_not_split_the_account(self, sysdb):
        """Cross-provider identity must not be case-sensitive.

        Providers disagree on normalization — Microsoft lower-cases the
        resolved claim, Google passes the raw `email` claim through — and
        ``repo.get_by_email`` is an exact string match on both backends. So the
        same person signing in through two providers (or through one IdP that
        changed the casing of a claim) would land on two accounts. Normalize
        once here, where every provider passes.
        """
        from app.auth.provisioning import ensure_user
        from src.repositories import users_repo

        first = ensure_user("Mixed.Case@Example.com", "M", source="test")
        second = ensure_user("mixed.case@example.com", "M", source="test")
        assert first["id"] == second["id"]
        # Stored normalized, so a later exact-match lookup finds it too.
        assert first["email"] == "mixed.case@example.com"
        assert users_repo().get_by_email("mixed.case@example.com") is not None

    def test_surrounding_whitespace_does_not_split_the_account(self, sysdb):
        from app.auth.provisioning import ensure_user

        first = ensure_user("spaced@example.com", "S", source="test")
        second = ensure_user("  spaced@example.com  ", "S", source="test")
        assert first["id"] == second["id"]

    def test_preexisting_mixed_case_row_is_matched_not_duplicated(self, sysdb):
        """An account created before normalization landed (raw Google claim)
        must still be matched, never duplicated."""
        import uuid

        from app.auth.provisioning import ensure_user
        from src.repositories import users_repo

        legacy_id = str(uuid.uuid4())
        users_repo().create(id=legacy_id, email="Legacy.User@Example.com", name="L")
        user = ensure_user("legacy.user@example.com", "L", source="test")
        assert user["id"] == legacy_id

    def test_preexisting_case_variants_resolve_to_the_oldest(self, sysdb):
        """Two case-variant rows already coexist — the oldest must win.

        This is the population the case-insensitive lookup was written for: an
        exact-match read that runs FIRST silently preserves the split, because
        the arriving claim matches the newer duplicate byte-for-byte and the
        case-insensitive read is never consulted. The documented contract is
        "oldest wins", so it has to be the ONLY lookup.
        """
        import uuid

        from app.auth.provisioning import ensure_user
        from src.repositories import users_repo

        repo = users_repo()
        old_id = str(uuid.uuid4())
        new_id = str(uuid.uuid4())
        repo.create(id=old_id, email="dup@example.com", name="Old")
        repo.create(id=new_id, email="Dup@Example.com", name="New")
        sysdb.execute("UPDATE users SET created_at = ? WHERE id = ?", ["2025-01-01 00:00:00", old_id])
        sysdb.execute("UPDATE users SET created_at = ? WHERE id = ?", ["2026-06-01 00:00:00", new_id])

        # The claim matches the NEWER row exactly — an exact-first lookup
        # returns it and the split survives.
        user = ensure_user("Dup@Example.com", "New", source="test")
        assert user["id"] == old_id

    def test_deactivated_user_raises(self, sysdb):
        from app.auth.provisioning import UserDeactivatedError, ensure_user
        from src.repositories import users_repo

        user = ensure_user("gone@example.com", "Gone", source="test")
        users_repo().update(id=user["id"], active=False)
        with pytest.raises(UserDeactivatedError):
            ensure_user("gone@example.com", "Gone", source="test")
