"""
Jira API service for fetching issue data.

Handles communication with Jira Cloud REST API to fetch complete issue data
including all fields, comments, and attachments.

After saving issue data and attachments, triggers incremental Parquet transform
for real-time updates available via rsync.
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class _JiraConfig:
    """Jira configuration from environment variables."""
    JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "")
    JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
    JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
    JIRA_DATA_DIR = Path(os.environ.get("JIRA_DATA_DIR", "/data/src_data/raw/jira"))
    JIRA_CLOUD_ID = os.environ.get("JIRA_CLOUD_ID", "")
    JIRA_SLA_EMAIL = os.environ.get("JIRA_SLA_EMAIL", "")
    JIRA_SLA_API_TOKEN = os.environ.get("JIRA_SLA_API_TOKEN", "")


Config = _JiraConfig


def trigger_incremental_transform(issue_key: str, deleted: bool = False) -> bool:
    """
    Trigger incremental Parquet transform for a single issue.

    This updates only the affected monthly Parquet file, making the change
    immediately available for rsync to analysts.

    Args:
        issue_key: Jira issue key (e.g., "SUPPORT-1234")
        deleted: If True, remove issue from Parquet files

    Returns:
        True if transform succeeded, False otherwise
    """
    try:
        from connectors.jira.incremental_transform import transform_single_issue

        success = transform_single_issue(
            issue_key=issue_key,
            deleted=deleted,
        )

        if success:
            logger.info(f"Incremental transform completed for {issue_key}")
            # Rebuild Jira views in master analytics.duckdb
            try:
                from src.orchestrator import SyncOrchestrator
                SyncOrchestrator().rebuild_source("jira")
            except Exception as orch_err:
                logger.warning(f"Orchestrator rebuild failed: {orch_err}")
        else:
            logger.warning(f"Incremental transform failed for {issue_key}")

        return success

    except ImportError as e:
        logger.warning(f"Incremental transform not available: {e}")
        return False
    except Exception as e:
        logger.error(f"Error in incremental transform for {issue_key}: {e}")
        return False


class JiraService:
    """Service for interacting with Jira Cloud REST API."""

    # Max attachment size to download (50 MB)
    MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024

    def __init__(self) -> None:
        """Initialize Jira service with configuration."""
        self.domain = Config.JIRA_DOMAIN
        self.email = Config.JIRA_EMAIL
        self.api_token = Config.JIRA_API_TOKEN
        self.data_dir = Config.JIRA_DATA_DIR
        self.attachments_dir = self.data_dir / "attachments"

        if not all([self.domain, self.email, self.api_token]):
            logger.warning("Jira credentials not fully configured")

    @property
    def base_url(self) -> str:
        """Get Jira API base URL."""
        return f"https://{self.domain}/rest/api/3"

    @property
    def auth(self) -> tuple[str, str]:
        """Get HTTP Basic auth tuple."""
        return (self.email, self.api_token)

    def is_configured(self) -> bool:
        """Check if Jira service is properly configured."""
        return all([self.domain, self.email, self.api_token])

    def fetch_issue(self, issue_key: str) -> dict[str, Any] | None:
        """
        Fetch complete issue data from Jira.

        Args:
            issue_key: Issue key (e.g., "KSP-123")

        Returns:
            Issue data dict or None if fetch failed
        """
        if not self.is_configured():
            logger.error("Jira service not configured, cannot fetch issue")
            return None

        url = f"{self.base_url}/issue/{issue_key}"
        params = {
            "expand": "renderedFields,changelog",
            "fields": "*all",
        }

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    params=params,
                    headers={"Accept": "application/json"},
                )

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.warning(f"Issue {issue_key} not found")
                return None
            else:
                logger.error(
                    f"Failed to fetch issue {issue_key}: "
                    f"{response.status_code} - {response.text[:200]}"
                )
                return None

        except httpx.RequestError as e:
            logger.error(f"Request error fetching issue {issue_key}: {e}")
            return None

    def fetch_sla_fields(self, issue_key: str) -> dict[str, Any] | None:
        """
        Fetch SLA fields using the JSM service account.

        The personal API token lacks JSM Agent licence needed for SLA fields.
        This method uses a separate service account with the cloud API URL.

        Args:
            issue_key: Issue key (e.g., "SUPPORT-123")

        Returns:
            Dict with SLA field values, or None if not configured/failed
        """
        cloud_id = Config.JIRA_CLOUD_ID
        sla_email = Config.JIRA_SLA_EMAIL
        sla_token = Config.JIRA_SLA_API_TOKEN

        if not all([cloud_id, sla_email, sla_token]):
            logger.debug("SLA service account not configured, skipping SLA fetch")
            return None

        base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
        url = f"{base_url}/issue/{issue_key}"
        params = {"fields": "customfield_10328,customfield_10161"}

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=(sla_email, sla_token),
                    headers={"Accept": "application/json"},
                )

            if response.status_code == 200:
                return response.json().get("fields", {})
            else:
                logger.warning(
                    f"Failed to fetch SLA for {issue_key}: "
                    f"{response.status_code}"
                )
                return None

        except httpx.RequestError as e:
            logger.warning(f"SLA fetch error for {issue_key}: {e}")
            return None

    def fetch_remote_links(self, issue_key: str) -> list[dict]:
        """
        Fetch remote links for an issue from Jira.

        Args:
            issue_key: Issue key (e.g., "KSP-123")

        Returns:
            List of remote link dicts, empty list on failure
        """
        if not self.is_configured():
            return []

        url = f"{self.base_url}/issue/{issue_key}/remotelink"

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    headers={"Accept": "application/json"},
                )

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                return []
            else:
                logger.warning(
                    f"Failed to fetch remote links for {issue_key}: "
                    f"{response.status_code}"
                )
                return []

        except httpx.RequestError as e:
            logger.warning(f"Request error fetching remote links for {issue_key}: {e}")
            return []

    def save_issue(self, issue_data: dict[str, Any]) -> Path | None:
        """
        Save issue data to JSON file.

        Args:
            issue_data: Complete issue data from Jira API

        Returns:
            Path to saved file or None if save failed
        """
        issue_key = issue_data.get("key")
        if not issue_key:
            logger.error("Issue data missing 'key' field")
            return None

        # Create data directory if needed
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Add metadata
        issue_data["_synced_at"] = datetime.utcnow().isoformat()

        # Fetch and embed remote links for Parquet transform
        issue_key_for_links = issue_data.get("key")
        if issue_key_for_links:
            issue_data["_remote_links"] = self.fetch_remote_links(issue_key_for_links)

        # Overlay SLA fields from JSM service account (personal token lacks permissions)
        sla_fields = self.fetch_sla_fields(issue_key)
        if sla_fields:
            if "fields" not in issue_data:
                issue_data["fields"] = {}
            for sla_field_id in ("customfield_10328", "customfield_10161"):
                if sla_field_id in sla_fields:
                    issue_data["fields"][sla_field_id] = sla_fields[sla_field_id]
            logger.info(f"Overlayed SLA fields for {issue_key}")

        # Save to file (one file per issue for now, later we'll batch to parquet)
        issues_dir = self.data_dir / "issues"
        file_path = issues_dir / f"{issue_key}.json"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            from connectors.jira.file_lock import issue_json_lock

            # Lock protects the JSON write + Parquet transform from concurrent
            # SLA poll writes. Attachment download stays outside the lock.
            with issue_json_lock(issues_dir, issue_key):
                # Atomic write: temp file + replace
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(file_path.parent), suffix=".tmp"
                )
                os.fchmod(fd, 0o660)  # Restore group rw for ACL
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(issue_data, f, indent=2, default=str)
                    os.replace(tmp_path, str(file_path))
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
                logger.info(f"Saved issue {issue_key} to {file_path}")

                # Trigger incremental Parquet transform FIRST for real-time rsync.
                # This must run before attachment download because large attachments
                # can cause gunicorn worker timeouts (SIGKILL), preventing the
                # transform from ever running. Parquet availability is higher
                # priority than local attachment files.
                trigger_incremental_transform(issue_key, deleted=False)

            # Download attachments OUTSIDE the lock (non-fatal: timeout/failure
            # here should not block the webhook response or prevent Parquet
            # from being updated, and can be slow)
            try:
                downloaded = self.download_all_attachments(issue_data)
                if downloaded:
                    logger.info(f"Downloaded {len(downloaded)} attachments for {issue_key}")
            except Exception as att_err:
                logger.warning(f"Attachment download failed for {issue_key}: {att_err}")

            return file_path
        except Exception as e:
            logger.error(f"Failed to save issue {issue_key}: {e}")
            return None

    def download_attachment(self, attachment: dict[str, Any], issue_key: str) -> Path | None:
        """
        Download a single attachment from Jira.

        Args:
            attachment: Attachment metadata from Jira API
            issue_key: Issue key for organizing files

        Returns:
            Path to downloaded file or None if download failed
        """
        content_url = attachment.get("content")
        filename = attachment.get("filename", "unknown")
        size = attachment.get("size", 0)
        attachment_id = attachment.get("id", "unknown")

        if not content_url:
            logger.warning(f"Attachment {filename} has no content URL")
            return None

        # Skip large attachments
        if size > self.MAX_ATTACHMENT_SIZE:
            logger.warning(
                f"Skipping attachment {filename} ({size} bytes) - exceeds max size"
            )
            return None

        # Create issue-specific attachment directory
        issue_attachments_dir = self.attachments_dir / issue_key
        issue_attachments_dir.mkdir(parents=True, exist_ok=True)

        # Use attachment ID in filename to avoid collisions
        safe_filename = f"{attachment_id}_{filename}"
        file_path = issue_attachments_dir / safe_filename

        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                response = client.get(
                    content_url,
                    auth=self.auth,
                )

            if response.status_code == 200:
                with open(file_path, "wb") as f:
                    f.write(response.content)
                logger.info(f"Downloaded attachment {filename} to {file_path}")
                return file_path
            else:
                logger.error(
                    f"Failed to download attachment {filename}: "
                    f"{response.status_code}"
                )
                return None

        except httpx.RequestError as e:
            logger.error(f"Request error downloading attachment {filename}: {e}")
            return None

    def download_all_attachments(self, issue_data: dict[str, Any]) -> list[Path]:
        """
        Download all attachments for an issue (from fields and comments).

        Args:
            issue_data: Complete issue data from Jira API

        Returns:
            List of paths to downloaded files
        """
        issue_key = issue_data.get("key", "unknown")
        downloaded = []

        # Get direct attachments from issue fields
        attachments = issue_data.get("fields", {}).get("attachment", [])
        logger.info(f"Issue {issue_key} has {len(attachments)} direct attachments")

        for attachment in attachments:
            path = self.download_attachment(attachment, issue_key)
            if path:
                downloaded.append(path)

        # Check comments for inline attachments (ADF media nodes)
        # Comments in Jira Cloud use Atlassian Document Format (ADF)
        comments_data = issue_data.get("fields", {}).get("comment", {})
        comments = comments_data.get("comments", [])

        for comment in comments:
            # ADF body may contain mediaSingle/mediaInline nodes with attachments
            body = comment.get("body", {})
            media_attachments = self._extract_media_from_adf(body)

            for media_id in media_attachments:
                # Media in comments references attachments by ID
                # Find matching attachment in the attachment list
                for attachment in attachments:
                    if attachment.get("id") == media_id:
                        # Already downloaded above
                        break
                else:
                    # Media not in main attachments - try to fetch directly
                    logger.debug(f"Found media {media_id} in comment, not in attachments")

        logger.info(f"Downloaded {len(downloaded)} attachments for {issue_key}")
        return downloaded

    def _extract_media_from_adf(self, node: dict[str, Any]) -> list[str]:
        """
        Extract media IDs from Atlassian Document Format (ADF) content.

        Args:
            node: ADF node (recursive structure)

        Returns:
            List of media attachment IDs found in the content
        """
        media_ids = []

        if not isinstance(node, dict):
            return media_ids

        # Check if this node is a media node
        node_type = node.get("type", "")
        if node_type in ("mediaSingle", "mediaInline", "media"):
            attrs = node.get("attrs", {})
            media_id = attrs.get("id")
            if media_id:
                media_ids.append(media_id)

        # Recursively check content
        content = node.get("content", [])
        if isinstance(content, list):
            for child in content:
                media_ids.extend(self._extract_media_from_adf(child))

        return media_ids

    def process_webhook_event(self, event_data: dict[str, Any]) -> bool:
        """
        Process a webhook event by fetching and saving the related issue.

        Args:
            event_data: Webhook event payload from Jira

        Returns:
            True if processing succeeded, False otherwise
        """
        # Extract issue key from event
        # Jira webhook format: {"webhookEvent": "jira:issue_updated", "issue": {"key": "KSP-123", ...}}
        issue = event_data.get("issue", {})
        issue_key = issue.get("key")

        if not issue_key:
            # Try alternative format for some events
            issue_key = event_data.get("issue_key")

        if not issue_key:
            logger.warning(f"Could not extract issue key from webhook event: {event_data.get('webhookEvent')}")
            return False

        webhook_event = event_data.get("webhookEvent", "unknown")
        logger.info(f"Processing webhook event: {webhook_event} for issue {issue_key}")

        # Handle deletion events
        if "deleted" in webhook_event.lower():
            return self._handle_deletion(issue_key)

        # Fetch fresh data from API (webhook payload may not have all fields)
        issue_data = self.fetch_issue(issue_key)
        if not issue_data:
            # If fetch fails, try to use embedded issue data from webhook
            if issue and issue.get("fields"):
                logger.info(f"Using embedded issue data for {issue_key}")
                issue_data = issue
            else:
                return False

        # Save the issue
        return self.save_issue(issue_data) is not None

    def _handle_deletion(self, issue_key: str) -> bool:
        """
        Handle issue deletion by marking it as deleted and updating Parquet.

        Args:
            issue_key: Key of deleted issue

        Returns:
            True if handled successfully
        """
        file_path = self.data_dir / "issues" / f"{issue_key}.json"

        if file_path.exists():
            # Mark as deleted rather than removing
            try:
                from connectors.jira.file_lock import issue_json_lock

                issues_dir = self.data_dir / "issues"
                with issue_json_lock(issues_dir, issue_key):
                    with open(file_path) as f:
                        data = json.load(f)
                    data["_deleted_at"] = datetime.utcnow().isoformat()

                    # Atomic write: temp file + replace
                    fd, tmp_path = tempfile.mkstemp(
                        dir=str(file_path.parent), suffix=".tmp"
                    )
                    os.fchmod(fd, 0o660)  # Restore group rw for ACL
                    try:
                        with os.fdopen(fd, "w") as f:
                            json.dump(data, f, indent=2, default=str)
                        os.replace(tmp_path, str(file_path))
                    except Exception:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise
                    logger.info(f"Marked issue {issue_key} as deleted")

                    # Remove from Parquet files
                    trigger_incremental_transform(issue_key, deleted=True)

                return True
            except Exception as e:
                logger.error(f"Failed to mark issue {issue_key} as deleted: {e}")
                return False

        logger.info(f"Issue {issue_key} not found locally, nothing to delete")
        return True


# Singleton instance
_jira_service: JiraService | None = None


def get_jira_service() -> JiraService:
    """Get or create Jira service singleton."""
    global _jira_service
    if _jira_service is None:
        _jira_service = JiraService()
    return _jira_service
