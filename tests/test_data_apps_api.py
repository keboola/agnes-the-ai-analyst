"""Tests for the control-plane REST API (`/api/data-apps/...`).

Follows the fixture idiom of ``tests/test_data_apps_git.py``'s
``data_apps_git_env`` — real user + PAT rows via the DuckDB repos, feature
flag flipped on in an ``instance.yaml`` overlay, a real ``TestClient(app)``.

``fake_runner``/``dead_runner`` monkeypatch the module-level
``app.api.data_apps._runner`` indirection (the documented seam) with a stub
recording ``up_calls``/``stop_calls``/``logs_calls`` or one that always
raises ``RunnerUnavailable``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid

import pytest
import yaml
from cryptography.fernet import Fernet

from src.data_apps.runner_client import RunnerError, RunnerUnavailable


def _auth(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}"}


@pytest.fixture
def api_env(e2e_env, monkeypatch):
    """Real user/token/group rows + TestClient(app), data_apps enabled."""
    from app.main import create_app
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    data_dir = e2e_env["data_dir"]
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())

    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    import app.instance_config as instance_config

    instance_config._instance_config = None

    conn = get_system_db()
    try:
        users = UserRepository(conn)
        users.create(id="owner1", email="owner@test.local", name="Owner")
        users.create(id="other1", email="other@test.local", name="Other")
        users.create(id="admin1", email="admin@test.local", name="Admin")

        ug = UserGroupsRepository(conn)
        ug.ensure_system("Admin", "system")
        ug.ensure_system("Everyone", "system")
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]

        ugm = UserGroupMembersRepository(conn)
        ugm.add_member("admin1", admin_gid, source="system_seed")

        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email in [
            ("owner1", "owner@test.local"),
            ("other1", "other@test.local"),
            ("admin1", "admin@test.local"),
        ]:
            tid = str(uuid.uuid4())
            jwt = create_access_token(uid, email, token_id=tid, typ="pat")
            token_repo.create(
                id=tid,
                user_id=uid,
                name=f"{uid}-pat",
                token_hash=hashlib.sha256(jwt.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=None,
            )
            pats[uid] = jwt
    finally:
        conn.close()

    app = create_app()
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return {
        "client": client,
        "owner_pat": pats["owner1"],
        "other_pat": pats["other1"],
        "admin_pat": pats["admin1"],
        "data_dir": data_dir,
    }


@pytest.fixture
def client_as_user(api_env):
    c = api_env["client"]
    headers = _auth(api_env["owner_pat"])

    class _Wrapped:
        def get(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.get(url, **kw)

        def post(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.post(url, **kw)

        def put(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.put(url, **kw)

        def delete(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.delete(url, **kw)

    return _Wrapped()


@pytest.fixture
def client_as_other_user(api_env):
    c = api_env["client"]
    headers = _auth(api_env["other_pat"])

    class _Wrapped:
        def get(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.get(url, **kw)

        def post(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.post(url, **kw)

        def put(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.put(url, **kw)

        def delete(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.delete(url, **kw)

    return _Wrapped()


@pytest.fixture
def admin_client(api_env):
    c = api_env["client"]
    headers = _auth(api_env["admin_pat"])

    class _Wrapped:
        def get(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.get(url, **kw)

        def post(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.post(url, **kw)

        def put(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.put(url, **kw)

        def delete(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.delete(url, **kw)

    return _Wrapped()


class _FakeRunner:
    def __init__(self):
        self.up_calls = []
        self.stop_calls = []
        self.logs_calls = []

    def up(self, slug, spec, config_json):
        self.up_calls.append((slug, spec, config_json))
        return {"container": "running", "ready": True}

    def stop(self, slug, mode="recreate"):
        self.stop_calls.append((slug, mode))
        return {"container": "stopped", "ready": False}

    def resume(self, slug):
        return {"container": "running", "ready": True}

    def status(self, slug):
        return {"container": "running", "ready": True}

    def logs(self, slug, tail=200):
        self.logs_calls.append((slug, tail))
        return "log line 1\nlog line 2\n"


class _DeadRunner:
    def up(self, slug, spec, config_json):
        raise RunnerUnavailable("connection refused")

    def stop(self, slug, mode="recreate"):
        raise RunnerUnavailable("connection refused")

    def resume(self, slug):
        raise RunnerUnavailable("connection refused")

    def status(self, slug):
        raise RunnerUnavailable("connection refused")

    def logs(self, slug, tail=200):
        raise RunnerUnavailable("connection refused")


@pytest.fixture
def fake_runner(monkeypatch):
    import app.api.data_apps as data_apps_api

    runner = _FakeRunner()
    monkeypatch.setattr(data_apps_api, "_runner", lambda: runner)
    return runner


@pytest.fixture
def dead_runner(monkeypatch):
    import app.api.data_apps as data_apps_api

    runner = _DeadRunner()
    monkeypatch.setattr(data_apps_api, "_runner", lambda: runner)
    return runner


def _seed_app_with_commit(data_dir, slug="sapp", owner_id="owner1"):
    """Register a `data_apps` row + materialize its bare repo with one
    commit on `main` — the shape `deploy` needs to succeed."""
    from src.data_apps.git_repos import init_app_repo
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(slug=slug, name=slug.upper(), owner_user_id=owner_id)
    finally:
        conn.close()

    repo_dir = init_app_repo(slug)
    work = data_dir / f"work-{slug}"
    subprocess.run(["git", "clone", str(repo_dir), str(work)], check=True, capture_output=True)
    (work / "app.py").write_text("print('hi')")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c1"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"], check=True, capture_output=True)


def _seed_empty_app(slug="empty1", owner_id="owner1"):
    from src.data_apps.git_repos import init_app_repo
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(slug=slug, name=slug.upper(), owner_user_id=owner_id)
    finally:
        conn.close()
    init_app_repo(slug)


@pytest.fixture
def seeded_repo_with_commit(api_env):
    _seed_app_with_commit(api_env["data_dir"], slug="sapp", owner_id="owner1")
    return "sapp"


@pytest.fixture
def running_idle_app(api_env):
    """A `running` app whose `last_request_at` is far in the past — should
    be picked up by `list_idle`."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="sapp", name="S", owner_user_id="owner1", idle_timeout_s=300)
        repo.set_state(app_id, "running")
        conn.execute(
            "UPDATE data_apps SET last_request_at = now() - INTERVAL 2 HOUR WHERE id = ?",
            [app_id],
        )
    finally:
        conn.close()
    return "sapp"


