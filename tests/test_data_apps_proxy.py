"""Tests for the auth-gated ingress proxy (`/apps/{slug}/...`) — Task 8:
wake-on-request, the holding page, touch-debounce, hop-by-hop header
hygiene, subdomain-host rewrite, and the WS bridge's auth-reject path.

Follows the `api_env`-fixture idiom of `tests/test_data_apps_api.py`: real
user/token rows via the DuckDB repos, `data_apps.enabled` flipped on in an
`instance.yaml` overlay, a real `TestClient(app)`.

No `respx` in dev deps (`grep respx pyproject.toml` came up empty) — the
upstream fake monkeypatches the module-level `_upstream_client()` seam
(`app.api.data_apps_proxy._upstream_client`) to return an
`httpx.AsyncClient(transport=httpx.MockTransport(handler))`, wrapped in a
respx-like `.calls[i].request` shape for readability.
"""

from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet
from starlette.websockets import WebSocketDisconnect

from src.data_apps.runner_client import RunnerUnavailable


def _auth(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}"}


@pytest.fixture(autouse=True)
def _inline_spawn_wake(monkeypatch):
    """`_spawn_wake` backgrounds the redeploy in production (fire-and-forget
    `asyncio.create_task`) so a wake-triggering request never blocks on it.
    That's untestable-by-default (nothing guarantees the background task
    has run by the time a test asserts on it right after the response
    returns) — so every test EXCEPT the one that specifically asserts the
    non-blocking behavior (`test_sleeping_recreate_wake_does_not_block_response`,
    which overrides this patch itself) gets `_spawn_wake` replaced with a
    version that `await`s `_run_wake_fn` directly, making its effects
    (`fake_runner.up_calls`, the row's new state) observable synchronously.
    """
    import app.api.data_apps_proxy as proxy_api

    async def _inline(fn, row):
        await proxy_api._run_wake_fn(fn, row)

    monkeypatch.setattr(proxy_api, "_spawn_wake", _inline)


@pytest.fixture(autouse=True)
def _reset_coordination():
    """The `memory` coordination backend is a process-wide singleton
    (`app.coordination.factory._instance`) with no autouse reset in
    `tests/conftest.py` — without this, a wake/touch lease acquired by one
    test (many of this module's fixtures reuse slug `"s"`, so they'd share
    lease names like `dataapp:wake:s`) stays held for its full TTL and
    leaks into the next test on the same xdist worker."""
    from app.coordination.factory import reset_coordination_for_tests

    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


@pytest.fixture
def proxy_env(e2e_env, monkeypatch):
    """Real user/token rows + TestClient(app), data_apps enabled."""
    from app.main import create_app
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.users import UserRepository

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

        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email in [("owner1", "owner@test.local"), ("other1", "other@test.local")]:
            tid = str(uuid.uuid4())
            jwt_token = create_access_token(uid, email, token_id=tid, typ="pat")
            token_repo.create(
                id=tid,
                user_id=uid,
                name=f"{uid}-pat",
                token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=None,
            )
            pats[uid] = jwt_token
    finally:
        conn.close()

    app = create_app()
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return {"client": client, "owner_pat": pats["owner1"], "other_pat": pats["other1"], "data_dir": data_dir}


def _set_data_apps_config(data_dir, **overrides) -> None:
    """Overlay `instance.yaml`'s `data_apps:` block (merged with `enabled: True`)
    and drop the cached instance_config singleton so the next read picks it up."""
    import app.instance_config as instance_config

    state = data_dir / "state"
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True, **overrides}}))
    instance_config._instance_config = None


class _AuthedClient:
    """Thin TestClient wrapper that injects a bearer token while still
    letting individual calls pass/override their own headers (e.g. Accept,
    Host)."""

    def __init__(self, client, pat: str):
        self._client = client
        self._auth_headers = {"Authorization": f"Bearer {pat}"}

    def _merge(self, kw: dict) -> dict:
        headers = dict(self._auth_headers)
        headers.update(kw.pop("headers", None) or {})
        kw["headers"] = headers
        return kw

    def get(self, url, **kw):
        return self._client.get(url, **self._merge(kw))

    def post(self, url, **kw):
        return self._client.post(url, **self._merge(kw))

    def websocket_connect(self, url, **kw):
        return self._client.websocket_connect(url, **self._merge(kw))


