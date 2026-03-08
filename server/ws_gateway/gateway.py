"""Main WebSocket Gateway with TCP WebSocket server and Unix socket HTTP dispatch."""

import asyncio
import json
import logging
import os
from collections import defaultdict

import aiohttp
from aiohttp import web, WSMsgType

from .auth import validate_token
from .config import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_MISSED,
    MAX_CONNECTIONS_PER_USER,
    WS_DISPATCH_SOCKET,
    WS_GATEWAY_HOST,
    WS_GATEWAY_PORT,
)

logger = logging.getLogger(__name__)

# Global connection registry: username -> list of WebSocket responses
connections: dict[str, list[web.WebSocketResponse]] = defaultdict(list)


def _total_connections() -> int:
    """Return total number of active WebSocket connections."""
    return sum(len(ws_list) for ws_list in connections.values())


def _remove_connection(username: str, ws: web.WebSocketResponse) -> None:
    """Remove a WebSocket connection from the registry."""
    if username in connections:
        try:
            connections[username].remove(ws)
        except ValueError:
            pass
        if not connections[username]:
            del connections[username]


async def _heartbeat_loop(
    username: str, ws: web.WebSocketResponse
) -> None:
    """Send periodic pings and disconnect on missed pongs.

    NOTE: This task may be cancelled and restarted when the client sends a pong.
    Connection cleanup is handled by ws_handler's finally block, NOT here.
    """
    missed = 0
    try:
        while not ws.closed:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            if ws.closed:
                break
            try:
                await ws.send_json({"type": "ping"})
                missed += 1
            except ConnectionResetError:
                break

            if missed >= HEARTBEAT_TIMEOUT_MISSED:
                logger.warning(
                    "User %s missed %d heartbeats, disconnecting",
                    username,
                    missed,
                )
                await ws.close()
                break
    except asyncio.CancelledError:
        pass


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle incoming WebSocket connections on the TCP server."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    username: str | None = None
    heartbeat_task: asyncio.Task | None = None

    try:
        # Wait for auth message
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=10.0)
        except asyncio.TimeoutError:
            await ws.send_json({"type": "auth_error", "message": "Auth timeout"})
            await ws.close()
            return ws

        if msg.type != WSMsgType.TEXT:
            await ws.close()
            return ws

        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            await ws.send_json({"type": "auth_error", "message": "Invalid JSON"})
            await ws.close()
            return ws

        if data.get("type") != "auth" or "token" not in data:
            await ws.send_json(
                {"type": "auth_error", "message": "Expected auth message with token"}
            )
            await ws.close()
            return ws

        payload = validate_token(data["token"])
        if payload is None:
            await ws.send_json({"type": "auth_error", "message": "Invalid token"})
            await ws.close()
            return ws

        username = payload["sub"]

        # Enforce per-user connection limit
        if len(connections[username]) >= MAX_CONNECTIONS_PER_USER:
            await ws.send_json(
                {"type": "auth_error", "message": "Too many connections"}
            )
            await ws.close()
            return ws

        connections[username].append(ws)
        await ws.send_json({"type": "auth_ok", "username": username})
        logger.info("User %s connected (total: %d)", username, _total_connections())

        # Start heartbeat
        heartbeat_task = asyncio.create_task(_heartbeat_loop(username, ws))

        # Read loop - handle pong responses and ignore other messages
        async for msg in ws:
            logger.debug("User %s msg type=%s", username, msg.type)
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "pong":
                    # Reset missed counter by cancelling and restarting heartbeat
                    heartbeat_task.cancel()
                    heartbeat_task = asyncio.create_task(
                        _heartbeat_loop(username, ws)
                    )
            elif msg.type == WSMsgType.CLOSE:
                logger.info("User %s sent CLOSE frame", username)
                break
            elif msg.type == WSMsgType.ERROR:
                logger.warning("User %s WS error: %s", username, ws.exception())
                break

    except Exception:
        logger.exception("Error in WebSocket handler")
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
        if username is not None:
            _remove_connection(username, ws)
            logger.info(
                "User %s disconnected (total: %d)", username, _total_connections()
            )

    return ws


# --- HTTP dispatch handlers (Unix socket) ---


async def dispatch_handler(request: web.Request) -> web.Response:
    """Handle POST /dispatch to send notifications to connected users."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    user = body.get("user")
    notification = body.get("notification")

    if not user or not notification:
        return web.json_response(
            {"error": "Missing 'user' or 'notification'"}, status=400
        )

    user_connections = connections.get(user, [])
    sent_count = 0
    message = {"type": "notification", **notification}

    for ws in list(user_connections):
        if ws.closed:
            continue
        try:
            await ws.send_json(message)
            sent_count += 1
        except Exception:
            logger.warning("Failed to send notification to user %s", user)

    return web.json_response({"sent": sent_count})


async def health_handler(request: web.Request) -> web.Response:
    """Handle GET /health to report gateway status."""
    total = _total_connections()
    users = {user: len(ws_list) for user, ws_list in connections.items()}
    return web.json_response(
        {"status": "ok", "connections": total, "users": users}
    )


async def main() -> None:
    """Run both the WebSocket TCP server and the HTTP Unix socket dispatch server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # WebSocket TCP server
    ws_app = web.Application()
    ws_app.router.add_get("/", ws_handler)

    ws_runner = web.AppRunner(ws_app)
    await ws_runner.setup()
    ws_site = web.TCPSite(ws_runner, WS_GATEWAY_HOST, WS_GATEWAY_PORT)
    await ws_site.start()
    logger.info(
        "WebSocket server listening on %s:%d", WS_GATEWAY_HOST, WS_GATEWAY_PORT
    )

    # HTTP dispatch Unix socket server
    dispatch_app = web.Application()
    dispatch_app.router.add_post("/dispatch", dispatch_handler)
    dispatch_app.router.add_get("/health", health_handler)

    dispatch_runner = web.AppRunner(dispatch_app)
    await dispatch_runner.setup()
    dispatch_site = web.UnixSite(dispatch_runner, WS_DISPATCH_SOCKET)
    await dispatch_site.start()
    # Allow group members (data-ops: www-data, deploy) to connect to the socket
    os.chmod(WS_DISPATCH_SOCKET, 0o770)
    logger.info("Dispatch server listening on %s", WS_DISPATCH_SOCKET)

    # Run forever
    try:
        await asyncio.Event().wait()
    finally:
        await ws_runner.cleanup()
        await dispatch_runner.cleanup()
