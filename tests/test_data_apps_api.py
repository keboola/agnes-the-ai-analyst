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
        self._status = {"container": "running", "ready": True}

    def up(self, slug, spec, config_json):
        self.up_calls.append((slug, spec, config_json))
        return {"container": "running", "ready": True}

    def stop(self, slug, mode="recreate"):
        self.stop_calls.append((slug, mode))
        return {"container": "stopped", "ready": False}

    def resume(self, slug):
        return {"container": "running", "ready": True}

    def status(self, slug):
        return self._status

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


def _seed_external_app(slug="eapp", owner_id="owner1", repo_url="https://example.com/org/app.git", repo_branch="main"):
    """Register a `repo_mode="external"` app row — no internal bare repo is
    ever created for these (`init_app_repo` is internal-only at create), so
    deploy must never touch `fast_forward_live`."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(
            slug=slug,
            name=slug.upper(),
            owner_user_id=owner_id,
            repo_mode="external",
            repo_url=repo_url,
            repo_branch=repo_branch,
        )
    finally:
        conn.close()


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


@pytest.fixture
def running_active_app(api_env):
    """A `running` app whose `last_request_at` is recent (now) — mirrors
    `running_idle_app` but must NOT be picked up by the reap-idle sweep."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="active2", name="Active2", owner_user_id="owner1", idle_timeout_s=300)
        repo.set_state(app_id, "running")
        conn.execute("UPDATE data_apps SET last_request_at = now() WHERE id = ?", [app_id])
    finally:
        conn.close()
    return "active2"


@pytest.fixture
def running_idle_app_with_token(api_env):
    """Like `running_idle_app`, but with a real service token attached —
    proves reap-idle's SLEEP transition must NOT revoke it (unlike explicit
    stop/delete): a sleeping app needs its token to wake later."""
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.data_apps import DataAppsRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="idletok", name="IdleTok", owner_user_id="owner1", idle_timeout_s=300)
        repo.set_state(app_id, "running")
        conn.execute(
            "UPDATE data_apps SET last_request_at = now() - INTERVAL 2 HOUR WHERE id = ?",
            [app_id],
        )
        tid = str(uuid.uuid4())
        jwt_token = create_access_token("owner1", "owner@test.local", token_id=tid, typ="pat")
        AccessTokenRepository(conn).create(
            id=tid,
            user_id="owner1",
            name="data-app:idletok",
            token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=None,
        )
        repo.update(app_id, service_token_id=tid)
    finally:
        conn.close()
    return "idletok", tid


@pytest.fixture
def stale_deploying_app(api_env):
    """A `deploying` app whose `updated_at` is far in the past — a wake or
    operator-deploy that never finished. Should be recovered (-> `error`)
    by the reap-idle sweep's stale-deploying scan."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="stuck1", name="Stuck1", owner_user_id="owner1")
        repo.set_state(app_id, "deploying", "waking")
        conn.execute(
            "UPDATE data_apps SET updated_at = now() - INTERVAL 20 MINUTE WHERE id = ?",
            [app_id],
        )
    finally:
        conn.close()
    return "stuck1"


@pytest.fixture
def fresh_deploying_app(api_env):
    """A `deploying` app whose `updated_at` is recent — mirrors
    `stale_deploying_app` but must NOT be touched by the sweep (a wake that's
    genuinely still in flight)."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="fresh1", name="Fresh1", owner_user_id="owner1")
        repo.set_state(app_id, "deploying", "waking")
    finally:
        conn.close()
    return "fresh1"


