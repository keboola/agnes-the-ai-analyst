"""Keboola OAuth-host client: introspect parsing, instance gates, PAT minting.

HTTP is faked at the httpx layer so the status-code map, the JSON-shape
defenses and the endpoint-fallback behavior are the code under test.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
import pytest

from app.auth.providers import keboola_projects as kp
from app.auth.providers import keboola_verify as kv


@pytest.fixture(autouse=True)
def _configured_host(monkeypatch):
    monkeypatch.setattr(kv, "oauth_host", lambda: "https://connection.example.com")
    # These tests exercise the client, not the SSRF gate (the gate has its
    # own test below) — stub the shared validator permissive, same pattern
    # as tests/test_keboola_verify.py.
    monkeypatch.setattr("app.api.admin._validate_url_not_private", lambda url, field_name="url": None)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None, *, bad_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self) -> Dict[str, Any]:
        if self._bad_json:
            raise ValueError("not json")
        return self._payload or {}


class TestIntrospectProjects:
    def test_parses_projects_and_normalizes_ids(self, monkeypatch):
        payload = {
            "projects": [
                {"id": 516, "name": "Agnes - test", "role": "admin"},
                {"id": "7", "name": "Beta", "role": "readOnly"},
                {"name": "no id — dropped"},
                "not-a-dict",
            ]
        }
        seen = {}

        def fake_get(url, headers=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            return FakeResponse(200, payload)

        monkeypatch.setattr(httpx, "get", fake_get)
        projects = kp.introspect_projects("at-123")
        assert seen["url"] == "https://connection.example.com/v1/auth/token/introspect"
        assert seen["headers"]["Authorization"] == "Bearer at-123"
        assert [(p.id, p.name, p.role) for p in projects] == [
            ("516", "Agnes - test", "admin"),
            ("7", "Beta", "readOnly"),
        ]

    def test_rejected_token_maps_to_invalid_token(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(401))
        with pytest.raises(kp.KeboolaProjectApiError) as err:
            kp.introspect_projects("at-123")
        assert err.value.reason == "invalid_token"

    def test_network_failure_maps_to_introspect_failed(self, monkeypatch):
        def boom(*a, **k):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx, "get", boom)
        with pytest.raises(kp.KeboolaProjectApiError) as err:
            kp.introspect_projects("at-123")
        assert err.value.reason == "introspect_failed"

    def test_missing_projects_list_is_a_failure_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(200, {"unexpected": True}))
        with pytest.raises(kp.KeboolaProjectApiError) as err:
            kp.introspect_projects("at-123")
        assert err.value.reason == "introspect_failed"

    def test_non_json_response_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(200, bad_json=True))
        with pytest.raises(kp.KeboolaProjectApiError):
            kp.introspect_projects("at-123")

    def test_http_host_fails_closed_before_any_request(self, monkeypatch):
        monkeypatch.setattr(kv, "oauth_host", lambda: "http://connection.example.com")
        called = {"get": False}

        def fake_get(*a, **k):
            called["get"] = True
            return FakeResponse(200, {"projects": []})

        monkeypatch.setattr(httpx, "get", fake_get)
        with pytest.raises(kp.KeboolaProjectApiError) as err:
            kp.introspect_projects("at-123")
        assert err.value.reason == "not_configured"
        assert called["get"] is False


class TestFilterProjects:
    def _projects(self):
        return [
            kp.DiscoveredProject(id="516", name="A", role="admin"),
            kp.DiscoveredProject(id="7", name="B", role="guest"),
            kp.DiscoveredProject(id="8", name="C", role="readOnly"),
        ]

    def test_allowed_roles_narrow(self, monkeypatch):
        monkeypatch.setattr(kv, "allowed_roles", lambda: ["admin"])
        monkeypatch.setattr(kv, "is_wildcard_project", lambda: True)
        kept = kp.filter_projects(self._projects())
        assert [p.id for p in kept] == ["516"]

    def test_no_roles_config_keeps_all_under_wildcard(self, monkeypatch):
        monkeypatch.setattr(kv, "allowed_roles", lambda: None)
        monkeypatch.setattr(kv, "is_wildcard_project", lambda: True)
        assert len(kp.filter_projects(self._projects())) == 3

    def test_pinned_project_narrows_discovery(self, monkeypatch):
        monkeypatch.setattr(kv, "allowed_roles", lambda: None)
        monkeypatch.setattr(kv, "is_wildcard_project", lambda: False)
        monkeypatch.setattr(kv, "configured_project_id", lambda: "7")
        kept = kp.filter_projects(self._projects())
        assert [p.id for p in kept] == ["7"]


class TestExchangeProjectPat:
    def test_mints_with_scope_and_read_only(self, monkeypatch):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append((url, headers, json))
            return FakeResponse(200, {"token": "pat-abc"})

        monkeypatch.setattr(httpx, "post", fake_post)
        token = kp.exchange_project_pat("at-123", "516", read_only=False)
        assert token == "pat-abc"
        url, headers, body = calls[0]
        assert url == "https://connection.example.com/v1/auth/pat/exchange"
        assert headers["Authorization"] == "Bearer at-123"
        assert body == {"scope": {"projects": ["516"]}, "readOnly": False}

    def test_falls_back_to_the_pat_endpoint_on_404(self, monkeypatch):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(url)
            if url.endswith("/pat/exchange"):
                return FakeResponse(404)
            return FakeResponse(201, {"pat": "pat-fallback"})

        monkeypatch.setattr(httpx, "post", fake_post)
        assert kp.exchange_project_pat("at-123", "7", read_only=True) == "pat-fallback"
        assert calls == [
            "https://connection.example.com/v1/auth/pat/exchange",
            "https://connection.example.com/v1/auth/pat",
        ]

    def test_denied_mint_maps_to_pat_exchange_denied(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(403))
        with pytest.raises(kp.KeboolaProjectApiError) as err:
            kp.exchange_project_pat("at-123", "516", read_only=False)
        assert err.value.reason == "pat_exchange_denied"

    def test_nested_token_shape_is_unwrapped(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(200, {"token": {"token": "pat-x"}}))
        assert kp.exchange_project_pat("at-123", "516", read_only=False) == "pat-x"

    def test_tokenless_response_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(200, {"id": 1}))
        with pytest.raises(kp.KeboolaProjectApiError) as err:
            kp.exchange_project_pat("at-123", "516", read_only=False)
        assert err.value.reason == "pat_exchange_failed"

    def test_both_endpoints_missing_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(404))
        with pytest.raises(kp.KeboolaProjectApiError) as err:
            kp.exchange_project_pat("at-123", "516", read_only=False)
        assert err.value.reason == "pat_exchange_failed"
