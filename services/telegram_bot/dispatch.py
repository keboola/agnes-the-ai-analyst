"""
Shared notification dispatch to WebSocket gateway.

Used by both the Telegram bot and the webapp REST API to push
notifications to connected desktop app clients.
"""

import logging
import os
import time
import uuid


logger = logging.getLogger(__name__)

WS_GATEWAY_SOCKET_PATH = os.environ.get("WS_GATEWAY_SOCKET", "/run/ws-gateway/ws.sock")


def dispatch_to_ws_gateway(username: str, output: dict, script_name: str) -> None:
    """Dispatch notification to WebSocket gateway for desktop app clients."""
    if not os.path.exists(WS_GATEWAY_SOCKET_PATH):
        return
    try:
        import httpx

        transport = httpx.HTTPTransport(uds=WS_GATEWAY_SOCKET_PATH)
        with httpx.Client(transport=transport, timeout=10) as client:
            notification = {
                "id": str(uuid.uuid4()),
                "title": output.get("title", ""),
                "message": output.get("message", ""),
                "script": script_name,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            image_path = output.get("image_path", "")
            if image_path and os.path.isfile(image_path):
                filename = os.path.basename(image_path)
                notification["image_url"] = f"/api/notifications/images/{filename}"
            client.post(
                "http://localhost/dispatch",
                json={"user": username, "notification": notification},
            )
    except Exception:
        logger.exception("WS gateway dispatch failed")