class TestCrud:
    def test_create_and_quota(self, client_as_user):
        for i in range(3):
            r = client_as_user.post("/api/data-apps", json={"slug": f"a{i}", "name": f"A{i}"})
            assert r.status_code == 201, r.text
        r = client_as_user.post("/api/data-apps", json={"slug": "a3", "name": "A3"})
        assert r.status_code == 403
        assert r.json()["detail"] == "app_quota_exceeded"

    def test_create_returns_git_url(self, client_as_user):
        r = client_as_user.post("/api/data-apps", json={"slug": "gitcheck", "name": "G"})
        assert r.status_code == 201
        body = r.json()
        assert body["slug"] == "gitcheck"
        assert "data-apps.git/gitcheck" in body["git_url"]
        assert "id" in body

    def test_slug_validation(self, client_as_user):
        r = client_as_user.post("/api/data-apps", json={"slug": "Bad_Slug", "name": "x"})
        assert r.status_code == 400

    def test_duplicate_slug_conflict(self, client_as_user):
        r1 = client_as_user.post("/api/data-apps", json={"slug": "dupe", "name": "One"})
        assert r1.status_code == 201
        r2 = client_as_user.post("/api/data-apps", json={"slug": "dupe", "name": "Two"})
        assert r2.status_code == 409

    def test_list_hides_secrets(self, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "sh", "name": "SH"})
        rows = client_as_user.get("/api/data-apps").json()
        assert rows
        row = next(r for r in rows if r["slug"] == "sh")
        assert "secrets_enc" not in row
        assert "service_token_id" not in row
        assert row["url"] == "/apps/sh/"

    def test_feature_disabled_404s(self, api_env, monkeypatch):
        import app.instance_config as instance_config

        original = instance_config._instance_config
        instance_config._instance_config = {**(original or {}), "data_apps": {"enabled": False}}
        try:
            c = api_env["client"]
            r = c.get("/api/data-apps", headers=_auth(api_env["owner_pat"]))
            assert r.status_code == 404
            assert r.json()["detail"] == "data_apps_disabled"
        finally:
            instance_config._instance_config = original


