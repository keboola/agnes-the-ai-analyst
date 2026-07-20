"""
Services package - standalone optional services.

Each service is a self-contained module with its own systemd unit files,
configuration, and README. Services are auto-discovered by deploy.sh
from services/*/systemd/*.service and *.timer.

Available services:
- telegram_bot: Telegram notification bot
- corporate_memory: AI knowledge extraction from analyst insights
- session_collector: User session log collection

The standalone WebSocket notification gateway (``ws_gateway``) was absorbed
into the main FastAPI app (wave-2F task 6) — desktop/browser notifications
now ride the coordination pub/sub channel served by the GATEWAY-role app
process (see ``app/notifications.py`` + ``app/api/notifications_ws.py``)
instead of a separate process.
"""
