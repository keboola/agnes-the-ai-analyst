"""Tests for `services.telegram_bot.runner` username validation.

Issue #84: the runner shells out via `sudo -u <username>`. Without an
input gate, a username controlled by an attacker (via tampering with
the linked-users JSON, or via an upstream caller that doesn't validate)
could carry sudo flags or shell metacharacters. Every value flowing
into `subprocess.run([..., "-u", username, ...])` must match a
POSIX-conservative shape; bad shapes are refused before the subprocess
fires.
"""

from unittest.mock import patch

from services.telegram_bot.runner import _USERNAME_RE, run_user_script


def test_username_regex_accepts_normal_usernames():
    for u in ("alice", "bob42", "data_ops", "svc-agnes", "_system"):
        assert _USERNAME_RE.match(u), u


def test_username_regex_rejects_obvious_attacks():
    bad = [
        "-u",                 # sudo flag
        "--shell=/bin/bash",  # GNU long flag
        "alice; rm -rf /",    # shell metachar
        "alice && id",
        "alice|cat /etc/shadow",
        "alice$IFS",
        "1starts_with_digit",
        "alice/with/slash",
        "alice with space",
        "",                    # empty
        "a" * 33,              # too long
    ]
    for u in bad:
        assert not _USERNAME_RE.match(u), u


def test_run_user_script_refuses_bad_username_without_subprocess():
    """If validation refuses the username, subprocess.run must not fire.

    Pre-fix, a tampered telegram_users.json with `username = "-u root"`
    would have sudo'd as root via flag injection. The fix has the runner
    short-circuit to None before any subprocess call.
    """
    with patch("services.telegram_bot.runner.subprocess.run") as run_mock:
        result = run_user_script("-u", "ok_script.py")
    assert result is None
    run_mock.assert_not_called()


def test_run_user_script_refuses_bad_script_name_without_subprocess():
    """Existing guard at L24 rejects non-.py scripts; verify it still does
    after the new username gate so a valid username + bad script combo
    doesn't slip through and run."""
    with patch("services.telegram_bot.runner.subprocess.run") as run_mock:
        result = run_user_script("alice", "not_python.sh")
    assert result is None
    run_mock.assert_not_called()