class TestDetailRbac:
    def test_owner_can_view(self, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "rbac1", "name": "R"})
        r = client_as_user.get("/api/data-apps/rbac1")
        assert r.status_code == 200

    def test_stranger_forbidden(self, client_as_user, client_as_other_user):
        client_as_user.post("/api/data-apps", json={"slug": "rbac2", "name": "R"})
        r = client_as_other_user.get("/api/data-apps/rbac2")
        assert r.status_code == 403

    def test_admin_can_view(self, client_as_user, admin_client):
        client_as_user.post("/api/data-apps", json={"slug": "rbac3", "name": "R"})
        r = admin_client.get("/api/data-apps/rbac3")
        assert r.status_code == 200

    def test_granted_group_can_view(self, client_as_user, client_as_other_user, api_env):
        client_as_user.post("/api/data-apps", json={"slug": "rbac4", "name": "R"})

        from src.db import get_system_db
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.resource_grants import ResourceGrantsRepository

        conn = get_system_db()
        try:
            gid = UserGroupsRepository(conn).create(name="Viewers", description="d")["id"]
            UserGroupMembersRepository(conn).add_member("other1", gid, source="admin")
            ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="rbac4")
        finally:
            conn.close()

        r = client_as_other_user.get("/api/data-apps/rbac4")
        assert r.status_code == 200

    def test_missing_app_404s(self, client_as_user):
        r = client_as_user.get("/api/data-apps/does-not-exist")
        assert r.status_code == 404


