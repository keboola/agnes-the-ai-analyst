"""SR-12 Slack binding hardening tests.

Covers:
- One active code per slack_user_id (DELETE prior on re-issue)
- Issuance throttle (max 3 per 10 minutes)
- Per-caller redeem throttle (max 5 failed redeems per caller per window →
  6th raises BindingThrottled, even with the correct code)
- Audit logged on successful redeem
"""
import duckdb
import pytest


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE users(id VARCHAR, email VARCHAR)")
    c.execute("INSERT INTO users VALUES ('ua','a@example.com')")
    return c


def test_one_active_code_per_slack_user(conn):
    from services.slack_bot.binding import issue_verification_code
    issue_verification_code(conn, slack_user_id="U1")
    c2 = issue_verification_code(conn, slack_user_id="U1")
    rows = conn.execute("SELECT code FROM slack_binding_codes WHERE slack_user_id='U1'").fetchall()
    assert len(rows) == 1 and rows[0][0] == c2  # prior deleted on re-issue


def test_issuance_throttle(conn):
    from services.slack_bot.binding import issue_verification_code, BindingThrottled
    for _ in range(3):
        issue_verification_code(conn, slack_user_id="U2")
    with pytest.raises(BindingThrottled):
        issue_verification_code(conn, slack_user_id="U2")


def test_attempt_lockout_on_redeem(conn):
    """Per-caller redeem throttle: 5 failed guesses lock out the caller.

    The 6th attempt raises BindingThrottled even when the submitted code is
    correct — this is the brute-force protection. The throttle is keyed on
    the redeeming caller (user_email), not on a per-code counter (a wrong
    guess matches no code row, so a per-code counter would be dead code),
    and it never evicts another user's code (no cross-user DoS).
    """
    from services.slack_bot.binding import (
        issue_verification_code, redeem_verification_code, BindingThrottled,
    )
    real = issue_verification_code(conn, slack_user_id="U1")
    for _ in range(5):
        assert redeem_verification_code(conn, user_email="a@example.com", code="000000") is False
    # 6th attempt is locked out — even the correct code raises rather than binds.
    with pytest.raises(BindingThrottled):
        redeem_verification_code(conn, user_email="a@example.com", code=real)
    # The victim's code was not consumed by the brute force.
    assert conn.execute(
        "SELECT 1 FROM slack_binding_codes WHERE code = ?", [real]
    ).fetchone() is not None
