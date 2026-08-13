"""X-StorageApi-Token header auth: mapping, precedence, classification,
cache, flood guard — and the require_session_token laundering block.

No ``/auth/me`` (or other bare whoami) route exists on this instance — see
``app/auth/router.py`` / the ``/api/me*`` routers, none of which expose a
plain "who am I" JSON payload reachable with nothing but ``get_current_user``.
Per the task brief's own fallback, endpoint-level assertions below exercise
``GET /api/catalog/tables`` (an ordinary ``Depends(get_current_user)`` route,
see ``app/api/catalog.py``) and assert on status codes only; the
mapping-to-identity assertion (``credential_surface`` / ``token_type``) is
covered at the unit level via ``resolve_header_user`` directly in
``TestResolveUnit``.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.providers import keboola_verify as kv


def _identity(email="jane@example.com"):
    return kv.VerifiedKeboolaIdentity(
        token_id="204",
        project_id="5947",
        project_name="Acme DWH",
        email=email,
        name="Jane",
        role="admin",
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setenv("AGNES_KEBOOLA_ALLOW_TOKEN_HEADER", "1")
    monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
    monkeypatch.setattr(kv, "configured_project_id", lambda: "5947")
    from app.auth import keboola_header

    keboola_header.reset_state_for_tests()
    from app.main import create_app
    from src.repositories import users_repo

    app = create_app()
    c = TestClient(app)
    uid = str(uuid.uuid4())
    users_repo().create(id=uid, email="jane@example.com", name="Jane")
    return c


class TestHeaderAuth:
    def test_maps_to_existing_user(self, client, monkeypatch):
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-1"})
        assert resp.status_code == 200

    def test_unknown_user_gets_onboarding_hint(self, client, monkeypatch):
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity("nobody@example.com"))
        resp = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-2"})
        assert resp.status_code == 401
        assert "sign in" in resp.json()["detail"].lower()

    def test_switch_off_ignores_header(self, client, monkeypatch):
        called = []
        monkeypatch.setenv("AGNES_KEBOOLA_ALLOW_TOKEN_HEADER", "0")
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: called.append(tok) or _identity())
        resp = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-3"})
        assert resp.status_code == 401
        # Switched off means the header is never even consulted — verify
        # must not be invoked at all.
        assert called == []

    def test_bearer_takes_precedence(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: called.append(tok) or _identity())
        resp = client.get(
            "/api/catalog/tables",
            headers={"Authorization": "Bearer not-a-jwt", "X-StorageApi-Token": "tok-4"},
        )
        # The bogus bearer fails auth; the storage header must NOT rescue it.
        assert resp.status_code == 401
        assert called == []

    def test_cookie_takes_precedence(self, client, monkeypatch):
        # Mirrors test_bearer_takes_precedence for the cookie half of the
        # precedence rule: a present-but-invalid session cookie must not be
        # rescued by a valid X-StorageApi-Token header either.
        called = []
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: called.append(tok) or _identity())
        client.cookies.set("access_token", "not-a-jwt")
        resp = client.get(
            "/api/catalog/tables",
            headers={"X-StorageApi-Token": "tok-cookie"},
        )
        assert resp.status_code == 401
        assert called == []

    def test_deactivation_takes_effect_immediately(self, client, monkeypatch):
        # The upstream Keboola verify result is cached for 60s, but the
        # Agnes-side users_repo().active check is NOT — an admin flipping
        # a user inactive must lock them out on the very next request, not
        # after the cache expires.
        calls = []

        def counting(tok):
            calls.append(tok)
            return _identity()

        monkeypatch.setattr(kv, "verify_storage_token", counting)
        from src.repositories import users_repo

        # Warm the cache with one successful request.
        resp = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-deactivate"})
        assert resp.status_code == 200
        assert len(calls) == 1

        jane = users_repo().get_by_email("jane@example.com")
        users_repo().update(jane["id"], active=False)

        resp = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-deactivate"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Account deactivated"
        # The upstream verify was NOT re-invoked — the deactivation is
        # caught by the uncached users_repo lookup, not by re-verifying.
        assert len(calls) == 1

    def test_verify_cache_hits_within_ttl(self, client, monkeypatch):
        calls = []

        def counting(tok):
            calls.append(tok)
            return _identity()

        monkeypatch.setattr(kv, "verify_storage_token", counting)
        for _ in range(3):
            assert client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-5"}).status_code == 200
        assert len(calls) == 1

    def test_cannot_mint_pat(self, client, monkeypatch):
        # The laundering block: a Storage token must never create a persistent PAT.
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.post(
            "/auth/tokens",
            json={"name": "laundered"},
            headers={"X-StorageApi-Token": "tok-6"},
        )
        assert resp.status_code == 403

    def test_cannot_mint_cowork_bundle(self, client, monkeypatch):
        # Same laundering hole, different mint: a Cowork Setup Bundle embeds
        # a setup token AND a pre-baked 90-day PAT (app/api/cowork_bundle.py
        # generate_bundle) — a captured Storage API token must not be able
        # to walk away with either.
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.post(
            "/api/user/cowork-bundle",
            headers={"X-StorageApi-Token": "tok-bundle"},
        )
        assert resp.status_code == 403

    def test_cannot_create_data_app(self, client, monkeypatch):
        # Same laundering hole for the data-apps credential-minting surface
        # (app/api/data_apps.py create/deploy/git-credential/preview-grant/
        # drafts) — exercised here via the create route. The
        # reject_keboola_header_credential dependency runs during FastAPI's
        # dependency-resolution phase, before the handler body's own
        # _feature_gate() check, so this 403s regardless of whether
        # data_apps is enabled in this test environment.
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.post(
            "/api/data-apps",
            json={"slug": "laundered-app", "name": "Laundered"},
            headers={"X-StorageApi-Token": "tok-app"},
        )
        assert resp.status_code == 403

    def test_flood_guard_trips_on_distinct_invalid_tokens(self, client, monkeypatch):
        def failing(tok):
            raise kv.KeboolaVerifyError("invalid_token", "no")

        monkeypatch.setattr(kv, "verify_storage_token", failing)
        last = None
        for i in range(30):
            last = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": f"junk-{i}"})
        assert last.status_code == 429

    def test_flood_guard_does_not_trip_on_successful_bursts(self, client, monkeypatch):
        # The bug being fixed: the global cache-miss cap used to count
        # SUCCESSFUL verifies too, so >30 distinct legitimate tokens (each a
        # cache miss, each a valid master token) per 60s window would 429
        # every caller after the cap. Only FAILED verifies may consume the
        # flood budget — a burst of 35 distinct, always-successful tokens
        # (more than the old _MAX_FAILURES_GLOBAL=30 cap) must all succeed.
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        statuses = [
            client.get("/api/catalog/tables", headers={"X-StorageApi-Token": f"good-{i}"}).status_code
            for i in range(35)
        ]
        assert statuses == [200] * 35


class TestResolveUnit:
    def test_credential_surface_is_stack(self, client, monkeypatch):
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        from app.auth.keboola_header import resolve_header_user

        user, reason = resolve_header_user("tok-7", None)
        assert reason == ""
        assert user["email"] == "jane@example.com"
        assert user["credential_surface"] == "stack"
        assert user["token_type"] == "keboola_token"


class TestRejectKeboolaHeaderCredential:
    """Unit coverage for app.auth.dependencies.reject_keboola_header_credential
    independent of any route — the FastAPI-level 403s above prove it's wired
    in; these prove the predicate itself is correct for both classes of
    caller it must distinguish.
    """

    def test_rejects_keboola_token_user(self):
        from app.auth.dependencies import reject_keboola_header_credential
        from fastapi import HTTPException

        user = {"id": "u1", "email": "jane@example.com", "token_type": "keboola_token"}
        with pytest.raises(HTTPException) as exc_info:
            reject_keboola_header_credential(user=user)
        assert exc_info.value.status_code == 403

    def test_passes_through_pat_user(self):
        from app.auth.dependencies import reject_keboola_header_credential

        user = {"id": "u1", "email": "jane@example.com", "token_type": "pat"}
        assert reject_keboola_header_credential(user=user) is user

    def test_passes_through_regular_session_user(self):
        from app.auth.dependencies import reject_keboola_header_credential

        # No token_type at all — the shape of a plain cookie-session user.
        user = {"id": "u1", "email": "jane@example.com"}
        assert reject_keboola_header_credential(user=user) is user


class TestAuditClientKind:
    """The header credential is non-interactive, like a PAT — the audit
    trail must bucket it the same way, not as 'web' (an interactive browser
    session).
    """

    def test_keboola_token_buckets_as_cli(self):
        from src.audit_helpers import client_kind_from_user

        user = {"id": "u1", "email": "jane@example.com", "token_type": "keboola_token"}
        assert client_kind_from_user(user) == "cli"


class TestBackoffDecay:
    """Direct, deterministic coverage of the per-IP backoff arming/decay in
    app.auth.keboola_header — driven with synthetic ``now`` values so it
    needs no real sleeping. Calls the module's private helpers directly
    (single-threaded test, no concurrent caller to race the lock).
    """

    def test_backoff_decays_after_window_elapses(self):
        from app.auth import keboola_header as kh

        kh.reset_state_for_tests()
        ip = "203.0.113.5"
        t0 = 1_000_000.0
        # Arm the backoff: 5 consecutive failures.
        for i in range(kh._FAILURES_BEFORE_BACKOFF):
            assert kh._admit_miss(ip, t0 + i) is None
            kh._record_failure(ip, t0 + i)

        # Still inside the backoff window — blocked without a decay.
        t_inside = t0 + kh._FAILURES_BEFORE_BACKOFF + 1
        assert kh._admit_miss(ip, t_inside) == "rate_limited"

        # Well past backoff_until with no further failures — must decay,
        # not stay armed forever.
        t_after = t0 + kh._FAILURES_BEFORE_BACKOFF + kh._FAILURE_BACKOFF_SECONDS + 1
        assert kh._admit_miss(ip, t_after) is None
        assert ip not in kh._failure_state


class TestStatePruning:
    """The per-IP flood-guard dicts must not grow without bound under
    rotating-source abuse — _record_failure sweeps stale entries once a dict
    crosses _STATE_MAX_ENTRIES."""

    def test_stale_entries_are_pruned_live_ones_kept(self):
        from app.auth import keboola_header as kh

        kh.reset_state_for_tests()
        now = 2_000_000.0
        # Seed more than the cap of long-elapsed entries (window + backoff far
        # in the past), plus one still-live armed IP that must survive.
        for i in range(kh._STATE_MAX_ENTRIES + 50):
            kh._failure_windows[f"ip:stale{i}"] = (now - 10 * kh._FAILURE_WINDOW_SECONDS, 3)
            kh._failure_state[f"ip:stale{i}"] = (now - 10 * kh._FAILURE_BACKOFF_SECONDS, 5)
        kh._failure_state["ip:live"] = (now + kh._FAILURE_BACKOFF_SECONDS, 5)  # backoff still active

        kh._record_failure("198.51.100.7", now)  # crosses the cap → sweeps

        # Stale entries gone; the live armed IP and the GLOBAL counter remain.
        assert not any(k.startswith("ip:stale") for k in kh._failure_state)
        assert not any(k.startswith("ip:stale") for k in kh._failure_windows)
        assert "ip:live" in kh._failure_state
        assert kh._GLOBAL_KEY in kh._failure_windows