class TestDeploy:
    def test_deploy_happy_path(self, client_as_user, fake_runner, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 200, r.text
        assert fake_runner.up_calls

        slug, spec, config_json = fake_runner.up_calls[0]
        assert slug == "sapp"
        assert "dataApp" in config_json
        assert config_json["dataApp"]["git"]["repository"].startswith("http://app:8000/data-apps.git/")
        assert "secrets" in config_json["dataApp"]

        row = client_as_user.get("/api/data-apps/sapp").json()
        assert row["state"] == "running"
        assert row["deployed_sha"]

    def test_deploy_mints_and_stores_service_token(self, client_as_user, fake_runner, seeded_repo_with_commit):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
        finally:
            conn.close()
        assert row["service_token_id"]

    def test_deploy_forbidden_for_stranger(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_other_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 403

    def test_deploy_empty_repo_conflict(self, client_as_user, fake_runner, api_env):
        _seed_empty_app(slug="empty1", owner_id="owner1")
        r = client_as_user.post("/api/data-apps/empty1/deploy", json={})
        assert r.status_code == 409
        assert r.json()["detail"] == "deploy_empty_repo"

    def test_runner_down_sets_error(self, client_as_user, dead_runner, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 502
        assert r.json()["detail"] == "runner_unavailable"

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
        finally:
            conn.close()
        assert row["state"] == "error"

    def test_runner_error_sets_error(self, client_as_user, monkeypatch, seeded_repo_with_commit):
        import app.api.data_apps as data_apps_api

        class _ErrRunner:
            def up(self, slug, spec, config_json):
                raise RunnerError(500, "boom")

        monkeypatch.setattr(data_apps_api, "_runner", lambda: _ErrRunner())
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 502
        assert r.json()["detail"] == "runner_unavailable"


class TestStop:
    def test_stop_happy_path(self, client_as_user, fake_runner, seeded_repo_with_commit):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})
        r = client_as_user.post("/api/data-apps/sapp/stop")
        assert r.status_code == 200
        assert fake_runner.stop_calls
        row = client_as_user.get("/api/data-apps/sapp").json()
        assert row["state"] == "stopped"

    def test_stop_forbidden_for_stranger(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_other_user.post("/api/data-apps/sapp/stop")
        assert r.status_code == 403


class TestDelete:
    def test_delete_happy_path(self, client_as_user, fake_runner, seeded_repo_with_commit):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
        finally:
            conn.close()
        assert token_id

        r = client_as_user.delete("/api/data-apps/sapp")
        assert r.status_code == 204, r.text
        assert not r.content
        assert fake_runner.stop_calls

        conn = get_system_db()
        try:
            assert DataAppsRepository(conn).get_by_slug("sapp") is None
            from src.repositories.access_tokens import AccessTokenRepository

            token_row = AccessTokenRepository(conn).get_by_id(token_id)
            assert token_row["revoked_at"] is not None
        finally:
            conn.close()

    def test_delete_forbidden_for_stranger(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_other_user.delete("/api/data-apps/sapp")
        assert r.status_code == 403


class TestSecrets:
    def test_put_secrets_owner(self, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "sec1", "name": "S"})
        r = client_as_user.put("/api/data-apps/sec1/secrets", json={"secrets": {"API_KEY": "xyz"}})
        assert r.status_code == 200

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository
        from app.secrets_vault import decrypt_secret

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sec1")
        finally:
            conn.close()
        assert row["secrets_enc"]
        decrypted = json.loads(decrypt_secret(row["secrets_enc"].encode("ascii")))
        assert decrypted == {"API_KEY": "xyz"}

    def test_put_secrets_forbidden_for_stranger(self, client_as_user, client_as_other_user):
        client_as_user.post("/api/data-apps", json={"slug": "sec2", "name": "S"})
        r = client_as_other_user.put("/api/data-apps/sec2/secrets", json={"secrets": {"K": "v"}})
        assert r.status_code == 403

    def test_deploy_includes_secrets_in_config_json(self, client_as_user, fake_runner, seeded_repo_with_commit):
        client_as_user.put("/api/data-apps/sapp/secrets", json={"secrets": {"MY_SECRET": "hunter2"}})
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        assert fake_runner.up_calls
        _, _, config_json = fake_runner.up_calls[0]
        assert config_json["dataApp"]["secrets"]["#MY_SECRET"] == "hunter2"


class TestLogs:
    def test_logs_owner_only(self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit):
        r = client_as_user.get("/api/data-apps/sapp/logs?tail=50")
        assert r.status_code == 200
        assert fake_runner.logs_calls == [("sapp", 50)]

        r2 = client_as_other_user.get("/api/data-apps/sapp/logs")
        assert r2.status_code == 403


class TestReadiness:
    def test_readiness_for_granted_viewer(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit, api_env
    ):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        from src.db import get_system_db
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.resource_grants import ResourceGrantsRepository

        conn = get_system_db()
        try:
            gid = UserGroupsRepository(conn).create(name="ReadyViewers", description="d")["id"]
            UserGroupMembersRepository(conn).add_member("other1", gid, source="admin")
            ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="sapp")
        finally:
            conn.close()

        r = client_as_other_user.get("/api/data-apps/sapp/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "running"
        assert body["ready"] is True

    def test_readiness_forbidden_for_stranger(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_other_user.get("/api/data-apps/sapp/readiness")
        assert r.status_code == 403

    def test_readiness_created_state_not_ready(self, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "notready", "name": "N"})
        r = client_as_user.get("/api/data-apps/notready/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "created"
        assert body["ready"] is False


class TestReap:
    def test_reap_idle(self, admin_client, fake_runner, running_idle_app):
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == ["sapp"]
        assert fake_runner.stop_calls == [("sapp", "recreate")]

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
        finally:
            conn.close()
        assert row["state"] == "sleeping"

    def test_reap_idle_requires_admin(self, client_as_user, fake_runner, running_idle_app):
        r = client_as_user.post("/api/data-apps/reap-idle")
        assert r.status_code == 403

    def test_reap_idle_skips_recently_active(self, admin_client, fake_runner, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "active1", "name": "A"})
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == []
