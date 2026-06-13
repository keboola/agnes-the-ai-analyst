"""Tests for app.services.sync_notifier — webhook alert on scheduled sync failure.

The notifier is best-effort: a webhook outage must never break the sync. It
POSTs a Slack / Google-Chat-compatible ``{"text": ...}`` payload to the
configured ``notifications.alert_webhook_url`` and no-ops when that URL is unset.
"""

import pytest


@pytest.fixture(autouse=True)
def _clear_instance_cache():
    """Each test sets its own ``alert_webhook_url`` via monkeypatched
    ``get_value`` — make sure no cached instance.yaml leaks between tests."""
    yield


def test_notify_no_url_does_not_post(monkeypatch):
    """Unset webhook URL → no HTTP call at all."""
    from app.services import sync_notifier

    monkeypatch.setattr(sync_notifier, "_alert_webhook_url", lambda: "")

    calls = []
    monkeypatch.setattr(
        sync_notifier.httpx, "post", lambda *a, **kw: calls.append((a, kw))
    )

    sync_notifier.notify_sync_failure(
        failed_tables=[{"table": "orders", "error": "boom"}], fatal=None
    )
    assert calls == []


def test_notify_fatal_posts_once_with_context(monkeypatch):
    """A fatal exception → exactly one webhook POST whose text names the error."""
    from app.services import sync_notifier

    monkeypatch.setattr(
        sync_notifier, "_alert_webhook_url", lambda: "https://hooks.example.com/x"
    )

    calls = []

    class _Resp:
        status_code = 200

    def _fake_post(url, **kw):
        calls.append((url, kw))
        return _Resp()

    monkeypatch.setattr(sync_notifier.httpx, "post", _fake_post)

    sync_notifier.notify_sync_failure(
        failed_tables=[], fatal=RuntimeError("disk full")
    )

    assert len(calls) == 1
    url, kw = calls[0]
    assert url == "https://hooks.example.com/x"
    payload = kw["json"]
    assert "text" in payload
    assert "disk full" in payload["text"]


def test_notify_per_table_errors_listed(monkeypatch):
    """Per-table errors → the POST text lists each failed table + its error."""
    from app.services import sync_notifier

    monkeypatch.setattr(
        sync_notifier, "_alert_webhook_url", lambda: "https://hooks.example.com/x"
    )

    calls = []

    class _Resp:
        status_code = 200

    monkeypatch.setattr(
        sync_notifier.httpx,
        "post",
        lambda url, **kw: calls.append((url, kw)) or _Resp(),
    )

    sync_notifier.notify_sync_failure(
        failed_tables=[
            {"table": "orders", "error": "COPY failed"},
            {"table": "users", "error": "budget exceeded"},
        ],
        fatal=None,
    )

    assert len(calls) == 1
    text = calls[0][1]["json"]["text"]
    assert "orders" in text
    assert "COPY failed" in text
    assert "users" in text
    assert "budget exceeded" in text


def test_notify_empty_inputs_no_post(monkeypatch):
    """Nothing failed (no fatal, no table errors) → no POST even with a URL set."""
    from app.services import sync_notifier

    monkeypatch.setattr(
        sync_notifier, "_alert_webhook_url", lambda: "https://hooks.example.com/x"
    )

    calls = []
    monkeypatch.setattr(
        sync_notifier.httpx, "post", lambda *a, **kw: calls.append((a, kw))
    )

    sync_notifier.notify_sync_failure(failed_tables=[], fatal=None)
    assert calls == []


def test_notify_webhook_raising_is_swallowed(monkeypatch):
    """A webhook POST that raises must NOT propagate — best-effort contract."""
    from app.services import sync_notifier

    monkeypatch.setattr(
        sync_notifier, "_alert_webhook_url", lambda: "https://hooks.example.com/x"
    )

    def _boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(sync_notifier.httpx, "post", _boom)

    # Must not raise.
    sync_notifier.notify_sync_failure(
        failed_tables=[{"table": "t", "error": "e"}], fatal=None
    )


# ── post_webhook URL redaction (#648 review: webhook URL is a credential) ──

def test_redact_url_strips_path_and_query():
    from services.telegram_bot.sender import _redact_url

    secret = "https://hooks.slack.com/services/T00/B11/zzSECRETtoken?x=1"
    out = _redact_url(secret)
    assert out == "https://hooks.slack.com"
    assert "zzSECRET" not in out
    assert "B11" not in out


def test_redact_url_unparseable_is_safe():
    from services.telegram_bot.sender import _redact_url

    assert _redact_url("not a url") == "<unparseable>"


def test_post_webhook_non_2xx_does_not_log_full_url(monkeypatch, caplog):
    import logging

    import services.telegram_bot.sender as sender

    class _Resp:
        status_code = 500

    monkeypatch.setattr(sender.httpx, "post", lambda *a, **k: _Resp())
    secret = "https://hooks.slack.com/services/T00/B11/zzSECRETtoken"
    with caplog.at_level(logging.ERROR):
        assert sender.post_webhook(secret, {"text": "hi"}) is False
    assert "zzSECRET" not in caplog.text
    assert "B11" not in caplog.text


def test_post_webhook_exception_does_not_log_full_url(monkeypatch, caplog):
    import logging

    import services.telegram_bot.sender as sender

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(sender.httpx, "post", _boom)
    secret = "https://hooks.slack.com/services/T00/B11/zzSECRETtoken"
    with caplog.at_level(logging.ERROR):
        assert sender.post_webhook(secret, {"text": "hi"}) is False
    assert "zzSECRET" not in caplog.text
