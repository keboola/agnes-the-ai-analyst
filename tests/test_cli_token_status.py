"""Tests for cli/token_status.py — proactive PAT re-mint nudge (#477).

Design decision (Option 3, no new server primitives): the CLI decodes the
locally-stored PAT's `exp` claim (HS256 JWTs are client-decodable without
the signing secret) and nudges the analyst to re-run `agnes auth login`
when the stored token is inside the renewal window. No refresh-token
grant, no TTL change to the 90-day PAT.

Covers: exp-claim parsing (valid / garbage / missing), the renewal window
+ `AGNES_TOKEN_RENEW_DAYS` env override (incl. `0` disables), the
once-per-day marker, and the `agnes auth whoami` / `agnes update` report
surfacing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from typer.testing import CliRunner

from cli import token_status as ts
from cli.main import app

runner = CliRunner()


def _make_token(*, exp: datetime | None = None, email: str = "alice@example.com", **extra) -> str:
    payload = {"email": email, **extra}
    if exp is not None:
        payload["exp"] = int(exp.timestamp())
    return pyjwt.encode(payload, "unused-secret", algorithm="HS256")


@pytest.fixture(autouse=True)
def _tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AGNES_TOKEN_RENEW_DAYS", raising=False)
    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")
    yield tmp_path


# --- decode_expiry / days_remaining -----------------------------------------


def test_decode_expiry_valid_token():
    exp = datetime(2030, 1, 1, tzinfo=timezone.utc)
    token = _make_token(exp=exp)
    assert ts.decode_expiry(token) == exp


def test_decode_expiry_garbage_token_returns_none():
    assert ts.decode_expiry("not-a-jwt-at-all") is None


def test_decode_expiry_missing_exp_claim_returns_none():
    token = _make_token(exp=None)  # no exp claim at all
    assert ts.decode_expiry(token) is None


def test_days_remaining_computes_from_now():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = _make_token(exp=now + timedelta(days=5))
    left = ts.days_remaining(token, now=now)
    assert left is not None
    assert 4.9 < left < 5.1


def test_days_remaining_negative_when_expired():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    token = _make_token(exp=now - timedelta(days=2))
    left = ts.days_remaining(token, now=now)
    assert left is not None
    assert left < 0


def test_days_remaining_none_when_expiry_unknown():
    token = _make_token(exp=None)
    assert ts.days_remaining(token) is None


# --- get_renew_days ----------------------------------------------------------


def test_get_renew_days_default_is_seven(monkeypatch):
    monkeypatch.delenv("AGNES_TOKEN_RENEW_DAYS", raising=False)
    assert ts.get_renew_days() == 7


def test_get_renew_days_reads_env(monkeypatch):
    monkeypatch.setenv("AGNES_TOKEN_RENEW_DAYS", "14")
    assert ts.get_renew_days() == 14


def test_get_renew_days_zero_disables(monkeypatch):
    monkeypatch.setenv("AGNES_TOKEN_RENEW_DAYS", "0")
    assert ts.get_renew_days() == 0


def test_get_renew_days_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AGNES_TOKEN_RENEW_DAYS", "not-a-number")
    assert ts.get_renew_days() == 7


# --- maybe_print_nudge --------------------------------------------------------


def test_nudge_fires_inside_window(monkeypatch, tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = _make_token(exp=now + timedelta(days=3))
    monkeypatch.setattr(ts, "get_token", lambda: token)

    fired = ts.maybe_print_nudge(now=now)
    assert fired is True


def test_nudge_prints_expected_text(monkeypatch, capsys):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = _make_token(exp=now + timedelta(days=3))
    monkeypatch.setattr(ts, "get_token", lambda: token)

    ts.maybe_print_nudge(now=now)
    captured = capsys.readouterr()
    assert "expires in 3 day" in captured.err
    assert "agnes auth login" in captured.err


def test_nudge_silent_outside_window(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = _make_token(exp=now + timedelta(days=30))
    monkeypatch.setattr(ts, "get_token", lambda: token)

    assert ts.maybe_print_nudge(now=now) is False


def test_nudge_silent_when_no_token(monkeypatch):
    monkeypatch.setattr(ts, "get_token", lambda: None)
    assert ts.maybe_print_nudge() is False


def test_nudge_silent_when_expiry_unknown(monkeypatch):
    token = _make_token(exp=None)
    monkeypatch.setattr(ts, "get_token", lambda: token)
    assert ts.maybe_print_nudge() is False


def test_nudge_disabled_via_env_zero(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = _make_token(exp=now + timedelta(days=1))
    monkeypatch.setattr(ts, "get_token", lambda: token)
    monkeypatch.setenv("AGNES_TOKEN_RENEW_DAYS", "0")

    assert ts.maybe_print_nudge(now=now) is False


def test_nudge_respects_once_per_day_marker(monkeypatch):
    now = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
    token = _make_token(exp=now + timedelta(days=3))
    monkeypatch.setattr(ts, "get_token", lambda: token)

    assert ts.maybe_print_nudge(now=now) is True
    # Same day, different hour — must stay silent.
    later_same_day = now + timedelta(hours=6)
    assert ts.maybe_print_nudge(now=later_same_day) is False
    # Next day — re-arms.
    next_day = now + timedelta(days=1, hours=1)
    assert ts.maybe_print_nudge(now=next_day) is True


def test_nudge_marker_file_persists_last_nudge_date(monkeypatch, tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = _make_token(exp=now + timedelta(days=1))
    monkeypatch.setattr(ts, "get_token", lambda: token)

    ts.maybe_print_nudge(now=now)
    marker = tmp_path / "token_nudge_state.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["last_nudge_date"] == "2026-01-01"


def test_nudge_never_raises_on_garbage_state(monkeypatch, tmp_path):
    (tmp_path / "token_nudge_state.json").write_text("not json {", encoding="utf-8")
    monkeypatch.setattr(ts, "get_token", lambda: _make_token(exp=datetime.now(timezone.utc) + timedelta(days=1)))
    # Must not raise despite the corrupt marker file.
    ts.maybe_print_nudge()


# --- format_status_line (used by `agnes auth whoami` / `agnes update`) -------


def test_format_status_line_valid_token():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = _make_token(exp=now + timedelta(days=10))
    line = ts.format_status_line(token, now=now)
    assert "2026-01-11" in line
    assert "10 day" in line


def test_format_status_line_expired_token():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    token = _make_token(exp=now - timedelta(days=2))
    line = ts.format_status_line(token, now=now)
    assert "expired" in line


def test_format_status_line_unknown_expiry():
    token = _make_token(exp=None)
    line = ts.format_status_line(token)
    assert "unknown" in line


# --- root-callback wiring (cli/main.py) --------------------------------------


def test_root_callback_prints_nudge_on_non_quiet_command(monkeypatch, tmp_path):
    real_now = datetime.now(timezone.utc)
    token = _make_token(exp=real_now + timedelta(days=2))
    monkeypatch.setattr("cli.token_status.get_token", lambda: token)
    monkeypatch.setattr("sys.argv", ["agnes", "catalog", "--help"])

    result = runner.invoke(app, ["catalog", "--help"])
    assert "expires in" in result.stderr
    assert "agnes auth login" in result.stderr


def test_root_callback_silent_under_quiet(monkeypatch, tmp_path):
    real_now = datetime.now(timezone.utc)
    token = _make_token(exp=real_now + timedelta(days=2))
    monkeypatch.setattr("cli.token_status.get_token", lambda: token)
    monkeypatch.setattr("sys.argv", ["agnes", "pull", "--quiet"])

    result = runner.invoke(app, ["pull", "--quiet"])
    assert "expires in" not in (result.stderr or "")
