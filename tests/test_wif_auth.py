"""Unit tests for the Workload Identity Federation token helper (app/auth/wif.py).

Covers the exchange request shape, caching/refresh, cache invalidation, and the
failure modes — all with the network mocked, so no real federation rule needed.
"""

from __future__ import annotations

import asyncio

import pytest

import app.auth.wif as wif


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ""

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch):
    wif.clear_token_cache()
    for k in (
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "ANTHROPIC_WORKSPACE_ID",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
    ):
        monkeypatch.delenv(k, raising=False)
    yield
    wif.clear_token_cache()


def _set_federation_env(monkeypatch, *, workspace=None, identity="eyJhbGci.fake.jwt"):
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_test")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_test")
    if identity is not None:
        monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN", identity)
    if workspace is not None:
        monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", workspace)


def test_exchange_builds_correct_body_and_returns_token(monkeypatch):
    _set_federation_env(monkeypatch, workspace="wrkspc_test", identity="the.identity.jwt")
    captured = {}

    def _fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(200, {"access_token": "sk-ant-oat01-ABC", "expires_in": 3600})

    monkeypatch.setattr(wif.httpx, "post", _fake_post)

    tok = wif.get_federated_access_token()
    assert tok == "sk-ant-oat01-ABC"
    assert captured["url"].endswith("/v1/oauth/token")
    body = captured["json"]
    assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert body["assertion"] == "the.identity.jwt"
    assert body["federation_rule_id"] == "fdrl_test"
    assert body["organization_id"] == "00000000-0000-0000-0000-000000000000"
    assert body["service_account_id"] == "svac_test"
    assert body["workspace_id"] == "wrkspc_test"


def test_workspace_id_omitted_when_unset(monkeypatch):
    _set_federation_env(monkeypatch, workspace=None)
    captured = {}

    def _fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _FakeResp(200, {"access_token": "t", "expires_in": 3600})

    monkeypatch.setattr(wif.httpx, "post", _fake_post)
    wif.get_federated_access_token()
    assert "workspace_id" not in captured["json"]


def test_token_is_cached_no_second_exchange(monkeypatch):
    _set_federation_env(monkeypatch)
    calls = {"n": 0}

    def _fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(200, {"access_token": "sk-ant-oat01-CACHED", "expires_in": 3600})

    monkeypatch.setattr(wif.httpx, "post", _fake_post)
    assert wif.get_federated_access_token() == "sk-ant-oat01-CACHED"
    assert wif.get_federated_access_token() == "sk-ant-oat01-CACHED"
    assert calls["n"] == 1  # second call served from cache


def test_clear_token_cache_forces_reexchange(monkeypatch):
    _set_federation_env(monkeypatch)
    calls = {"n": 0}

    def _fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(200, {"access_token": f"tok-{calls['n']}", "expires_in": 3600})

    monkeypatch.setattr(wif.httpx, "post", _fake_post)
    assert wif.get_federated_access_token() == "tok-1"
    wif.clear_token_cache()
    assert wif.get_federated_access_token() == "tok-2"
    assert calls["n"] == 2


def test_identity_token_from_file(monkeypatch, tmp_path):
    _set_federation_env(monkeypatch, identity=None)
    p = tmp_path / "token"
    p.write_text("file.identity.jwt\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", str(p))
    captured = {}

    def _fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _FakeResp(200, {"access_token": "t", "expires_in": 3600})

    monkeypatch.setattr(wif.httpx, "post", _fake_post)
    wif.get_federated_access_token()
    assert captured["json"]["assertion"] == "file.identity.jwt"  # stripped


def test_missing_federation_env_raises(monkeypatch):
    # identity token present but federation ids missing
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN", "x.y.z")
    with pytest.raises(wif.WIFAuthError, match="FEDERATION_RULE_ID"):
        wif.get_federated_access_token()


def test_missing_identity_token_raises(monkeypatch):
    _set_federation_env(monkeypatch, identity=None)  # no ANTHROPIC_IDENTITY_TOKEN[_FILE]
    with pytest.raises(wif.WIFAuthError, match="IDENTITY_TOKEN"):
        wif.get_federated_access_token()


def test_non_200_exchange_raises(monkeypatch):
    _set_federation_env(monkeypatch)
    monkeypatch.setattr(
        wif.httpx,
        "post",
        lambda url, json=None, timeout=None: _FakeResp(400, text='{"error":"invalid_grant"}'),
    )
    with pytest.raises(wif.WIFAuthError, match="HTTP 400"):
        wif.get_federated_access_token()


def test_missing_access_token_in_response_raises(monkeypatch):
    _set_federation_env(monkeypatch)
    monkeypatch.setattr(
        wif.httpx,
        "post",
        lambda url, json=None, timeout=None: _FakeResp(200, {"token_type": "Bearer"}),
    )
    with pytest.raises(wif.WIFAuthError, match="missing access_token"):
        wif.get_federated_access_token()


# --- test_wif_credentials (admin "test connection" probe for WIF mode) --------


class _FakeMessages:
    def __init__(self, exc=None):
        self._exc = exc

    def create(self, **_k):
        if self._exc is not None:
            raise self._exc
        return object()


class _FakeAnthropic:
    exc = None

    def __init__(self, **_k):
        self.messages = _FakeMessages(_FakeAnthropic.exc)


def test_wif_credentials_ok(monkeypatch):
    import anthropic

    import app.auth.wif as w
    from app.chat.readiness import test_wif_credentials

    monkeypatch.setattr(w, "get_federated_access_token", lambda: "sk-ant-oat01-OK")
    _FakeAnthropic.exc = None
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    r = asyncio.run(test_wif_credentials())
    assert r["ok"] is True


def test_wif_credentials_exchange_failure(monkeypatch):
    import app.auth.wif as w
    from app.chat.readiness import test_wif_credentials

    def _boom():
        raise w.WIFAuthError("no ANTHROPIC_IDENTITY_TOKEN set")

    monkeypatch.setattr(w, "get_federated_access_token", _boom)
    r = asyncio.run(test_wif_credentials())
    assert r["ok"] is False
    assert "exchange failed" in r["detail"]


def test_wif_credentials_token_minted_but_api_rejects(monkeypatch):
    import anthropic

    import app.auth.wif as w
    from app.chat.readiness import test_wif_credentials

    monkeypatch.setattr(w, "get_federated_access_token", lambda: "sk-ant-oat01-SCOPED")
    cleared = {"n": 0}
    monkeypatch.setattr(w, "clear_token_cache", lambda: cleared.__setitem__("n", cleared["n"] + 1))

    class _AuthErr(Exception):
        status_code = 403

    _FakeAnthropic.exc = _AuthErr("forbidden")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    r = asyncio.run(test_wif_credentials())
    assert r["ok"] is False
    assert "API call failed" in r["detail"]
    assert cleared["n"] == 1  # bad token dropped from cache