class TestCrud:
    def test_create_and_quota(self, client_as_user):
        for i in range(3):
            r = client_as_user.post("/api/data-apps", json={"slug": f"a{i}", "name": f"A{i}"})
            assert r.status_code == 201, r.text
        r = client_as_user.post("/api/data-apps", json={"slug": "a3", "name": "A3"})
        assert r.status_code == 403
        assert r.json()["detail"] == "app_quota_exceeded"

    def test_create_quota_race_lease_conflict(self, client_as_user, monkeypatch):
        """When the create-lease can't be acquired for a non-admin caller
        (already held by a concurrent request for the same user), create
        is rejected rather than racing the count-then-create quota check."""
        import app.coordination.factory as coord_factory

        class _AlwaysBusyBackend:
            def lease_acquire(self, name, holder_id, *, ttl_s):
                return False

            def lease_release(self, name, holder_id):
                pass

        monkeypatch.setattr(coord_factory, "coordination", lambda: _AlwaysBusyBackend())
        r = client_as_user.post("/api/data-apps", json={"slug": "racy1", "name": "R"})
        assert r.status_code == 409
        assert r.json()["detail"] == "create_in_progress"

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

    def test_reserved_slug_rejected(self, client_as_user):
        """ "detail" is a literal path segment the web UI's
        `GET /apps/detail/{slug}` route owns (app/web/router.py's
        `apps_web_router`) — a data app named "detail" would have its own
        sub-paths swallowed by that route instead of reaching the proxy.
        Rejected at create time (`src.data_apps.spec.RESERVED_SLUGS`) so the
        collision can never happen, rather than relying on route-registration
        order alone."""
        r = client_as_user.post("/api/data-apps", json={"slug": "detail", "name": "x"})
        assert r.status_code == 400
        assert r.json()["detail"] == "reserved_slug"

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

    def test_deploy_runner_down_rolls_back_new_token(
        self, client_as_user, fake_runner, seeded_repo_with_commit, monkeypatch
    ):
        """A failed redeploy must not leave a dangling, unused service PAT
        live, and must not clobber the still-working previous token."""
        r1 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r1.status_code == 200

        from src.db import get_system_db
        from src.repositories.access_tokens import AccessTokenRepository
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            old_token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
            tokens_before = {t["id"] for t in AccessTokenRepository(conn).list_for_user("owner1")}
        finally:
            conn.close()
        assert old_token_id

        import app.api.data_apps as data_apps_api

        monkeypatch.setattr(data_apps_api, "_runner", lambda: _DeadRunner())
        r2 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r2.status_code == 502
        assert r2.json()["detail"] == "runner_unavailable"

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
            tokens_after = AccessTokenRepository(conn).list_for_user("owner1")
            old_token_row = AccessTokenRepository(conn).get_by_id(old_token_id)
        finally:
            conn.close()

        # Row's service_token_id is restored to the pre-attempt (old) value.
        assert row["service_token_id"] == old_token_id
        # The previously-working token must survive a failed redeploy —
        # a sleeping-but-deployed app must still be able to wake with it.
        assert old_token_row["revoked_at"] is None

        # Exactly one new token was minted during the failed attempt, and
        # it must be revoked — it was never handed to a container.
        new_token_ids = {t["id"] for t in tokens_after} - tokens_before
        assert len(new_token_ids) == 1
        conn = get_system_db()
        try:
            new_token_row = AccessTokenRepository(conn).get_by_id(next(iter(new_token_ids)))
        finally:
            conn.close()
        assert new_token_row["revoked_at"] is not None

    def test_deploy_redeploy_revokes_old_stores_new(self, client_as_user, fake_runner, seeded_repo_with_commit):
        r1 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r1.status_code == 200

        from src.db import get_system_db
        from src.repositories.access_tokens import AccessTokenRepository
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            old_token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
        finally:
            conn.close()

        r2 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r2.status_code == 200

        conn = get_system_db()
        try:
            new_token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
            old_token_row = AccessTokenRepository(conn).get_by_id(old_token_id)
        finally:
            conn.close()

        assert new_token_id != old_token_id
        assert old_token_row["revoked_at"] is not None

    def test_deploy_external_repo_happy_path(self, client_as_user, fake_runner, api_env):
        """External-repo apps never get an internal bare repo (`init_app_repo`
        is internal-only at create), so deploy must not go through
        `fast_forward_live` — the runtime clones HEAD of `repo_branch` at
        boot instead of a pinned internal sha."""
        _seed_external_app(
            slug="eapp", owner_id="owner1", repo_url="https://example.com/org/app.git", repo_branch="main"
        )
        r = client_as_user.post("/api/data-apps/eapp/deploy", json={})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "running"
        assert r.json()["deployed_sha"] == ""

        assert fake_runner.up_calls
        slug, spec, config_json = fake_runner.up_calls[0]
        assert slug == "eapp"
        git = config_json["dataApp"]["git"]
        assert git["repository"] == "https://example.com/org/app.git"
        assert git["branch"] == "main"
        assert "username" not in git
        assert "#password" not in git

        # Service token is still minted for the platform API even though no
        # internal git credential is handed to the container.
        assert "AGNES_TOKEN" in config_json["dataApp"]["secrets"]

        row = client_as_user.get("/api/data-apps/eapp").json()
        assert row["state"] == "running"
        assert row["deployed_sha"] == ""

    def test_deploy_external_repo_with_sha_rejected(self, client_as_user, fake_runner, api_env):
        _seed_external_app(slug="eapp2", owner_id="owner1")
        r = client_as_user.post("/api/data-apps/eapp2/deploy", json={"sha": "abc"})
        assert r.status_code == 400
        assert r.json()["detail"] == "external_repo_sha_unsupported"
        assert not fake_runner.up_calls


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

    def test_stop_revokes_service_token(self, client_as_user, fake_runner, seeded_repo_with_commit):
        """Spec §8/§10: stop must revoke the app's service token (only sleep
        via reap-idle keeps it live, for wake) — see TestReap's
        `test_reap_idle_sleep_does_not_revoke_token` for the contrast."""
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
        finally:
            conn.close()
        assert token_id

        r = client_as_user.post("/api/data-apps/sapp/stop")
        assert r.status_code == 200

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
            from src.repositories.access_tokens import AccessTokenRepository

            token_row = AccessTokenRepository(conn).get_by_id(token_id)
        finally:
            conn.close()
        assert row["service_token_id"] == ""
        assert token_row["revoked_at"] is not None


