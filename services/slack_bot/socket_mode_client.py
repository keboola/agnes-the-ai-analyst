"""Optional Socket Mode inbound transport for the Slack bot.

A `SocketModeDispatcher` owns one Socket Mode WebSocket (xapp- token):
connect, ack each envelope FIRST (<3s), then schedule the same
`dispatch_event` the HTTP webhook uses — no event-shape translation,
because `SocketModeRequest.payload` for `events_api` is byte-identical
to the HTTP webhook body. Reconnect/backoff is handled by slack_sdk's
SocketModeClient; we just own the lifecycle.

`slack_sdk` is an OPTIONAL dependency (`pip install '.[slack-socket]'`)
imported lazily inside `start()` so HTTP-only deployments never need it.
"""
from __future__ import annotations

import logging

from services.slack_bot.events import _run_logged, _schedule, dispatch_event

logger = logging.getLogger(__name__)


def _slack_sdk_importable() -> bool:
    """True iff the optional slack_sdk dep is installed. Isolated so the
    preflight gate is unit-testable without the package present."""
    try:
        import slack_sdk  # noqa: F401
        return True
    except ImportError:
        return False


def socket_mode_preflight(
    *, workers: int, app_token: str, bot_token: str,
) -> tuple[bool, str]:
    """Fail-closed gate for the socket transport.

    Returns (ok, reason). On any failure the lifespan caller logs `reason`
    and disables Slack — it never starts a dead WS or crashes the app.
    """
    if workers > 1:
        return False, (
            "Socket Mode requires a single worker (one WS; N workers "
            "fracture dedup) but UVICORN_WORKERS > 1"
        )
    if not app_token:
        return False, "SLACK_APP_TOKEN missing (required for Socket Mode)"
    if not app_token.startswith("xapp-"):
        return False, "SLACK_APP_TOKEN must be an app-level token (xapp- prefix)"
    if not bot_token:
        return False, "SLACK_BOT_TOKEN missing (required for Socket Mode)"
    if not bot_token.startswith("xoxb-"):
        return False, "SLACK_BOT_TOKEN must be a bot token (xoxb- prefix)"
    if not _slack_sdk_importable():
        return False, (
            "Socket Mode requires the 'slack-socket' extra — install with: "
            "pip install '.[slack-socket]'"
        )
    return True, ""


class SocketModeImportError(RuntimeError):
    """Raised when transport=socket but slack_sdk is not importable."""


class SocketModeDispatcher:
    def __init__(self, *, app, app_token: str, bot_token: str) -> None:
        self._app = app
        self._app_token = app_token
        self._bot_token = bot_token
        self._client = None  # slack_sdk SocketModeClient, built in start()

    async def _on_request(self, client, req) -> None:
        # 1. ACK FIRST (<3s) so Slack never retries / disconnects.
        # Lazy + cached: _on_request is only ever registered as a listener inside
        # start(), after slack_sdk imported successfully — so this never hits ImportError.
        from slack_sdk.socket_mode.response import SocketModeResponse

        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )
        # 2. Funnel into the SAME dispatcher the HTTP webhook uses. No
        #    payload translation — req.payload["event"] is byte-identical
        #    to the HTTP body's payload["event"]. on_failure=None here for
        #    the same reason as the HTTP call site: _handle_dm emits its own
        #    inline replies (the recovery seam is used by later phases).
        if req.type == "events_api" and req.payload.get("type") == "event_callback":
            _schedule(_run_logged(dispatch_event(self._app, req.payload["event"])))
        # slash_commands / interactive routing arrives in later phases.

    async def start(self) -> None:
        """Connect the WS. Lazy-imports slack_sdk; ImportError -> actionable
        fail-closed error the lifespan gate turns into 'Slack disabled'."""
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
        except ImportError as e:  # noqa: F841
            raise SocketModeImportError(
                "chat.slack.transport=socket requires the 'slack-socket' "
                "extra — install with: pip install '.[slack-socket]'"
            ) from e

        self._client = SocketModeClient(
            app_token=self._app_token,
            # web_client=None → slack_sdk builds an UNAUTHENTICATED AsyncWebClient.
            # Fine for Phase 0: inbound event receipt only; outbound replies
            # resolve SLACK_BOT_TOKEN via slack_secret (env > vault) in sender.py.
            # self._bot_token is collected now
            # and gets wired here (AsyncWebClient(token=self._bot_token)) when Web API
            # calls are needed in a later phase.
            web_client=None,
        )
        self._client.socket_mode_request_listeners.append(self._on_request)
        await self._client.connect()
        logger.info("Slack Socket Mode connected")

    async def stop(self) -> None:
        """Clean shutdown of the WS at app teardown."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.exception("Slack Socket Mode disconnect failed (non-fatal)")
            finally:
                self._client = None
                logger.info("Slack Socket Mode disconnected")
