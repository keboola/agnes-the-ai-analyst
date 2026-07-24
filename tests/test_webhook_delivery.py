"""`app/chat/webhook_delivery.py` (V1b Task 6) — SSRF resolve-and-pin guard,
HMAC signing, and delivery with failure-tracking/auto-disable.

`validate_and_resolve` tests monkeypatch `socket.getaddrinfo` for
determinism (no real DNS / network access, and no dependency on how the
sandbox running these tests resolves `localhost` or handles literal IPs).

`deliver` tests use a real `agent_webhooks_repo()`-backed row (DuckDB,
temp `DATA_DIR`) so `record_success`/`record_failure`/`disable` exercise
the actual repository — only the network boundary (`httpx.Client`) and the
SSRF resolution (`validate_and_resolve`) are faked.
"""

from __future__ import annotations

import socket
import uuid

import pytest


# ---------------------------------------------------------------------------
# validate_and_resolve
# ---------------------------------------------------------------------------


def _mock_getaddrinfo(monkeypatch, mapping: dict[str, list[str]]) -> None:
    """Patch `webhook_delivery.socket.getaddrinfo` to resolve `mapping`
    entries only; anything else raises `socket.gaierror` (mirrors NXDOMAIN)."""
    import app.chat.webhook_delivery as webhook_delivery

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(f"no mock DNS entry for {host!r}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in mapping[host]]

    monkeypatch.setattr(webhook_delivery.socket, "getaddrinfo", fake_getaddrinfo)


def test_rejects_plain_http():
    from app.chat.webhook_delivery import validate_and_resolve

    with pytest.raises(ValueError, match="https"):
        validate_and_resolve("http://example.com/hook")


def test_rejects_metadata_ip_literal():
    from app.chat.webhook_delivery import validate_and_resolve

    with pytest.raises(ValueError, match="forbidden"):
        validate_and_resolve("https://169.254.169.254/latest/meta-data/")


def test_rejects_localhost(monkeypatch):
    from app.chat.webhook_delivery import validate_and_resolve

    _mock_getaddrinfo(monkeypatch, {"localhost": ["127.0.0.1"]})
    with pytest.raises(ValueError, match="forbidden"):
        validate_and_resolve("https://localhost/hook")


def test_rejects_hostname_resolving_to_loopback(monkeypatch):
    """A hostname an attacker controls DNS for, currently pointed at
    127.0.0.1 — the general case `test_rejects_localhost` is a special
    case of."""
    from app.chat.webhook_delivery import validate_and_resolve

    _mock_getaddrinfo(monkeypatch, {"evil.example.com": ["127.0.0.1"]})
    with pytest.raises(ValueError, match="forbidden"):
        validate_and_resolve("https://evil.example.com/hook")


def test_rejects_private_ipv4_literal():
    from app.chat.webhook_delivery import validate_and_resolve

    with pytest.raises(ValueError, match="forbidden"):
        validate_and_resolve("https://10.1.2.3/hook")


def test_rejects_cgnat_ipv4_literal():
    """100.64.0.0/10 (RFC 6598, carrier-grade NAT / shared address space) —
    not globally routable, and NOT classified as `is_private`/`is_reserved`
    by Python's stdlib `ipaddress` (verified directly), so this needs its
    own explicit check — see `_CGNAT_NET`."""
    from app.chat.webhook_delivery import validate_and_resolve

    with pytest.raises(ValueError, match="forbidden"):
        validate_and_resolve("https://100.64.0.1/hook")


def test_rejects_if_any_resolved_address_is_forbidden(monkeypatch):
    """A hostname resolving to BOTH a public decoy and a private target must
    be denied — not accepted because "at least one" address is public."""
    from app.chat.webhook_delivery import validate_and_resolve

    _mock_getaddrinfo(monkeypatch, {"rebind.example.com": ["93.184.216.34", "10.0.0.9"]})
    with pytest.raises(ValueError, match="forbidden"):
        validate_and_resolve("https://rebind.example.com/hook")


def test_rejects_unresolvable_host(monkeypatch):
    from app.chat.webhook_delivery import validate_and_resolve

    _mock_getaddrinfo(monkeypatch, {})  # every host raises gaierror
    with pytest.raises(ValueError, match="could not resolve"):
        validate_and_resolve("https://nxdomain.example.invalid/hook")


def test_accepts_public_host_and_returns_pinned_ip(monkeypatch):
    from app.chat.webhook_delivery import validate_and_resolve

    _mock_getaddrinfo(monkeypatch, {"hooks.example.com": ["93.184.216.34"]})
    assert validate_and_resolve("https://hooks.example.com/incoming") == "93.184.216.34"


# ---------------------------------------------------------------------------
# sign
# ---------------------------------------------------------------------------


def test_sign_is_deterministic_and_verifiable():
    import hashlib
    import hmac as hmac_mod

    from app.chat.webhook_delivery import sign

    body = b'{"event":"job.completed"}'
    sig1 = sign("s3cr3t", body)
    sig2 = sign("s3cr3t", body)
    assert sig1 == sig2
    assert sig1.startswith("sha256=")

    expected = "sha256=" + hmac_mod.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert sig1 == expected


def test_sign_differs_by_secret_and_body():
    from app.chat.webhook_delivery import sign

    body = b"{}"
    assert sign("secret-a", body) != sign("secret-b", body)
    assert sign("secret-a", b"{}") != sign("secret-a", b'{"x":1}')


# ---------------------------------------------------------------------------
# deliver
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()
    yield
    close_system_db()


def _make_webhook(agent_id="agent-1", owner_id="owner-1", url="https://hooks.example.com/incoming"):
    from src.repositories import agent_webhooks_repo

    webhook_id = uuid.uuid4().hex
    agent_webhooks_repo().create(
        id=webhook_id,
        agent_id=agent_id,
        owner_user_id=owner_id,
        url=url,
        secret="webhook-secret",
        events="job.completed,job.failed",
    )
    return agent_webhooks_repo().get(webhook_id)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHTTPXClient:
    """Fakes `httpx.Client` at the `with ... as client: client.post(...)`
    seam `deliver`/`_post_to_pinned_ip` uses. Records every `.post()` call
    so tests can assert on the URL/headers/extensions actually sent."""

    calls: list[dict] = []
    response_status: int = 200
    raise_on_post: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_FakeHTTPXClient":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def post(self, url, **kwargs):
        type(self).calls.append({"url": url, **kwargs})
        if type(self).raise_on_post is not None:
            raise type(self).raise_on_post
        return _FakeResponse(type(self).response_status)


@pytest.fixture(autouse=True)
def _reset_fake_client():
    _FakeHTTPXClient.calls = []
    _FakeHTTPXClient.response_status = 200
    _FakeHTTPXClient.raise_on_post = None
    yield


def _patch_httpx_client(monkeypatch) -> None:
    import app.chat.webhook_delivery as webhook_delivery

    monkeypatch.setattr(webhook_delivery.httpx, "Client", _FakeHTTPXClient)


def _patch_pinned_ip(monkeypatch, ip: str) -> None:
    import app.chat.webhook_delivery as webhook_delivery

    monkeypatch.setattr(webhook_delivery, "validate_and_resolve", lambda url: ip)


def test_deliver_success_records_success(db, monkeypatch):
    from src.repositories import agent_webhooks_repo

    webhook = _make_webhook()
    _patch_pinned_ip(monkeypatch, "203.0.113.7")
    _patch_httpx_client(monkeypatch)
    _FakeHTTPXClient.response_status = 200

    from app.chat.webhook_delivery import deliver

    ok = deliver(webhook, {"event": "job.completed", "job_id": "j1", "agent_slug": "a", "status": "completed"})

    assert ok is True
    row = agent_webhooks_repo().get(webhook["id"])
    assert row["consecutive_failures"] == 0
    assert row["active"] is True


def test_deliver_failure_increments_failure_count(db, monkeypatch):
    from src.repositories import agent_webhooks_repo

    webhook = _make_webhook()
    _patch_pinned_ip(monkeypatch, "203.0.113.7")
    _patch_httpx_client(monkeypatch)
    _FakeHTTPXClient.response_status = 500

    from app.chat.webhook_delivery import deliver

    ok = deliver(webhook, {"event": "job.failed", "job_id": "j1", "agent_slug": "a", "status": "failed"})

    assert ok is False
    row = agent_webhooks_repo().get(webhook["id"])
    assert row["consecutive_failures"] == 1
    assert row["active"] is True


def test_deliver_disables_after_max_consecutive_failures(db, monkeypatch):
    from src.repositories import agent_webhooks_repo
    from app.chat.webhook_delivery import deliver, webhook_max_failures

    webhook = _make_webhook()
    _patch_pinned_ip(monkeypatch, "203.0.113.7")
    _patch_httpx_client(monkeypatch)
    _FakeHTTPXClient.response_status = 500

    max_failures = webhook_max_failures()
    assert max_failures == 5  # default, no instance.yaml overlay present

    for i in range(max_failures):
        deliver(webhook, {"event": "job.failed", "job_id": f"j{i}", "agent_slug": "a", "status": "failed"})

    row = agent_webhooks_repo().get(webhook["id"])
    assert row["consecutive_failures"] == max_failures
    assert row["active"] is False
    assert row["disabled_at"] is not None


def test_deliver_success_resets_failure_count(db, monkeypatch):
    from src.repositories import agent_webhooks_repo

    webhook = _make_webhook()
    _patch_pinned_ip(monkeypatch, "203.0.113.7")
    _patch_httpx_client(monkeypatch)

    from app.chat.webhook_delivery import deliver

    _FakeHTTPXClient.response_status = 500
    deliver(webhook, {"status": "failed"})
    deliver(webhook, {"status": "failed"})
    assert agent_webhooks_repo().get(webhook["id"])["consecutive_failures"] == 2

    _FakeHTTPXClient.response_status = 200
    deliver(webhook, {"status": "completed"})
    assert agent_webhooks_repo().get(webhook["id"])["consecutive_failures"] == 0


def test_deliver_ssrf_denial_at_send_time_counts_as_failure(db, monkeypatch):
    """DNS-rebind defense: even if the URL passed create-time validation,
    a `deliver()` call that re-resolves to a now-forbidden address must
    fail closed (never call out) and still count against the webhook."""
    from src.repositories import agent_webhooks_repo

    webhook = _make_webhook()
    _patch_httpx_client(monkeypatch)

    import app.chat.webhook_delivery as webhook_delivery

    def deny(url):
        raise ValueError("webhook host resolves to a forbidden address")

    monkeypatch.setattr(webhook_delivery, "validate_and_resolve", deny)

    ok = webhook_delivery.deliver(webhook, {"status": "completed"})

    assert ok is False
    assert _FakeHTTPXClient.calls == []  # never connected anywhere
    assert agent_webhooks_repo().get(webhook["id"])["consecutive_failures"] == 1


def test_deliver_connects_to_pinned_ip_with_original_host_header(db, monkeypatch):
    webhook = _make_webhook(url="https://hooks.example.com:8443/incoming?x=1")
    _patch_pinned_ip(monkeypatch, "203.0.113.9")
    _patch_httpx_client(monkeypatch)

    from app.chat.webhook_delivery import deliver

    deliver(webhook, {"event": "job.completed"})

    assert len(_FakeHTTPXClient.calls) == 1
    call = _FakeHTTPXClient.calls[0]
    # The socket connects to the PINNED IP (not the hostname)...
    assert call["url"].startswith("https://203.0.113.9:8443/incoming")
    assert "hooks.example.com" not in call["url"]
    # ...but the Host header and TLS SNI still carry the ORIGINAL hostname.
    assert call["headers"]["Host"] == "hooks.example.com"
    assert call["extensions"] == {"sni_hostname": "hooks.example.com"}
    assert call["follow_redirects"] is False


def test_deliver_signs_body_with_webhook_secret(db, monkeypatch):
    from app.chat.webhook_delivery import deliver, sign

    webhook = _make_webhook()
    _patch_pinned_ip(monkeypatch, "203.0.113.9")
    _patch_httpx_client(monkeypatch)

    payload = {"event": "job.completed", "job_id": "j1", "agent_slug": "a", "status": "completed", "ts": "now"}
    deliver(webhook, payload)

    call = _FakeHTTPXClient.calls[0]
    body = call["content"]
    assert call["headers"]["x-agnes-signature"] == sign(webhook["secret"], body)