@pytest.fixture
def client_granted(proxy_env):
    return _AuthedClient(proxy_env["client"], proxy_env["owner_pat"])


@pytest.fixture
def client_stranger(proxy_env):
    return _AuthedClient(proxy_env["client"], proxy_env["other_pat"])


class _FakeRunner:
    def __init__(self):
        self.up_calls: list[tuple] = []
        self.resume_calls: list[str] = []
        self.stop_calls: list[tuple] = []
        self._status: dict = {"container": "running", "ready": True}

    def up(self, slug, spec, config_json):
        self.up_calls.append((slug, spec, config_json))
        return {"container": "running", "ready": True}

    def stop(self, slug, mode="recreate"):
        self.stop_calls.append((slug, mode))
        return {"container": "stopped", "ready": False}

    def resume(self, slug):
        self.resume_calls.append(slug)
        return {"container": "running", "ready": True}

    def status(self, slug):
        return self._status

    def logs(self, slug, tail=200):
        return ""


class _DeadRunner:
    def up(self, slug, spec, config_json):
        raise RunnerUnavailable("connection refused")

    def resume(self, slug):
        raise RunnerUnavailable("connection refused")

    def status(self, slug):
        raise RunnerUnavailable("connection refused")

    def stop(self, slug, mode="recreate"):
        raise RunnerUnavailable("connection refused")

    def logs(self, slug, tail=200):
        raise RunnerUnavailable("connection refused")


@pytest.fixture
def fake_runner(monkeypatch):
    """Patches BOTH `app.api.data_apps._runner` (used by `redeploy_current`'s
    `up()` call) and `app.api.data_apps_proxy._runner` (used directly by
    `_trigger_wake`'s `resume()` call) to the SAME stub instance — the two
    modules keep independent monkeypatch seams (mirrors the rest of the
    codebase's per-module `_runner()` indirection convention) but a caller
    wants one shared fake to observe both call sites together.
    """
    import app.api.data_apps as data_apps_api
    import app.api.data_apps_proxy as proxy_api

    runner = _FakeRunner()
    monkeypatch.setattr(data_apps_api, "_runner", lambda: runner)
    monkeypatch.setattr(proxy_api, "_runner", lambda: runner)
    return runner


@pytest.fixture
def dead_runner(monkeypatch):
    import app.api.data_apps as data_apps_api
    import app.api.data_apps_proxy as proxy_api

    runner = _DeadRunner()
    monkeypatch.setattr(data_apps_api, "_runner", lambda: runner)
    monkeypatch.setattr(proxy_api, "_runner", lambda: runner)
    return runner


def _create_app_row(slug="s", owner_id="owner1", state="running", sleep_mode="recreate"):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug=slug, name=slug.upper(), owner_user_id=owner_id, sleep_mode=sleep_mode)
        if state != "created":
            repo.set_state(app_id, state)
    finally:
        conn.close()
    return app_id


@pytest.fixture
def running_app(proxy_env):
    _create_app_row(slug="s", state="running")
    return "s"


@pytest.fixture
def sleeping_app(proxy_env):
    _create_app_row(slug="s", state="sleeping", sleep_mode="recreate")
    return "s"


class _Call:
    def __init__(self, request):
        self.request = request


class _Recorder:
    def __init__(self):
        self.calls: list[_Call] = []


