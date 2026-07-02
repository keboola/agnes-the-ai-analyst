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
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from connectors.jira.validation import is_valid_issue_key, safe_join_under

logger = logging.getLogger(__name__)


class JiraFetchError(Exception):
    """Raised by Jira fetch helpers when the API returns an auth (401/403)
    or server (5xx) error. Callers that overlay the result onto cached
    issue JSON (save_issue, backfill processors) MUST catch this and
    skip the overlay; otherwise a transient outage silently wipes
    existing parquet rows for that issue.
    """


class _JiraConfig:
    """Jira configuration from environment variables."""

    JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "")
    JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
    JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
    JIRA_DATA_DIR = Path(os.environ.get("JIRA_DATA_DIR", "/data/src_data/raw/jira"))
    JIRA_CLOUD_ID = os.environ.get("JIRA_CLOUD_ID", "")
    JIRA_WEBHOOK_SECRET = os.environ.get("JIRA_WEBHOOK_SECRET", "")
    DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true")


Config = _JiraConfig


_VALID_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def refresh_fields() -> list[tuple[str, str]]:
    """``[(field_id, column_name), ...]`` parsed from ``JIRA_REFRESH_FIELDS``.

    Format: comma-separated ``field_id`` or ``field_id:column_name``. There are no
    defaults — field ids are assigned per Jira instance, so a hard-coded value would
    be wrong for any other deployment. A ``column_name`` that is not a valid
    SQL/parquet identifier falls back to the field id; entries without an id are
    skipped. Lazy (read at call time, not import) so CLI scripts that load ``.env``
    via ``load_dotenv()`` at runtime see the value. Discover field ids with
    ``verify_sla_access --list-fields``.
    """
    out: list[tuple[str, str]] = []
    for entry in os.environ.get("JIRA_REFRESH_FIELDS", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        field_id, _, alias = entry.partition(":")
        field_id = field_id.strip()
        alias = alias.strip()
        if not field_id:
            continue
        column = alias if alias and _VALID_COLUMN.match(alias) else field_id
        out.append((field_id, column))
    return out


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
            issue_key: Issue key (e.g., "PROJ-123")

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
                logger.error(f"Failed to fetch issue {issue_key}: {response.status_code} - {response.text[:200]}")
                return None

        except httpx.RequestError as e:
            logger.error(f"Request error fetching issue {issue_key}: {e}")
            return None

    def fetch_refresh_fields(self, issue_key: str) -> dict[str, Any] | None:
        """
        Fetch the configured refresh fields for an issue using the primary token.

        The field ids come from ``refresh_fields()`` (``JIRA_REFRESH_FIELDS``, no
        defaults); when none are configured this returns ``None`` (nothing to
        fetch). These are ordinary issue custom fields, readable via the regular
        issue REST API with the same primary credentials as ``fetch_issue`` (the
        account needs whatever read permission the field requires — e.g. a JSM
        Agent licence for SLA fields). The base URL is the site domain by default;
        when ``JIRA_CLOUD_ID`` is set (required for a *scoped* API token) the
        ``api.atlassian.com`` gateway is used instead, on the same primary auth.

        Args:
            issue_key: Issue key (e.g., "SUPPORT-123")

        Returns:
            Dict with the fetched field values, or None if not configured/failed
        """
        field_ids = [fid for fid, _ in refresh_fields()]
        if not field_ids:
            logger.debug("No refresh fields configured, skipping fetch")
            return None

        if not self.is_configured():
            logger.error("Jira service not configured, cannot fetch refresh fields")
            return None

        cloud_id = Config.JIRA_CLOUD_ID
        if cloud_id:
            base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
        else:
            base_url = self.base_url
        url = f"{base_url}/issue/{issue_key}"
        params = {"fields": ",".join(field_ids)}

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    params=params,
                    headers={"Accept": "application/json"},
                )

            if response.status_code == 200:
                return response.json().get("fields", {})
            else:
                logger.warning(f"Failed to fetch refresh fields for {issue_key}: {response.status_code}")
                return None

        except httpx.RequestError as e:
            logger.warning(f"Refresh-fields fetch error for {issue_key}: {e}")
            return None

    def fetch_remote_links(self, issue_key: str) -> list[dict]:
        """
        Fetch remote links for an issue from Jira.

        Returns the list of remote links on 200; an empty list on 404
        (issue legitimately has no remote links). Raises JiraFetchError
        on ANY other status code or on httpx.RequestError, so callers
        that overlay this onto cached issue data can skip the overlay
        instead of wiping existing rows. Critically, 429 rate limits
        also raise — silently returning [] there would re-trigger the
        same wipe bug (a webhook burst hitting Jira's rate limiter is
        the most likely production scenario).
        """
        # Unconfigured-service case: per the new contract, callers
        # interpret `[]` as "issue legitimately has no remote links"
        # and `JiraFetchError` as "fetch failed, preserve existing
        # rows". Silently returning `[]` here would overlay an empty
        # list onto cached issue JSON and wipe existing parquet rows
        # the next time a webhook fires while creds happen to be
        # missing — the exact regression this PR closes for the 401
        # / 429 / 5xx paths. Raise instead so the overlay site skips.
        if not self.is_configured():
            raise JiraFetchError(
                f"Remote-links fetch for {issue_key} failed: Jira service not configured (missing API credentials)"
            )

        url = f"{self.base_url}/issue/{issue_key}/remotelink"

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError as e:
            raise JiraFetchError(f"Remote-links fetch for {issue_key} failed: connection — {e}") from e

        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return []
        if response.status_code in (401, 403):
            raise JiraFetchError(
                f"Remote-links fetch for {issue_key} failed: auth error "
                f"({response.status_code}) — token may be expired/revoked"
            )
        if response.status_code == 429:
            raise JiraFetchError(f"Remote-links fetch for {issue_key} failed: rate limited (429) — retry later")
        if response.status_code >= 500:
            raise JiraFetchError(f"Remote-links fetch for {issue_key} failed: server error ({response.status_code})")
        raise JiraFetchError(f"Remote-links fetch for {issue_key} failed: unexpected status {response.status_code}")

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

        # Defense-in-depth: validate `issue_key` BEFORE any code path
        # uses it — including the HTTP URL constructions in
        # fetch_remote_links / fetch_refresh_fields below. The webhook
        # handler already validates upstream, but a future internal
        # caller invoking save_issue directly with attacker-controlled
        # input would otherwise fire outbound requests with a malicious
        # path component (limited SSRF / path manipulation against the
        # Jira API server) before the filesystem-side guard rejected it.
        # Issue #83 round 3.
        if not is_valid_issue_key(issue_key):
            logger.error(f"Refusing to save issue with malformed key: {issue_key!r}")
            return None

        # Create data directory if needed
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Add metadata
        issue_data["_synced_at"] = datetime.now(timezone.utc).isoformat()

        # Overlay-skip guard: if fetch_remote_links raises (auth/server failure),
        # leave the _remote_links key ABSENT. transform_remote_links treats absent key
        # as "no fresh data, preserve existing parquet rows". A present-but-empty list
        # would be interpreted as "this issue has no remote links — wipe existing".
        issue_key_for_links = issue_data.get("key")
        if issue_key_for_links:
            try:
                issue_data["_remote_links"] = self.fetch_remote_links(issue_key_for_links)
            except JiraFetchError as e:
                logger.warning(
                    f"Skipping _remote_links overlay for {issue_key_for_links}: {e}. "
                    f"Existing parquet rows will be preserved."
                )

        # Overlay the configured refresh fields, fetched with the primary token.
        refreshed = self.fetch_refresh_fields(issue_key)
        if refreshed:
            if "fields" not in issue_data:
                issue_data["fields"] = {}
            for field_id, _ in refresh_fields():
                if field_id in refreshed:
                    issue_data["fields"][field_id] = refreshed[field_id]
            logger.info(f"Overlayed refresh fields for {issue_key}")

        # Save to file (one file per issue for now, later we'll batch to parquet)
        # Path.resolve() containment as second layer; the regex check
        # above is the primary defense.
        issues_dir = self.data_dir / "issues"
        issues_dir.mkdir(parents=True, exist_ok=True)
        try:
            file_path = safe_join_under(issues_dir, f"{issue_key}.json")
        except ValueError as e:
            logger.error(f"Path traversal blocked for issue {issue_key!r}: {e}")
            return None

        try:
            from connectors.jira.file_lock import issue_json_lock

            # Lock protects the JSON write + Parquet transform from concurrent
            # SLA poll writes. Attachment download stays outside the lock.
            with issue_json_lock(issues_dir, issue_key):
                # Atomic write: temp file + replace
                fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
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
            logger.warning(f"Skipping attachment {filename} ({size} bytes) - exceeds max size")
            return None

        # Create issue-specific attachment directory.
        # Two-layer guard against path traversal via issue_key (issue #83).
        if not is_valid_issue_key(issue_key):
            logger.error(f"Refusing to download attachment for malformed key: {issue_key!r}")
            return None
        try:
            issue_attachments_dir = safe_join_under(self.attachments_dir, issue_key)
        except ValueError as e:
            logger.error(f"Path traversal blocked for attachment {issue_key!r}: {e}")
            return None
        issue_attachments_dir.mkdir(parents=True, exist_ok=True)

        # Use attachment ID in filename to avoid collisions
        safe_filename = f"{attachment_id}_{filename}"
        try:
            file_path = safe_join_under(issue_attachments_dir, safe_filename)
        except ValueError as e:
            logger.error(f"Path traversal blocked for attachment filename {safe_filename!r}: {e}")
            return None

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
                logger.error(f"Failed to download attachment {filename}: {response.status_code}")
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
        # Defensive: a payload may carry `"issue": null` rather than
        # omitting the key. The webhook handler normalises this, but
        # do the same here too — process_webhook_event is reachable from
        # internal callers as well as the webhook path.
        issue = event_data.get("issue") or {}
        issue_key = issue.get("key")

        if not issue_key:
            # Try alternative format for some events
            issue_key = event_data.get("issue_key")

        if not issue_key:
            logger.warning(f"Could not extract issue key from webhook event: {event_data.get('webhookEvent')}")
            return False

        # Defense-in-depth: even if the webhook layer's validation is bypassed
        # (e.g. a future internal caller invokes process_webhook_event directly),
        # refuse a malformed key here. Issue #83.
        if not is_valid_issue_key(issue_key):
            logger.error(f"process_webhook_event: refusing malformed issue key {issue_key!r}")
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
        # Defense-in-depth path-traversal guard (issue #83). Callers should
        # already have validated; refuse anyway.
        if not is_valid_issue_key(issue_key):
            logger.error(f"_handle_deletion: refusing malformed issue key {issue_key!r}")
            return False
        try:
            file_path = safe_join_under(self.data_dir / "issues", f"{issue_key}.json")
        except ValueError as e:
            logger.error(f"_handle_deletion: path traversal blocked for {issue_key!r}: {e}")
            return False

        if file_path.exists():
            # Mark as deleted rather than removing
            try:
                from connectors.jira.file_lock import issue_json_lock

                issues_dir = self.data_dir / "issues"
                with issue_json_lock(issues_dir, issue_key):
                    with open(file_path) as f:
                        data = json.load(f)
                    data["_deleted_at"] = datetime.now(timezone.utc).isoformat()

                    # Atomic write: temp file + replace
                    fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
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
