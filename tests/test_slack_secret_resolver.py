"""Unit tests for the env > vault > none Slack secret resolver."""
from __future__ import annotations

import pytest

from services.slack_bot.secrets import SLACK_SECRET_NAMES, slack_secret


def test_env_wins_over_vault(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
    # Even if the vault would return something, env must win and the vault
    # must not be consulted. Make the vault raise to prove it isn't called.
    def _boom():
        raise AssertionError("vault must not be consulted when env is set")

    monkeypatch.setattr(
        "src.repositories.system_secrets_repo", _boom, raising=False
    )
    assert slack_secret("SLACK_BOT_TOKEN") == "xoxb-from-env"


def test_vault_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    class _Repo:
        def get(self, name):
            return "xapp-from-vault" if name == "SLACK_APP_TOKEN" else None

    monkeypatch.setattr("src.repositories.system_secrets_repo", lambda: _Repo())
    assert slack_secret("SLACK_APP_TOKEN") == "xapp-from-vault"


def test_none_when_neither(monkeypatch):
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

    class _Repo:
        def get(self, name):
            return None

    monkeypatch.setattr("src.repositories.system_secrets_repo", lambda: _Repo())
    assert slack_secret("SLACK_SIGNING_SECRET") is None


def test_non_allow_listed_name_raises(monkeypatch):
    with pytest.raises(ValueError):
        slack_secret("DATABASE_URL")


def test_vault_failure_is_swallowed(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("src.repositories.system_secrets_repo", _boom)
    assert slack_secret("SLACK_BOT_TOKEN") is None


def test_allow_list_contents():
    assert SLACK_SECRET_NAMES == (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
    )