class _AsyncByteStream(httpx.AsyncByteStream):
    """Real (not pre-materialized) async stream — `httpx.Response(...,
    text=...)` marks the response as already fully read
    (`is_stream_consumed=True`), which makes `_proxy`'s `resp.aiter_raw()`
    raise `StreamConsumed` on its very first read. The proxy implementation
    streams a real upstream response exactly once; this stream shape is
    what makes the MockTransport-backed fake behave like one."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


@pytest.fixture
def respx_upstream(monkeypatch):
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.calls.append(_Call(request))
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=_AsyncByteStream([b"hello from app"]),
        )

    def _fake_upstream_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    import app.api.data_apps_proxy as proxy_api

    monkeypatch.setattr(proxy_api, "_upstream_client", _fake_upstream_client)
    return recorder


# ---------------------------------------------------------------------------
# HTTP proxy — state routing, wake, debounce, header hygiene
# ---------------------------------------------------------------------------


def test_running_app_is_proxied(client_granted, fake_runner, respx_upstream, running_app):
    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 200, r.text
    assert r.text == "hello from app"
    assert respx_upstream.calls[0].request.headers["x-forwarded-prefix"] == "/apps/s"


def test_proxy_strips_hop_by_hop_headers(client_granted, fake_runner, respx_upstream, running_app):
    r = client_granted.get("/apps/s/hello", headers={"connection": "close", "x-custom": "kept"})
    assert r.status_code == 200
    sent = respx_upstream.calls[0].request.headers
    # httpx always emits its OWN `Connection` header on an outgoing request
    # (an HTTP protocol necessity, added by httpx's client defaults
    # regardless of what we forward) — the hygiene guarantee under test is
    # that the CALLER's hop-by-hop value never rides through unfiltered.
    assert sent["connection"] != "close"
    assert sent["x-custom"] == "kept"


def test_proxy_strips_caller_credentials(client_granted, fake_runner, respx_upstream, running_app):
    """Security: the caller's Agnes credentials (`Authorization` — already
    injected on every `client_granted` call — and `Cookie`, set explicitly
    here) must never reach the proxied data-app container. Distinct from
    the hop-by-hop test above: these aren't protocol headers, they're the
    caller's own auth material."""
    r = client_granted.get("/apps/s/hello", headers={"cookie": "access_token=whatever; other=1"})
    assert r.status_code == 200
    sent = respx_upstream.calls[0].request.headers
    assert "authorization" not in sent
    assert "cookie" not in sent


def test_sleeping_app_returns_holding_page_and_wakes(client_granted, fake_runner, sleeping_app):
    r = client_granted.get("/apps/s/", headers={"accept": "text/html"})
    assert r.status_code == 503
    assert "waking" in r.text.lower()
    assert fake_runner.up_calls  # wake fired exactly once


def test_sleeping_app_json_accept(client_granted, fake_runner, sleeping_app):
    r = client_granted.get("/apps/s/", headers={"accept": "application/json"})
    assert r.status_code == 503
    assert r.json()["status"] == "waking"


def test_sleeping_app_wake_fires_exactly_once_under_repeat_requests(client_granted, fake_runner, sleeping_app):
    """Second request lands while state is already 'deploying' (the wake
    lease + state flip from the first request) — must not fire a second
    redeploy."""
    r1 = client_granted.get("/apps/s/", headers={"accept": "application/json"})
    assert r1.status_code == 503
    r2 = client_granted.get("/apps/s/", headers={"accept": "application/json"})
    assert r2.status_code == 503
    assert len(fake_runner.up_calls) == 1


def test_sleeping_recreate_wake_does_not_block_response(client_granted, fake_runner, sleeping_app, monkeypatch):
    """Overrides the `_inline_spawn_wake` autouse fixture with a no-op
    recorder that never actually runs `fn` — proving the PRODUCTION
    `_spawn_wake` call site is genuinely fire-and-forget (the holding page
    response arrives regardless of how long the real redeploy would take),
    not just fast in this test suite because the fake runner is fast."""
    import app.api.data_apps_proxy as proxy_api

    calls = []

    async def _recorder(fn, row):
        calls.append((fn, row))
        # Deliberately does NOT call `fn` — if the handler awaited this
        # coroutine's *effect* rather than just scheduling it, a `fn` that
        # never resolves would hang the request forever. It doesn't hang,
        # which is exactly what this test is checking.

    monkeypatch.setattr(proxy_api, "_spawn_wake", _recorder)

    r = client_granted.get("/apps/s/", headers={"accept": "application/json"})
    assert r.status_code == 503
    assert r.json()["status"] == "waking"
    assert len(calls) == 1
    fn, row = calls[0]
    assert fn is proxy_api.redeploy_current
    assert row["slug"] == "s"
    assert not fake_runner.up_calls  # the recorder never actually ran fn


