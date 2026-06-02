import hashlib
import hmac
import time

from services.slack_bot.sigverify import verify_slack_signature


def test_accepts_valid():
    secret = "s3cret"
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    base = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_signature(secret, ts, sig, body) is True


def test_rejects_old_timestamp():
    secret = "s3cret"
    body = b"{}"
    ts = str(int(time.time()) - 600)  # 10 min old
    base = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_signature(secret, ts, sig, body) is False


def test_rejects_bad_sig():
    assert verify_slack_signature("s", "0", "v0=bad", b"{}") is False
