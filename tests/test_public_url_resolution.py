"""``get_public_url`` resolver and the ``app.state.public_url`` wiring.

Resolution order is env > yaml > ``""`` (unset), mirroring
:func:`app.instance_config.get_home_route`. ``PUBLIC_URL`` is the
Terraform-overrideable knob — operators set it on the VM without forking
instance.yaml.

Regression guard for the Slack magic-link bug: the bot (Socket Mode) reads
``app.state.public_url`` to mint *absolute* ``/slack/bind`` links, but nothing
ever assigned it, so every link came out root-relative (``/slack/bind?code=…``)
and was not clickable from Slack. These tests pin the resolver precedence and
the boot-time wiring so it can't silently regress to unset again.
"""

from __future__ import annotations

import tempfile

import pytest


@pytest.fixture
def fresh_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        monkeypatch.delenv("PUBLIC_URL", raising=False)
        # instance.yaml cache is process-global; drop it so each test reads
        # a clean config (no server.public_url leaking across tests).
        from app.instance_config import reset_cache

        reset_cache()
        yield tmp
        reset_cache()


def test_default_public_url_is_empty(fresh_env):
    """Unset PUBLIC_URL and no server.public_url → empty string."""
    from app.instance_config import get_public_url

    assert get_public_url() == ""


def test_env_overrides_default(fresh_env, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    from app.instance_config import get_public_url

    assert get_public_url() == "https://agnes.example.com"


def test_trailing_slash_is_stripped(fresh_env, monkeypatch):
    """A trailing slash would double up when callers append ``/slack/bind``."""
    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com/")
    from app.instance_config import get_public_url

    assert get_public_url() == "https://agnes.example.com"


def test_whitespace_is_stripped(fresh_env, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "  https://agnes.example.com  ")
    from app.instance_config import get_public_url

    assert get_public_url() == "https://agnes.example.com"


def test_app_state_public_url_is_wired_on_boot(fresh_env, monkeypatch):
    """The actual bug: booting the app must populate ``app.state.public_url``
    from PUBLIC_URL so the Slack bot's request-less handlers can build
    absolute links. Before the fix this attribute was never set."""
    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")

    from fastapi.testclient import TestClient
    from app.main import app

    # Entering the TestClient context runs the lifespan startup, which is
    # where app.state.public_url is resolved and stashed.
    with TestClient(app, follow_redirects=False):
        assert app.state.public_url == "https://agnes.example.com"


def test_bind_prompt_is_absolute_when_public_url_set(fresh_env, monkeypatch):
    """End-to-end on the message text: with PUBLIC_URL set, the bot's bind
    prompt contains an absolute, clickable magic link."""
    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")

    from app.instance_config import get_public_url
    from services.slack_bot.binding import bind_prompt

    msg = bind_prompt(get_public_url(), "123456")
    assert "https://agnes.example.com/slack/bind?code=123456" in msg


def test_bind_prompt_degrades_to_relative_when_unset(fresh_env):
    """With PUBLIC_URL unset the link is root-relative — degraded but not a
    crash. Documents the fallback contract callers rely on."""
    from app.instance_config import get_public_url
    from services.slack_bot.binding import bind_prompt

    msg = bind_prompt(get_public_url(), "123456")
    assert "/slack/bind?code=123456" in msg