def test_sleeping_pause_mode_resumes_and_sets_running(client_granted, fake_runner):
    _create_app_row(slug="p", state="sleeping", sleep_mode="pause")
    r = client_granted.get("/apps/p/", headers={"accept": "application/json"})
    assert r.status_code == 503
    assert fake_runner.resume_calls == ["p"]
    assert not fake_runner.up_calls  # pause mode never redeploys

    from src.repositories import data_apps_repo

    row = data_apps_repo().get_by_slug("p")
    assert row["state"] == "running"


def test_stranger_gets_403(client_stranger, running_app):
    assert client_stranger.get("/apps/s/").status_code == 403


def test_missing_app_404s(client_granted):
    assert client_granted.get("/apps/does-not-exist/").status_code == 404


def test_touch_debounced(client_granted, running_app, respx_upstream):
    from src.repositories import data_apps_repo

    client_granted.get("/apps/s/")
    first = data_apps_repo().get_by_slug("s")["last_request_at"]
    client_granted.get("/apps/s/")
    assert data_apps_repo().get_by_slug("s")["last_request_at"] == first


def test_stopped_app_no_auto_wake_json(client_granted, fake_runner):
    _create_app_row(slug="st", state="stopped")
    r = client_granted.get("/apps/st/", headers={"accept": "application/json"})
    assert r.status_code == 409
    assert r.json()["detail"] == "app_not_running"
    assert not fake_runner.up_calls
    assert not fake_runner.resume_calls


def test_stopped_app_holding_page_html(client_granted, fake_runner):
    _create_app_row(slug="st2", state="stopped")
    r = client_granted.get("/apps/st2/", headers={"accept": "text/html"})
    assert r.status_code == 409
    assert "stopped" in r.text.lower()
    assert not fake_runner.up_calls


def test_created_app_no_auto_wake(client_granted, fake_runner):
    _create_app_row(slug="cr", state="created")
    r = client_granted.get("/apps/cr/", headers={"accept": "application/json"})
    assert r.status_code == 409
    assert r.json()["detail"] == "app_not_running"
    assert not fake_runner.up_calls


def test_error_state_returns_409_with_detail(client_granted, fake_runner):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="err1", name="ERR1", owner_user_id="owner1")
        repo.set_state(app_id, "error", "boom")
    finally:
        conn.close()

    r = client_granted.get("/apps/err1/")
    assert r.status_code == 409
    assert r.json()["state_detail"] == "boom"


def test_running_app_upstream_unreachable_sets_error(client_granted, fake_runner, monkeypatch, running_app):
    import app.api.data_apps_proxy as proxy_api

    def _broken_client():
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(proxy_api, "_upstream_client", _broken_client)

    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 502

    from src.repositories import data_apps_repo

    assert data_apps_repo().get_by_slug("s")["state"] == "error"


