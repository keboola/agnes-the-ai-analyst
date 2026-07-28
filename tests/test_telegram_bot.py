"""Tests for Telegram bot message handlers."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

# bot.py creates a FileHandler at module-level import. Ensure the
# directory exists so import doesn't fail in CI environments.
_data_dir = os.environ.get("DATA_DIR", "/data")
os.makedirs(os.path.join(_data_dir, "notifications"), exist_ok=True)

try:
    from services.telegram_bot.bot import handle_message  # noqa: F401

    _BOT_AVAILABLE = True
except Exception:
    _BOT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _BOT_AVAILABLE,
    reason="services.telegram_bot.bot could not be imported (missing log dir or dependency)",
)


def _make_message(text: str, chat_id: int = 10) -> dict:
    return {"chat": {"id": chat_id}, "text": text}


def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


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


class TestPollingLease:
    """Wave-2C task 3: the Telegram long-poll loop runs behind the
    `telegram-poll` leader lease (app/coordination/leases.py) so only one
    replica of this standalone service polls Telegram at a time."""

    def test_start_polling_launches_a_cancellable_task(self):
        """`_start_polling` (the lease's `start` callback) must launch
        `polling_loop` as a background task rather than awaiting it inline
        — a `get_updates` long-poll blocks for up to POLL_TIMEOUT_SECONDS,
        and only task cancellation interrupts that promptly."""
        import services.telegram_bot.bot as bot_mod

        started = asyncio.Event()

        async def fake_polling_loop():
            started.set()
            await asyncio.sleep(10)  # would hang the test if not cancelled

        with patch.object(bot_mod, "polling_loop", fake_polling_loop):

            async def _run():
                await bot_mod._start_polling()
                await asyncio.wait_for(started.wait(), timeout=1)
                assert bot_mod._poll_task is not None
                assert not bot_mod._poll_task.done()
                await bot_mod._stop_polling()

            _run_and_assert = _run()
            asyncio.run(_run_and_assert)

    def test_stop_polling_cancels_the_task_and_clears_state(self):
        import services.telegram_bot.bot as bot_mod

        async def fake_polling_loop():
            await asyncio.sleep(10)

        with patch.object(bot_mod, "polling_loop", fake_polling_loop):

            async def _run():
                await bot_mod._start_polling()
                task = bot_mod._poll_task
                await bot_mod._stop_polling()
                assert task.cancelled()
                assert bot_mod._poll_task is None

            asyncio.run(_run())

    def test_stop_polling_is_a_noop_when_never_started(self):
        import services.telegram_bot.bot as bot_mod

        bot_mod._poll_task = None
        asyncio.run(bot_mod._stop_polling())  # must not raise
        assert bot_mod._poll_task is None

    def test_run_polling_with_lease_routes_through_run_with_lease(self):
        """Wiring-level check: `run_polling_with_lease` must call
        `run_with_lease` with the `telegram-poll` lease name and the
        module's `_start_polling`/`_stop_polling` callbacks — not run the
        poll loop directly — so a shared (redis) coordination backend
        actually enforces single-replica polling."""
        import app.coordination.leases as leases_mod
        import services.telegram_bot.bot as bot_mod

        calls: list[dict] = []

        async def fake_run_with_lease(name, holder_id, *, ttl_s, start, stop):
            calls.append({"name": name, "holder_id": holder_id, "ttl_s": ttl_s, "start": start, "stop": stop})

        with patch.object(leases_mod, "run_with_lease", fake_run_with_lease):
            asyncio.run(bot_mod.run_polling_with_lease())

        assert len(calls) == 1
        assert calls[0]["name"] == "telegram-poll"
        assert calls[0]["ttl_s"] == 15
        assert calls[0]["holder_id"]
        assert calls[0]["start"] is bot_mod._start_polling
        assert calls[0]["stop"] is bot_mod._stop_polling
