"""
User management service.

Handles checking if users exist and creating new analyst accounts.
"""

import grp
import logging
import pwd
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class UserInfo:
    """Information about an existing system user."""

    username: str
    exists: bool
    groups: list[str]
    home_dir: str | None = None
    is_analyst: bool = False
    is_privileged: bool = False
    is_admin: bool = False


# Reserved system usernames that cannot be used
RESERVED_USERNAMES = frozenset([
    "root", "admin", "administrator", "www-data", "nginx", "apache",
    "nobody", "daemon", "bin", "sys", "sync", "games", "man", "lp",
    "mail", "news", "uucp", "proxy", "backup", "list", "irc", "gnats",
    "sshd", "systemd", "messagebus", "syslog", "deploy", "git", "postgres",
    "mysql", "redis", "mongodb", "elasticsearch", "docker", "ubuntu",
    "debian", "centos", "data", "test", "guest", "user", "ftp", "http",
])


def get_username_from_email(email: str) -> str:
    """
    Convert email address to a unique system username.

    Always uses the full email to avoid collisions:
        admin@test.com   -> admin_test_com
        pavel@groupon.com -> pavel_groupon_com
        john.doe@acme.com -> john_doe_acme_com

    This ensures uniqueness across multiple domains and avoids
    collisions with reserved system usernames like 'admin', 'test', etc.
    """
    if not email or "@" not in email:
        return ""

    # Full email, normalized: replace @ and . with underscores
    safe_username = email.lower().replace("@", "_").replace(".", "_")
    return safe_username


def is_username_available(username: str) -> tuple[bool, str]:
    """
    Check if username is available for registration.

    Returns (is_available, reason).
    A username is NOT available if:
    - It's in the reserved list
    - It already exists as a system user who is NOT a dataread analyst
    """
    if not username:
        return False, "Username cannot be empty"

    if username in RESERVED_USERNAMES:
        return False, f"Username '{username}' is reserved for system use"

    # Check if user exists on the system
    user_info = check_user_exists(username)

    if user_info.exists:
        # User exists - check if it's an analyst account (created by this system)
        # Analysts will have the 'dataread' group
        if user_info.is_analyst:
            # This is an existing analyst - they can log in but not re-register
            return False, "Account already exists"
        else:
            # This is a system account (not created by add-analyst)
            return False, f"Username '{username}' is already in use by a system account"

    return True, ""


def check_user_exists(username: str) -> UserInfo:
    """
    Check if a system user exists and get their info.

    Returns UserInfo with exists=False if user doesn't exist.
    """
    try:
        pw = pwd.getpwnam(username)

        # Get all groups for this user
        groups = []
        for g in grp.getgrall():
            if username in g.gr_mem:
                groups.append(g.gr_name)

        # Also add primary group
        try:
            primary_group = grp.getgrgid(pw.pw_gid).gr_name
            if primary_group not in groups:
                groups.append(primary_group)
        except KeyError:
            pass

        return UserInfo(
            username=username,
            exists=True,
            groups=sorted(groups),
            home_dir=pw.pw_dir,
            is_analyst="dataread" in groups,
            is_privileged="data-private" in groups,
            is_admin="sudo" in groups or "data-ops" in groups,
        )

    except KeyError:
        # User doesn't exist
        return UserInfo(
            username=username,
            exists=False,
            groups=[],
        )


def validate_ssh_key(ssh_key: str) -> tuple[bool, str]:
    """
    Validate SSH public key format.

    Returns (is_valid, error_message).
    """
    if not ssh_key:
        return False, "SSH key is required"

    # Normalize whitespace: collapse newlines/tabs/multiple spaces to single spaces
    ssh_key = " ".join(ssh_key.split())

    # Check for basic SSH key format
    # Supports: ssh-rsa, ssh-ed25519, ecdsa-sha2-nistp256, etc.
    key_pattern = r"^(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp\d+|ssh-dss)\s+[A-Za-z0-9+/=]+(\s+.+)?$"

    if not re.match(key_pattern, ssh_key):
        return False, "Invalid SSH key format. Key should start with 'ssh-rsa', 'ssh-ed25519', etc."

    # Check minimum length (RSA keys are typically 372+ chars for 2048 bit)
    if len(ssh_key) < 80:
        return False, "SSH key appears too short"

    # Check for private key (should never be submitted)
    if "PRIVATE KEY" in ssh_key:
        return False, "This appears to be a private key. Please provide your PUBLIC key instead."

    return True, ""


def create_user(username: str, ssh_key: str) -> tuple[bool, str]:
    """
    Create a new standard analyst user.

    Uses sudo to call add-analyst script.
    Returns (success, message).
    """
    # Validate inputs
    if not username or not re.match(r"^[a-z][a-z0-9._-]*$", username):
        return False, "Invalid username format"

    is_valid, error = validate_ssh_key(ssh_key)
    if not is_valid:
        return False, error

    # Normalize whitespace: ensure key is a single line
    ssh_key = " ".join(ssh_key.split())

    try:
        # Call add-analyst via sudo
        result = subprocess.run(
            ["/usr/bin/sudo", "/usr/local/bin/add-analyst", username, ssh_key],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            logger.info(f"Successfully created user: {username}")
            return True, f"User '{username}' created successfully"
        else:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            logger.error(f"Failed to create user {username}: {error_msg}")
            return False, f"Failed to create user: {error_msg}"

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout creating user: {username}")
        return False, "User creation timed out"
    except FileNotFoundError:
        logger.error("add-analyst script not found")
        return False, "User creation script not found on server"
    except Exception as e:
        logger.exception(f"Error creating user {username}: {e}")
        return False, f"Error creating user: {str(e)}"