def test_get_apps_slug_redirects_to_trailing_slash(client_granted, running_app):
    r = client_granted.get("/apps/s", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/apps/s/"


def test_subdomain_host_rewrite(client_granted, running_app, respx_upstream, proxy_env):
    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com")
    r = client_granted.get("/", headers={"host": "s.apps.example.com"})
    assert r.status_code == 200  # reached the proxy handler for slug s


# ---------------------------------------------------------------------------
# readiness flip: deploying -> running once the runner reports ready
# ---------------------------------------------------------------------------


def test_readiness_flips_deploying_to_running(client_granted, fake_runner):
    from src.repositories import data_apps_repo

    _create_app_row(slug="dep", state="deploying")
    fake_runner._status = {"container": "running", "ready": True}

    r = client_granted.get("/api/data-apps/dep/readiness")
    assert r.status_code == 200
    assert r.json() == {"state": "running", "ready": True}
    assert data_apps_repo().get_by_slug("dep")["state"] == "running"


def test_readiness_stays_deploying_when_not_ready(client_granted, fake_runner):
    from src.repositories import data_apps_repo

    _create_app_row(slug="dep2", state="deploying")
    fake_runner._status = {"container": "starting", "ready": False}

    r = client_granted.get("/api/data-apps/dep2/readiness")
    assert r.status_code == 200
    assert r.json() == {"state": "deploying", "ready": False}
    assert data_apps_repo().get_by_slug("dep2")["state"] == "deploying"


# ---------------------------------------------------------------------------
# WebSocket bridge — auth-reject path only (no live upstream in this suite)
# ---------------------------------------------------------------------------


def test_ws_stranger_rejected_with_4403(client_stranger, running_app):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client_stranger.websocket_connect("/apps/s/ws"):
            pass
    assert excinfo.value.code == 4403


def test_ws_missing_app_rejected_with_4404(client_granted):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client_granted.websocket_connect("/apps/does-not-exist/ws"):
            pass
    assert excinfo.value.code == 4404


def test_ws_sleeping_app_rejected_with_4404(client_granted, sleeping_app):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client_granted.websocket_connect("/apps/s/ws"):
            pass
    assert excinfo.value.code == 4404


# ---------------------------------------------------------------------------
# Session-cookie domain — regression: no Domain= attribute when
# data_apps.subdomain_base is unset (today's exact behavior).
# ---------------------------------------------------------------------------


def test_session_cookie_no_domain_when_subdomain_base_unset(proxy_env):
    from starlette.responses import Response

    from app.auth.providers.password import _set_login_cookie

    resp = Response()
    _set_login_cookie(resp, "owner1", "owner@test.local")
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert set_cookie_header  # sanity: a cookie was actually set
    assert "domain=" not in set_cookie_header.lower()


def test_session_cookie_gets_parent_domain_when_subdomain_base_set(proxy_env):
    from starlette.responses import Response

    from app.auth.providers.password import _set_login_cookie

    _set_data_apps_config(proxy_env["data_dir"], subdomain_base="apps.example.com")
    resp = Response()
    _set_login_cookie(resp, "owner1", "owner@test.local")
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "domain=.example.com" in set_cookie_header.lower()


# ---------------------------------------------------------------------------
# get_data_apps_config() hardening — a `None` from get_value (bad/absent
# config, or a config-not-loaded-yet bootstrap state) must never crash the
# subdomain middleware or session_cookie_domain(); the accessor itself
# always returns a dict.
# ---------------------------------------------------------------------------


def test_get_data_apps_config_hardened_against_none(monkeypatch):
    import app.instance_config as instance_config

    monkeypatch.setattr(instance_config, "get_value", lambda *a, **k: None)
    assert instance_config.get_data_apps_config() == {}
    assert instance_config.session_cookie_domain() is None


def test_subdomain_middleware_survives_none_config(monkeypatch):
    import asyncio

    import app.data_apps_subdomain as subdomain_mod
    import app.instance_config as instance_config

    monkeypatch.setattr(instance_config, "get_value", lambda *a, **k: None)

    seen_paths = []

    async def inner_app(scope, receive, send):
        seen_paths.append(scope["path"])

    middleware = subdomain_mod.DataAppSubdomainMiddleware(inner_app)
    scope = {"type": "http", "path": "/metrics", "headers": [(b"host", b"example.com")]}

    asyncio.run(middleware(scope, None, None))
    assert seen_paths == ["/metrics"]  # no-op passthrough, no crash
