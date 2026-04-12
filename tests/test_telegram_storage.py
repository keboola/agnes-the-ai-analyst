"""Tests for Telegram bot storage (user linking and verification codes)."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def storage_paths(tmp_path, monkeypatch):
    """Redirect storage file paths to tmp_path."""
    users_file = str(tmp_path / "telegram_users.json")
    codes_file = str(tmp_path / "pending_codes.json")

    import services.telegram_bot.config as cfg
    monkeypatch.setattr(cfg, "TELEGRAM_USERS_FILE", users_file)
    monkeypatch.setattr(cfg, "PENDING_CODES_FILE", codes_file)
    # Also patch in the storage module namespace
    import services.telegram_bot.storage as storage_mod
    monkeypatch.setattr(storage_mod, "config", cfg)

    return {"users": users_file, "codes": codes_file}


class TestUserLinking:
    def test_link_user_and_get_chat_id(self, storage_paths):
        from services.telegram_bot.storage import get_chat_id, link_user
        link_user("alice", 100)
        assert get_chat_id("alice") == 100

    def test_get_chat_id_unknown_user_returns_none(self, storage_paths):
        from services.telegram_bot.storage import get_chat_id
        assert get_chat_id("nobody") is None

    def test_unlink_user_returns_true_when_linked(self, storage_paths):
        from services.telegram_bot.storage import link_user, unlink_user
        link_user("bob", 200)
        result = unlink_user("bob")
        assert result is True

    def test_unlink_user_removes_entry(self, storage_paths):
        from services.telegram_bot.storage import get_chat_id, link_user, unlink_user
        link_user("carol", 300)
        unlink_user("carol")
        assert get_chat_id("carol") is None

    def test_unlink_user_returns_false_when_not_linked(self, storage_paths):
        from services.telegram_bot.storage import unlink_user
        result = unlink_user("ghost")
        assert result is False

    def test_link_multiple_users(self, storage_paths):
        from services.telegram_bot.storage import get_chat_id, link_user
        link_user("user1", 111)
        link_user("user2", 222)
        assert get_chat_id("user1") == 111
        assert get_chat_id("user2") == 222


class TestVerificationCodes:
    def test_create_verification_code_returns_string(self, storage_paths):
        from services.telegram_bot.storage import create_verification_code
        code = create_verification_code(chat_id=42)
        assert isinstance(code, str)
        assert len(code) > 0

    def test_verify_code_returns_chat_id(self, storage_paths):
        from services.telegram_bot.storage import create_verification_code, verify_code
        code = create_verification_code(chat_id=55)
        result = verify_code(code)
        assert result == 55

    def test_code_consumed_after_first_verify(self, storage_paths):
        from services.telegram_bot.storage import create_verification_code, verify_code
        code = create_verification_code(chat_id=77)
        verify_code(code)
        # Second call must return None (code consumed)
        result = verify_code(code)
        assert result is None

    def test_verify_invalid_code_returns_none(self, storage_paths):
        from services.telegram_bot.storage import verify_code
        result = verify_code("000000")
        assert result is None

    def test_create_code_replaces_existing_for_same_chat_id(self, storage_paths):
        from services.telegram_bot.storage import create_verification_code, verify_code
        old_code = create_verification_code(chat_id=88)
        new_code = create_verification_code(chat_id=88)
        # Old code should be gone
        assert verify_code(old_code) is None
        # New code should work
        assert verify_code(new_code) == 88

    def test_expired_code_not_valid(self, storage_paths):
        """Manually write an expired code and verify it returns None."""
        import services.telegram_bot.config as cfg
        from services.telegram_bot.storage import verify_code

        # Write a code that expired long ago
        expired_data = {
            "123456": {
                "chat_id": 99,
                "created_at": time.time() - cfg.CODE_TTL_SECONDS - 1,
            }
        }
        Path(cfg.PENDING_CODES_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.PENDING_CODES_FILE, "w") as f:
            json.dump(expired_data, f)

        result = verify_code("123456")
        assert result is None
