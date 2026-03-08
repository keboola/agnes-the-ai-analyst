"""
Configuration for the webapp.

All sensitive values are loaded from environment variables.
Instance-specific branding is loaded from config/instance.yaml.
"""

import os
from pathlib import Path


def _load_instance_config():
    """Load instance config with graceful fallback for development."""
    try:
        from config.loader import load_instance_config
        return load_instance_config()
    except (FileNotFoundError, ImportError, Exception) as e:
        import logging
        logging.getLogger(__name__).warning(f"Instance config not found, using defaults: {e}")
        return {}


def _get(config, *keys, default=""):
    """Get nested config value by traversing keys."""
    value = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value if value is not None else default


_instance = _load_instance_config()


class Config:
    """Flask configuration from environment variables and instance config."""

    # Flask
    SECRET_KEY = os.environ.get("WEBAPP_SECRET_KEY", "dev-secret-key-change-me")
    DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    # Google OAuth
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    # Domain restriction for Google OAuth (loaded from instance config)
    ALLOWED_DOMAIN = _get(_instance, "auth", "allowed_domain", default="")

    # Password authentication for external users (whitelisted emails)
    ALLOWED_EMAILS = [
        e.strip().lower()
        for e in os.environ.get("ALLOWED_EMAILS", "").split(",")
        if e.strip()
    ]
    PASSWORD_USERS_FILE = Path(
        os.environ.get("PASSWORD_USERS_FILE", "/data/auth/password_users.json")
    )

    # SendGrid email service
    SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
    EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM_ADDRESS",
        _get(_instance, "email", "from_address", default="noreply@example.com"))
    EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME",
        _get(_instance, "email", "from_name", default="AI Data Analyst"))

    # Token expiry times (seconds)
    SETUP_TOKEN_EXPIRY = 86400  # 24 hours
    RESET_TOKEN_EXPIRY = 3600  # 1 hour

    # Server info for SSH connection instructions (loaded from instance config)
    SERVER_HOST = os.environ.get("SERVER_HOST",
        _get(_instance, "server", "host", default=""))
    SERVER_HOSTNAME = os.environ.get("SERVER_HOSTNAME",
        _get(_instance, "server", "hostname", default=""))

    # Session config
    SESSION_TYPE = "filesystem"
    SESSION_PERMANENT = False
    PERMANENT_SESSION_LIFETIME = 3600  # 1 hour

    # Desktop app JWT authentication
    DESKTOP_JWT_SECRET = os.environ.get("DESKTOP_JWT_SECRET", "")
    DESKTOP_JWT_EXPIRY_DAYS = 30
    DESKTOP_JWT_REFRESH_GRACE_DAYS = 7
    DESKTOP_JWT_ISSUER = _get(_instance, "desktop", "jwt_issuer", default="data-analyst")
    DESKTOP_URL_SCHEME = _get(_instance, "desktop", "url_scheme", default="data-analyst")

    # Instance branding (for templates)
    INSTANCE_NAME = _get(_instance, "instance", "name", default="AI Data Analyst")
    INSTANCE_SUBTITLE = _get(_instance, "instance", "subtitle", default="")
    INSTANCE_COPYRIGHT = _get(_instance, "instance", "copyright", default="")

    # Telegram bot
    TELEGRAM_BOT_USERNAME = _get(_instance, "telegram", "bot_username", default="")

    # Notification images directory
    NOTIFICATION_IMAGES_DIR = "/tmp"

    # Jira webhook integration
    JIRA_WEBHOOK_SECRET = os.environ.get("JIRA_WEBHOOK_SECRET", "")
    JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "")  # e.g., "yourorg.atlassian.net"
    JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
    JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

    # Jira SLA service account (JSM Agent licence required for SLA fields)
    JIRA_SLA_EMAIL = os.environ.get("JIRA_SLA_EMAIL", "")
    JIRA_SLA_API_TOKEN = os.environ.get("JIRA_SLA_API_TOKEN", "")
    JIRA_CLOUD_ID = os.environ.get("JIRA_CLOUD_ID", "")

    # Jira data storage (raw data, will be processed to parquet later)
    JIRA_DATA_DIR = Path(os.environ.get("JIRA_DATA_DIR", "/data/src_data/raw/jira"))

    @classmethod
    def validate(cls) -> list[str]:
        """Validate that required configuration is present."""
        errors = []
        if not cls.GOOGLE_CLIENT_ID:
            errors.append("GOOGLE_CLIENT_ID is not set")
        if not cls.GOOGLE_CLIENT_SECRET:
            errors.append("GOOGLE_CLIENT_SECRET is not set")
        if cls.SECRET_KEY == "dev-secret-key-change-me":
            errors.append("WEBAPP_SECRET_KEY should be set to a secure random value")
        return errors
