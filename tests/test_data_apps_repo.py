import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.data_apps import DataAppsRepository


@pytest.fixture
def repo():
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return DataAppsRepository(conn)


class TestCreateAndRead:
    def test_create_assigns_app_prefix_id(self, repo):
        aid = repo.create(slug="sales-dash", name="Sales dashboard", owner_user_id="u1")
        assert aid.startswith("app_")
        row = repo.get(aid)
        assert row["slug"] == "sales-dash"
        assert row["state"] == "created"
        assert row["repo_mode"] == "internal"
        assert row["sleep_mode"] == "recreate"

    def test_slug_unique(self, repo):
        repo.create(slug="dup", name="A", owner_user_id="u1")
        with pytest.raises(duckdb.ConstraintException):
            repo.create(slug="dup", name="B", owner_user_id="u2")

    def test_get_by_slug(self, repo):
        aid = repo.create(slug="x", name="X", owner_user_id="u1")
        assert repo.get_by_slug("x")["id"] == aid
        assert repo.get_by_slug("nope") is None


class TestLifecycle:
    def test_state_and_deploy(self, repo):
        aid = repo.create(slug="s", name="S", owner_user_id="u1")
        repo.set_state(aid, "deploying")
        assert repo.get(aid)["state"] == "deploying"
        repo.record_deploy(aid, "abc123")
        row = repo.get(aid)
        assert row["deployed_sha"] == "abc123"
        assert row["last_deploy_at"] is not None

    def test_list_idle(self, repo):
        aid = repo.create(slug="i", name="I", owner_user_id="u1")
        repo.set_state(aid, "running")
        repo.conn.execute("UPDATE data_apps SET last_request_at = now() - INTERVAL 2 HOUR WHERE id = ?", [aid])
        assert [r["id"] for r in repo.list_idle(older_than_s=3600)] == [aid]
        assert repo.list_idle(older_than_s=3600 * 3) == []

    def test_update_whitelist(self, repo):
        aid = repo.create(slug="w", name="W", owner_user_id="u1")
        assert repo.update(aid, mem_limit="2g", service_token_id="t1") is True
        row = repo.get(aid)
        assert row["mem_limit"] == "2g"
        with pytest.raises(ValueError):
            repo.update(aid, state="running")  # state changes go via set_state
