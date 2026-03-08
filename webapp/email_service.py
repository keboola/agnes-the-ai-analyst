"""
SendGrid email service for password authentication.

Sends setup and password reset emails to external users.
"""

import logging

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Email, Mail

from .config import Config

logger = logging.getLogger(__name__)


def _get_sendgrid_client() -> SendGridAPIClient | None:
    """Get SendGrid client if API key is configured."""
    if not Config.SENDGRID_API_KEY:
        logger.warning("SENDGRID_API_KEY not configured, emails will not be sent")
        return None
    return SendGridAPIClient(Config.SENDGRID_API_KEY)


def send_setup_email(email: str, token: str, base_url: str) -> tuple[bool, str]:
    """Send account setup email with magic link.

    Args:
        email: Recipient email address
        token: Setup token for the magic link
        base_url: Base URL of the application (e.g., https://data.example.com)

    Returns:
        Tuple of (success, message)
    """
    client = _get_sendgrid_client()
    if not client:
        return False, "Email service not configured"

    setup_url = f"{base_url}/auth/setup/{token}"

    instance_name = Config.INSTANCE_NAME

    subject = f"Set up your {instance_name} account"
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }}
        .header {{
            text-align: center;
            padding: 20px 0;
            border-bottom: 1px solid #e5e7eb;
        }}
        .content {{
            padding: 30px 0;
        }}
        .button {{
            display: inline-block;
            background-color: #2563eb;
            color: white !important;
            text-decoration: none;
            padding: 12px 24px;
            border-radius: 6px;
            font-weight: 500;
            margin: 20px 0;
        }}
        .button:hover {{
            background-color: #1d4ed8;
        }}
        .footer {{
            padding-top: 20px;
            border-top: 1px solid #e5e7eb;
            font-size: 14px;
            color: #6b7280;
        }}
        .warning {{
            background-color: #fef3c7;
            border: 1px solid #f59e0b;
            padding: 12px;
            border-radius: 6px;
            margin: 20px 0;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{instance_name}</h1>
    </div>
    <div class="content">
        <p>Hello,</p>
        <p>You've been granted access to the {instance_name} platform.
        Click the button below to set up your password and complete your account setup.</p>

        <p style="text-align: center;">
            <a href="{setup_url}" class="button">Set Up Your Account</a>
        </p>

        <div class="warning">
            <strong>This link expires in 24 hours.</strong><br>
            If you didn't request access to this platform, you can safely ignore this email.
        </div>

        <p>Or copy and paste this URL into your browser:</p>
        <p style="word-break: break-all; color: #6b7280; font-size: 14px;">
            {setup_url}
        </p>
    </div>
    <div class="footer">
        <p>This is an automated message from {instance_name} platform.</p>
    </div>
</body>
</html>
"""

    plain_content = f"""
{instance_name} - Account Setup

Hello,

You've been granted access to the {instance_name} platform.
Visit the link below to set up your password and complete your account setup:

{setup_url}

This link expires in 24 hours.

If you didn't request access to this platform, you can safely ignore this email.

---
This is an automated message from {instance_name} platform.
"""

    from_email = Email(Config.EMAIL_FROM_ADDRESS, Config.EMAIL_FROM_NAME)
    message = Mail(
        from_email=from_email,
        to_emails=email,
        subject=subject,
        html_content=html_content,
        plain_text_content=plain_content,
    )

    try:
        response = client.send(message)
        if response.status_code in (200, 201, 202):
            logger.info(f"Setup email sent to {email}")
            return True, "Setup email sent successfully"
        logger.error(f"SendGrid error: status {response.status_code}")
        return False, f"Failed to send email (status {response.status_code})"
    except Exception as e:
        logger.exception(f"Failed to send setup email to {email}: {e}")
        return False, f"Failed to send email: {e}"


def send_reset_email(email: str, token: str, base_url: str) -> tuple[bool, str]:
    """Send password reset email with magic link.

    Args:
        email: Recipient email address
        token: Reset token for the magic link
        base_url: Base URL of the application

    Returns:
        Tuple of (success, message)
    """
    client = _get_sendgrid_client()
    if not client:
        return False, "Email service not configured"

    reset_url = f"{base_url}/auth/reset/{token}"

    instance_name = Config.INSTANCE_NAME

    subject = f"Reset your {instance_name} password"
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }}
        .header {{
            text-align: center;
            padding: 20px 0;
            border-bottom: 1px solid #e5e7eb;
        }}
        .content {{
            padding: 30px 0;
        }}
        .button {{
            display: inline-block;
            background-color: #2563eb;
            color: white !important;
            text-decoration: none;
            padding: 12px 24px;
            border-radius: 6px;
            font-weight: 500;
            margin: 20px 0;
        }}
        .button:hover {{
            background-color: #1d4ed8;
        }}
        .footer {{
            padding-top: 20px;
            border-top: 1px solid #e5e7eb;
            font-size: 14px;
            color: #6b7280;
        }}
        .warning {{
            background-color: #fef3c7;
            border: 1px solid #f59e0b;
            padding: 12px;
            border-radius: 6px;
            margin: 20px 0;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{instance_name}</h1>
    </div>
    <div class="content">
        <p>Hello,</p>
        <p>We received a request to reset your password for your {instance_name} account.
        Click the button below to set a new password.</p>

        <p style="text-align: center;">
            <a href="{reset_url}" class="button">Reset Password</a>
        </p>

        <div class="warning">
            <strong>This link expires in 1 hour.</strong><br>
            If you didn't request a password reset, you can safely ignore this email.
            Your password will remain unchanged.
        </div>

        <p>Or copy and paste this URL into your browser:</p>
        <p style="word-break: break-all; color: #6b7280; font-size: 14px;">
            {reset_url}
        </p>
    </div>
    <div class="footer">
        <p>This is an automated message from {instance_name} platform.</p>
    </div>
</body>
</html>
"""

    plain_content = f"""
{instance_name} - Password Reset

Hello,

We received a request to reset your password for your {instance_name} account.
Visit the link below to set a new password:

{reset_url}

This link expires in 1 hour.

If you didn't request a password reset, you can safely ignore this email.
Your password will remain unchanged.

---
This is an automated message from {instance_name} platform.
"""

    from_email = Email(Config.EMAIL_FROM_ADDRESS, Config.EMAIL_FROM_NAME)
    message = Mail(
        from_email=from_email,
        to_emails=email,
        subject=subject,
        html_content=html_content,
        plain_text_content=plain_content,
    )

    try:
        response = client.send(message)
        if response.status_code in (200, 201, 202):
            logger.info(f"Reset email sent to {email}")
            return True, "Reset email sent successfully"
        logger.error(f"SendGrid error: status {response.status_code}")
        return False, f"Failed to send email (status {response.status_code})"
    except Exception as e:
        logger.exception(f"Failed to send reset email to {email}: {e}")
        return False, f"Failed to send email: {e}"
