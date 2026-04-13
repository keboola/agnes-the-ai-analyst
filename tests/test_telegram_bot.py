"""Tests for Telegram bot message handlers."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


def _make_message(text: str, chat_id: int = 10) -> dict:
    return {"chat": {"id": chat_id}, "text": text}


def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestHandleMessage:
    def test_start_unlinked_user_generates_verification_code(self):
        """'/start' for an unlinked user generates and sends a verification code."""
        with (
            patch("services.telegram_bot.bot.get_username_by_chat_id", return_value=None),
            patch("services.telegram_bot.bot.create_verification_code", return_value="123456") as mock_code,
            patch("services.telegram_bot.bot.send_message", new_callable=AsyncMock) as mock_send,
        ):
            from services.telegram_bot.bot import handle_message
            _run(handle_message(_make_message("/start", chat_id=10)))
            mock_code.assert_called_once_with(10)
            mock_send.assert_called_once()
            sent_text = mock_send.call_args[0][1]
            assert "123456" in sent_text

    def test_start_already_linked_user_no_code(self):
        """'/start' for an already-linked user does NOT generate a new code."""
        with (
            patch("services.telegram_bot.bot.get_username_by_chat_id", return_value="alice"),
            patch("services.telegram_bot.bot.create_verification_code") as mock_code,
            patch("services.telegram_bot.bot.send_message", new_callable=AsyncMock),
        ):
            from services.telegram_bot.bot import handle_message
            _run(handle_message(_make_message("/start", chat_id=10)))
            mock_code.assert_not_called()

    def test_help_returns_help_text(self):
        """'/help' sends a message containing help information."""
        with patch("services.telegram_bot.bot.send_message", new_callable=AsyncMock) as mock_send:
            from services.telegram_bot.bot import handle_message
            _run(handle_message(_make_message("/help", chat_id=20)))
            mock_send.assert_called_once()
            sent_text = mock_send.call_args[0][1]
            assert "/start" in sent_text
            assert "/help" in sent_text

    def test_unknown_command_sends_unknown_response(self):
        """An unknown command sends an 'Unknown command' reply."""
        with patch("services.telegram_bot.bot.send_message", new_callable=AsyncMock) as mock_send:
            from services.telegram_bot.bot import handle_message
            _run(handle_message(_make_message("/foobar", chat_id=30)))
            mock_send.assert_called_once()
            sent_text = mock_send.call_args[0][1]
            assert "Unknown" in sent_text or "unknown" in sent_text

    def test_message_with_no_chat_id_is_ignored(self):
        """A message without a chat id does nothing."""
        with patch("services.telegram_bot.bot.send_message", new_callable=AsyncMock) as mock_send:
            from services.telegram_bot.bot import handle_message
            _run(handle_message({"text": "/help"}))
            mock_send.assert_not_called()

    def test_whoami_linked_user_sends_username(self):
        """'/whoami' for a linked user sends the username."""
        with (
            patch("services.telegram_bot.bot.get_username_by_chat_id", return_value="dave"),
            patch("services.telegram_bot.bot.send_message", new_callable=AsyncMock) as mock_send,
        ):
            from services.telegram_bot.bot import handle_message
            _run(handle_message(_make_message("/whoami", chat_id=40)))
            mock_send.assert_called_once()
            sent_text = mock_send.call_args[0][1]
            assert "dave" in sent_text
