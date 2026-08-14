"""Pure helpers for reading/binding the Keboola project identity."""

from app.keboola_identity import project_identity, project_matches


class TestProjectIdentity:
    def test_reads_owner_id_and_name(self):
        assert project_identity({"owner": {"id": 12345, "name": "Acme"}}) == (12345, "Acme")

    def test_missing_owner_id_returns_none(self):
        assert project_identity({"owner": {"name": "x"}}) == (None, "")
        assert project_identity({}) == (None, "")
        assert project_identity(None) == (None, "")


class TestProjectMatches:
    def test_int_vs_str_coercion(self):
        # config from ${ENV} interpolation is a string; verify returns int (or str).
        assert project_matches("12345", {"owner": {"id": 12345}}) is True
        assert project_matches(12345, {"owner": {"id": "12345"}}) is True

    def test_mismatch(self):
        assert project_matches("12345", {"owner": {"id": 1}}) is False

    def test_none_holes_never_match(self):
        # An unreadable identity must never compare equal (spec: explicit None reject).
        assert project_matches("12345", {}) is False
        assert project_matches(None, {"owner": {"id": 12345}}) is False
        assert project_matches(None, {}) is False