class TestOpLeaseSerialization:
    """`dataapp:op:{slug}` — the lease shared by `deploy_data_app`,
    `stop_data_app`, and the ingress proxy's `_trigger_wake`
    (`app/api/data_apps_proxy.py`) so at most one runner-mutating
    operation is ever in flight per app. Regression coverage for the race
    flagged in PR #1002's review: `deploy_data_app`/`stop_data_app` used
    to call the runner directly with no lease at all, so a manual deploy
    could race an in-flight auto-wake for the same slug and both call
    `runner.up()` concurrently.
    """

    def test_deploy_409s_when_op_lease_held_elsewhere(self, client_as_user, fake_runner, seeded_repo_with_commit):
        """Simulates another in-flight operation (e.g. the proxy's
        `_trigger_wake`, which never explicitly releases the lease — see
        that function's docstring) already holding the lease for this
        slug. `deploy_data_app` must not proceed to `redeploy_current`."""
        from app.api.data_apps import release_op_lease, try_acquire_op_lease

        acquired, holder = try_acquire_op_lease("sapp")
        assert acquired
        try:
            r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
            assert r.status_code == 409
            assert r.json()["detail"] == "operation_in_progress"
            assert not fake_runner.up_calls
        finally:
            release_op_lease("sapp", holder)

    def test_stop_409s_when_op_lease_held_elsewhere(self, client_as_user, fake_runner, seeded_repo_with_commit):
        from app.api.data_apps import release_op_lease, try_acquire_op_lease

        client_as_user.post("/api/data-apps/sapp/deploy", json={})
        fake_runner.up_calls.clear()

        acquired, holder = try_acquire_op_lease("sapp")
        assert acquired
        try:
            r = client_as_user.post("/api/data-apps/sapp/stop")
            assert r.status_code == 409
            assert r.json()["detail"] == "operation_in_progress"
            assert not fake_runner.stop_calls
        finally:
            release_op_lease("sapp", holder)

    def test_concurrent_deploy_calls_never_overlap_inside_runner_up(
        self, client_as_user, monkeypatch, seeded_repo_with_commit
    ):
        """The actual race this lease closes: two `up()` invocations for the
        same slug running at once (`services/apps_runner/api.py::up()` does
        an unlocked check-then-act — get old container, remove, run new).
        Runs two real concurrent `deploy` requests through a runner stub
        that blocks inside `up()` long enough for a second call to exhaust
        `require_op_lease`'s retries — proving the lease actually serializes
        the two HTTP requests rather than just rejecting a pre-set-up lease
        in isolation (see the two tests above)."""
        import threading
        import time

        import app.api.data_apps as data_apps_api

        inside = {"current": 0, "peak": 0}
        lock = threading.Lock()
        first_call_entered = threading.Event()

        class _BlockingRunner:
            def up(self, slug, spec, config_json):
                with lock:
                    inside["current"] += 1
                    inside["peak"] = max(inside["peak"], inside["current"])
                first_call_entered.set()
                try:
                    # Long enough that the second call's retry-then-409
                    # window (`_OP_LEASE_RETRIES` * `_OP_LEASE_RETRY_DELAY_S`
                    # ~= 0.2s) fully elapses while this call is still inside.
                    time.sleep(0.5)
                    return {"container": "running", "ready": True}
                finally:
                    with lock:
                        inside["current"] -= 1

        monkeypatch.setattr(data_apps_api, "_runner", lambda: _BlockingRunner())

        results = []

        def _deploy():
            results.append(client_as_user.post("/api/data-apps/sapp/deploy", json={}))

        t1 = threading.Thread(target=_deploy)
        t1.start()
        assert first_call_entered.wait(timeout=5), "first deploy never reached runner.up()"
        r2 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        t1.join(timeout=5)

        assert inside["peak"] == 1, (
            f"two deploys called runner.up() concurrently for the same slug (peak={inside['peak']})"
        )
        assert len(results) == 1
        r1 = results[0]
        # Whichever request actually held the lease first succeeds; the
        # other is rejected outright — never both "in progress" at once,
        # never both succeeding.
        statuses = sorted([r1.status_code, r2.status_code])
        assert statuses == [200, 409], (r1.status_code, r2.status_code)
        if r2.status_code == 409:
            assert r2.json()["detail"] == "operation_in_progress"
        else:
            assert r1.json()["detail"] == "operation_in_progress"


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

    def test_delete_removes_config_dir(self, client_as_user, fake_runner, seeded_repo_with_commit, api_env):
        """The leftover `config.json` under `${DATA_DIR}/apps/<slug>` holds a
        now-revoked JWT — best-effort hygiene cleanup on delete, distinct
        from the git repo directory (which is intentionally kept)."""
        config_dir = api_env["data_dir"] / "apps" / "sapp"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text("{}")

        r = client_as_user.delete("/api/data-apps/sapp")
        assert r.status_code == 204

        assert not config_dir.exists()


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

    def test_reap_idle_sleep_does_not_revoke_token(self, admin_client, fake_runner, running_idle_app_with_token):
        """Contrast with `TestStop.test_stop_revokes_service_token`: only
        explicit stop/delete revoke — the idle sweep's sleep transition must
        leave a sleeping app's service token live so it can wake later."""
        slug, token_id = running_idle_app_with_token
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == [slug]

        from src.db import get_system_db
        from src.repositories.access_tokens import AccessTokenRepository
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug(slug)
            token_row = AccessTokenRepository(conn).get_by_id(token_id)
        finally:
            conn.close()
        assert row["state"] == "sleeping"
        assert row["service_token_id"] == token_id
        assert token_row["revoked_at"] is None

    def test_reap_idle_requires_admin(self, client_as_user, fake_runner, running_idle_app):
        r = client_as_user.post("/api/data-apps/reap-idle")
        assert r.status_code == 403

    def test_reap_idle_skips_never_deployed_app(self, admin_client, fake_runner, client_as_user):
        """A freshly-created app (state='created', never deployed) is not
        even a reap candidate — `list(state='running')` filters it out
        before the idle-threshold check ever runs. (Previously misnamed
        `test_reap_idle_skips_recently_active` — it never actually
        exercised a 'running' app; see `test_reap_idle_skips_recently_active_running_app`
        below for that case.)"""
        client_as_user.post("/api/data-apps", json={"slug": "active1", "name": "A"})
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == []

    def test_reap_idle_skips_recently_active_running_app(self, admin_client, fake_runner, running_active_app):
        """A `running` app whose `last_request_at` is recent must be left
        alone — reap-idle's per-app `idle_timeout_s` check must not fire
        just because the app happens to be in scanning scope."""
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == []
        assert fake_runner.stop_calls == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("active2")
        finally:
            conn.close()
        assert row["state"] == "running"

    def test_reap_idle_recovers_stale_deploying_app_when_runner_ready(
        self, admin_client, fake_runner, stale_deploying_app
    ):
        """A `deploying` row stuck past `_DEPLOY_STALE_TIMEOUT_S` is checked
        against the runner before being declared dead: if the runner reports
        the container is actually up and ready (a `readiness` poll that
        never happened to fire, say), the row is recovered to `running`
        rather than errored out from under a perfectly good deploy."""
        fake_runner._status = {"container": "running", "ready": True}
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        body = r.json()
        assert body["recovered"] == ["stuck1"]
        assert body["timed_out"] == []
        assert body["reaped"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("stuck1")
        finally:
            conn.close()
        assert row["state"] == "running"

    def test_reap_idle_recovers_stale_deploying_app_when_runner_not_ready(
        self, admin_client, fake_runner, stale_deploying_app
    ):
        """Same stale row, but the runner reports the container absent/not
        ready — genuinely dead, so it's flipped to `error` (not left wedged
        forever), reported separately from `reaped`/`recovered`."""
        fake_runner._status = {"container": "absent", "ready": False}
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        body = r.json()
        assert body["timed_out"] == ["stuck1"]
        assert body["recovered"] == []
        assert body["reaped"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("stuck1")
        finally:
            conn.close()
        assert row["state"] == "error"
        assert row["state_detail"] == "wake/deploy timed out"

    def test_reap_idle_leaves_fresh_deploying_app_untouched(self, admin_client, fake_runner, fresh_deploying_app):
        """A `deploying` row that's genuinely still in flight (recent
        `updated_at`) must not be touched by the stale-deploying scan."""
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["timed_out"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("fresh1")
        finally:
            conn.close()
        assert row["state"] == "deploying"
