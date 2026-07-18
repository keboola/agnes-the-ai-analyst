"""Shared notification dispatch to the desktop app.

Used by the Telegram bot to push notifications to connected desktop app
clients. Wave-2F task 6: this used to POST over a Unix socket to the
standalone `services/ws_gateway` process; it now publishes on the
coordination pub/sub channel via `app.notifications.publish_notification` —
whichever GATEWAY-role process holds a live desktop WebSocket for the user
receives it (same process, another process on this box, or another replica
entirely when the coordination backend is `redis`).
"""

import logging
import os
import time
import uuid

from app.notifications import publish_notification

logger = logging.getLogger(__name__)


def dispatch_desktop_notification(username: str, output: dict, script_name: str) -> None:
    """Build a desktop notification from a script's output and publish it."""
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
    publish_notification(username, notification)
