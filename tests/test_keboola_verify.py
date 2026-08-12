"""Identity gates over the /tokens/verify payload — master-token gate,
project binding, role gate, defensive adminOwner handling."""

import pytest

from app.auth.providers import keboola_verify as kv


def _payload(**overrides):
    base = {
        "id": "204",
        "isMasterToken": True,
        "owner": {"id": 5947, "name": "Acme DWH"},
        "admin": {"id": 42, "name": "Jane", "role": "admin"},
        "adminOwner": {"id": 42, "email": "jane@example.com", "name": "Jane"},
    }
    base.update(overrides)
    return base


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
    monkeypatch.setattr(kv, "configured_project_id", lambda: "5947")
    monkeypatch.setattr(kv, "allowed_roles", lambda: None)


class TestGates:
    def test_happy_path(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload())
        identity = kv.verify_storage_token("tok")
        assert identity.email == "jane@example.com"
        assert identity.project_id == "5947"
        assert identity.role == "admin"

    def test_non_master_token_rejected_even_with_adminowner(self, configured, monkeypatch):
        # The escalation case: a restricted token created by an admin verifies
        # WITH a back-filled adminOwner. isMasterToken is the discriminator.
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload(isMasterToken=False))
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "not_master_token"

    def test_project_mismatch(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload(owner={"id": 1, "name": "Other"}))
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "project_mismatch"

    def test_missing_owner_id_is_mismatch_not_pass(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload(owner={}))
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "project_mismatch"

    def test_missing_adminowner_email_is_explicit_failure(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload(adminOwner={}))
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "no_admin_identity"

    def test_role_gate(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "allowed_roles", lambda: ["admin", "share"])
        monkeypatch.setattr(
            kv,
            "_fetch_verify",
            lambda url, headers: _payload(admin={"id": 42, "name": "J", "role": "readOnly"}),
        )
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "role_forbidden"

    def test_unconfigured_stack_fails_closed(self, monkeypatch):
        monkeypatch.setattr(kv, "stack_url", lambda: None)
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "not_configured"

    def test_headers_choose_auth_scheme(self, configured, monkeypatch):
        seen = {}

        def fake(url, headers):
            seen.update(headers)
            return _payload()

        monkeypatch.setattr(kv, "_fetch_verify", fake)
        kv.verify_storage_token("plain-tok")
        assert seen == {"X-StorageApi-Token": "plain-tok"}
        seen.clear()
        kv.verify_oauth_access_token("oauth-tok")
        assert seen == {"Authorization": "Bearer oauth-tok"}


class TestErrorContract:
    """Every failure leaving this module is a KeboolaVerifyError — the SSRF
    gate's HTTPException, non-JSON 200 bodies, and config gaps included."""

    def test_ssrf_rejection_translates_to_verify_error(self, configured, monkeypatch):
        import fastapi

        def reject(url, field_name="url"):
            raise fastapi.HTTPException(
                status_code=400,
                detail="Invalid auth.keboola.stack_url: resolves to a private network",
            )

        monkeypatch.setattr("app.api.admin._validate_url_not_private", reject)

        def no_network(*args, **kwargs):
            raise AssertionError("httpx.get must not be reached when the SSRF gate rejects")

        monkeypatch.setattr(kv.httpx, "get", no_network)
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "verify_failed"
        assert "private network" in exc.value.detail

    def test_non_json_200_body_is_verify_error(self, configured, monkeypatch):
        import json

        monkeypatch.setattr("app.api.admin._validate_url_not_private", lambda url, field_name="url": None)

        class FakeResp:
            status_code = 200

            def json(self):
                raise json.JSONDecodeError("Expecting value", "<html>", 0)

        monkeypatch.setattr(kv.httpx, "get", lambda *args, **kwargs: FakeResp())
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "verify_failed"
        assert "non-JSON" in exc.value.detail

    @pytest.mark.parametrize("verify", ["verify_storage_token", "verify_oauth_access_token"])
    def test_missing_project_id_fails_closed_before_network(self, monkeypatch, verify):
        monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
        monkeypatch.setattr(kv, "configured_project_id", lambda: None)

        def fail_if_called(*args, **kwargs):
            raise AssertionError("_fetch_verify must not be called when project_id is unconfigured")

        monkeypatch.setattr(kv, "_fetch_verify", fail_if_called)
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            getattr(kv, verify)("tok")
        assert exc.value.reason == "not_configured"
