"""
Services package - standalone optional services.

Each service is a self-contained module with its own systemd unit files,
configuration, and README. Services are auto-discovered by deploy.sh
from services/*/systemd/*.service and *.timer.

Available services:
- telegram_bot: Telegram notification bot
- ws_gateway: WebSocket real-time notification gateway
- corporate_memory: AI knowledge extraction from analyst insights
- session_collector: User session log collection
"""
