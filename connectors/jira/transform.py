"""
Transform raw Jira JSON data into clean Parquet format for analysis.

Extracts key fields from Jira issues including custom fields used by support team.
Converts Atlassian Document Format (ADF) to plain text.
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Custom field mapping (ID -> human readable name)
# Verified against Jira field configuration (Feb 2026)
CUSTOM_FIELD_NAMES = {
    "customfield_10156": "participants",  # List of users watching/participating
    "customfield_10002": "organizations",  # Organizations
    "customfield_10010": "request_type_info",  # Service Desk request type details
    "customfield_10004": "severity",  # Severity level
    "customfield_10365": "spam",  # Spam flag
    "customfield_10157": "satisfaction",  # Customer satisfaction (was: sla_info)
    "customfield_10323": "triage",  # Triage multi-select (was: team_tier)
    "customfield_10330": "context",  # Context field (was: root_cause)
    "customfield_10325": "custom_url",  # Custom URL (was: resolution_summary)
    "customfield_10350": "slack_link",  # Slack link (was: customer_type)
    "customfield_10475": "email_address",  # Email address (was: context)
    "customfield_10511": "configuration_item",  # Configuration item (was: categories)
    "customfield_10676": "technical_issue_category",  # Technical issue category (was: satisfaction_rating)
    "customfield_10328": "first_response_time",  # SLA: first response time (new)
    "customfield_10161": "time_to_resolution",  # SLA: time to resolution (new)
    "customfield_11831": "l3_team",  # L3 team assignment (new)
}

# Explicit schema definitions for consistent types across monthly chunks
# This prevents DuckDB union errors when some months have all-NULL columns
ISSUES_SCHEMA = {
    "issue_key": "string",
    "issue_id": "string",
    "issue_url": "string",
    "summary": "string",
    "description": "string",
    "issue_type": "string",
    "status": "string",
    "status_category": "string",
    "priority": "string",
    "resolution": "string",
    "project_key": "string",
    "project_name": "string",
    "creator_email": "string",
    "creator_name": "string",
    "reporter_email": "string",
    "reporter_name": "string",
    "assignee_email": "string",
    "assignee_name": "string",
    "created_at": "datetime64[ns, UTC]",
    "updated_at": "datetime64[ns, UTC]",
    "resolved_at": "datetime64[ns, UTC]",
    "due_date": "string",
    "labels": "string",
    "attachment_count": "Int64",
    "comment_count": "Int64",
    "issuelink_count": "Int64",
    "request_type": "string",
    "request_status": "string",
    "severity": "string",
    "triage": "string",
    "configuration_item": "string",
    "participants": "string",
    "organizations": "string",
    "spam": "string",
    "context": "string",
    "custom_url": "string",
    "slack_link": "string",
    "technical_issue_category": "string",
    "email_address": "string",
    "satisfaction": "Int64",
    "first_response_breached": "string",
    "first_response_goal_millis": "Int64",
    "first_response_elapsed_millis": "Int64",
    "time_to_resolution_breached": "string",
    "time_to_resolution_goal_millis": "Int64",
    "time_to_resolution_elapsed_millis": "Int64",
    "l3_team": "string",
    "_synced_at": "string",
    "_raw_file": "string",
}

COMMENTS_SCHEMA = {
    "comment_id": "string",
    "issue_key": "string",
    "author_email": "string",
    "author_name": "string",
    "body": "string",
    "created_at": "datetime64[ns, UTC]",
    "updated_at": "datetime64[ns, UTC]",
    "update_author_email": "string",
}

ATTACHMENTS_SCHEMA = {
    "attachment_id": "string",
    "issue_key": "string",
    "filename": "string",
    "local_path": "string",
    "hierarchical_path": "string",
    "size_bytes": "Int64",
    "mime_type": "string",
    "author_email": "string",
    "created_at": "datetime64[ns, UTC]",
    "content_url": "string",
    "thumbnail_url": "string",
}

CHANGELOG_SCHEMA = {
    "change_id": "string",
    "issue_key": "string",
    "author_email": "string",
    "author_name": "string",
    "field_name": "string",
    "field_type": "string",
    "from_value": "string",
    "to_value": "string",
    "changed_at": "datetime64[ns, UTC]",
}

ISSUELINKS_SCHEMA = {
    "issue_key": "string",
    "link_id": "string",
    "link_type": "string",
    "direction": "string",
    "linked_issue_key": "string",
    "linked_issue_summary": "string",
    "linked_issue_status": "string",
    "linked_issue_priority": "string",
}

REMOTE_LINKS_SCHEMA = {
    "issue_key": "string",
    "remote_link_id": "string",
    "url": "string",
    "title": "string",
    "application_name": "string",
    "application_type": "string",
}


def get_pyarrow_schema(schema_dict: dict) -> pa.Schema:
    """Convert schema dict to PyArrow schema for consistent Parquet types."""
    pa_fields = []
    for col, dtype in schema_dict.items():
        if dtype == "string":
            pa_fields.append(pa.field(col, pa.string()))
        elif dtype.startswith("datetime64"):
            pa_fields.append(pa.field(col, pa.timestamp("us", tz="UTC")))
        elif dtype == "Int64":
            pa_fields.append(pa.field(col, pa.int64()))
        else:
            pa_fields.append(pa.field(col, pa.string()))
    return pa.schema(pa_fields)


def apply_schema(df: pd.DataFrame, schema: dict) -> pa.Table:
    """
    Apply explicit schema to DataFrame and return PyArrow Table.

    This ensures all monthly chunks have the same column types,
    preventing DuckDB union errors when querying with glob patterns.
    """
    # Ensure all schema columns exist
    for col in schema.keys():
        if col not in df.columns:
            df[col] = None

    # Convert types
    for col, dtype in schema.items():
        if dtype == "string":
            # Convert to string, keeping None as None
            df[col] = df[col].apply(lambda x: str(x) if x is not None and pd.notna(x) else None)
        elif dtype.startswith("datetime64"):
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
        elif dtype == "Int64":
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Reorder columns to match schema
    df = df[[col for col in schema.keys()]]

    # Convert to PyArrow with explicit schema
    pa_schema = get_pyarrow_schema(schema)
    return pa.Table.from_pandas(df, schema=pa_schema, preserve_index=False)


def extract_text_from_adf(node: dict | list | None) -> str:
    """
    Extract plain text from Atlassian Document Format (ADF) content.

    ADF is a nested JSON structure used by Jira for rich text.
    This function recursively extracts all text content.
    """
    if node is None:
        return ""

    if isinstance(node, str):
        return node

    if isinstance(node, list):
        return " ".join(extract_text_from_adf(item) for item in node)

    if not isinstance(node, dict):
        return ""

    # Get text from this node
    text_parts = []

    # Direct text content
    if "text" in node:
        text_parts.append(node["text"])

    # Recursive content
    if "content" in node:
        text_parts.append(extract_text_from_adf(node["content"]))

    return " ".join(text_parts).strip()


def extract_user_info(user: dict | None) -> dict:
    """Extract key user information from Jira user object."""
    if not user:
        return {"email": None, "name": None, "account_id": None}

    return {
        "email": user.get("emailAddress"),
        "name": user.get("displayName"),
        "account_id": user.get("accountId"),
    }


def extract_option_value(field: Any) -> str | None:
    """Extract value from Jira option field (select/radio)."""
    if field is None:
        return None
    if isinstance(field, dict):
        return field.get("value") or field.get("name")
    return str(field)


def extract_option_list(field: Any) -> list[str]:
    """Extract values from Jira multi-select field."""
    if not field or not isinstance(field, list):
        return []
    return [extract_option_value(item) for item in field if item]


def parse_datetime(dt_str: str | None) -> datetime | None:
    """Parse Jira datetime string to datetime object."""
    if not dt_str:
        return None
    try:
        # Jira format: "2026-02-03T12:06:52.829+0100"
        # Remove milliseconds and parse
        dt_str = re.sub(r'\.\d+', '', dt_str)
        return datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return None


def extract_sla_metrics(sla_field: Any) -> dict:
    """
    Extract flattened SLA metrics from a Jira SLA field.

    Prefers ongoingCycle (active tickets), falls back to last completedCycle.
    Returns dict with breached, goal_millis, elapsed_millis.
    """
    result = {"breached": None, "goal_millis": None, "elapsed_millis": None}

    if not isinstance(sla_field, dict):
        return result
    # Skip error responses from permission issues
    if "errorMessage" in sla_field:
        return result

    # Prefer ongoing cycle, fall back to last completed cycle
    cycle = sla_field.get("ongoingCycle")
    if not cycle:
        completed = sla_field.get("completedCycles", [])
        if completed:
            cycle = completed[-1]

    if cycle:
        result["breached"] = str(cycle.get("breached")) if cycle.get("breached") is not None else None
        goal = cycle.get("goalDuration", {})
        elapsed = cycle.get("elapsedTime", {})
        result["goal_millis"] = goal.get("millis")
        result["elapsed_millis"] = elapsed.get("millis")

    return result


def transform_issue(raw_issue: dict) -> dict:
    """
    Transform a single raw Jira issue into clean format.

    Returns a flat dictionary suitable for DataFrame conversion.
    """
    fields = raw_issue.get("fields", {})

    # Extract user info
    creator = extract_user_info(fields.get("creator"))
    reporter = extract_user_info(fields.get("reporter"))
    assignee = extract_user_info(fields.get("assignee"))

    # Extract request type info from Service Desk field
    request_type_info = fields.get("customfield_10010", {}) or {}
    request_type = request_type_info.get("requestType", {}) or {}
    current_status = request_type_info.get("currentStatus", {}) or {}

    # Build clean record
    record = {
        # Core identifiers
        "issue_key": raw_issue.get("key"),
        "issue_id": raw_issue.get("id"),
        "issue_url": f"https://{os.environ.get('JIRA_DOMAIN', 'your-org.atlassian.net')}/browse/{raw_issue.get('key')}",

        # Standard fields
        "summary": fields.get("summary"),
        "description": extract_text_from_adf(fields.get("description")),
        "issue_type": fields.get("issuetype", {}).get("name") if fields.get("issuetype") else None,
        "status": fields.get("status", {}).get("name") if fields.get("status") else None,
        "status_category": fields.get("status", {}).get("statusCategory", {}).get("name") if fields.get("status") else None,
        "priority": fields.get("priority", {}).get("name") if fields.get("priority") else None,
        "resolution": fields.get("resolution", {}).get("name") if fields.get("resolution") else None,

        # Project
        "project_key": fields.get("project", {}).get("key") if fields.get("project") else None,
        "project_name": fields.get("project", {}).get("name") if fields.get("project") else None,

        # People
        "creator_email": creator["email"],
        "creator_name": creator["name"],
        "reporter_email": reporter["email"],
        "reporter_name": reporter["name"],
        "assignee_email": assignee["email"],
        "assignee_name": assignee["name"],

        # Dates
        "created_at": parse_datetime(fields.get("created")),
        "updated_at": parse_datetime(fields.get("updated")),
        "resolved_at": parse_datetime(fields.get("resolutiondate")),
        "due_date": fields.get("duedate"),

        # Arrays as JSON strings for Parquet compatibility
        "labels": json.dumps(fields.get("labels", [])),

        # Counts
        "attachment_count": len(fields.get("attachment", [])),
        "comment_count": fields.get("comment", {}).get("total", 0),
        "issuelink_count": len(fields.get("issuelinks", [])),

        # Service Desk specific
        "request_type": request_type.get("name"),
        "request_status": current_status.get("status"),

        # Custom fields (verified against Jira field configuration Feb 2026)
        "severity": extract_option_value(fields.get("customfield_10004")),
        "triage": json.dumps(extract_option_list(fields.get("customfield_10323"))),
        "configuration_item": json.dumps(extract_option_list(fields.get("customfield_10511"))),
        "participants": json.dumps([
            extract_user_info(u).get("email")
            for u in (fields.get("customfield_10156") or [])
        ]),
        "organizations": json.dumps(extract_option_list(fields.get("customfield_10002"))),
        "spam": extract_option_value(fields.get("customfield_10365")),
        "context": extract_text_from_adf(fields.get("customfield_10330")) or None,
        "custom_url": fields.get("customfield_10325"),
        "slack_link": extract_option_value(fields.get("customfield_10350")),
        "technical_issue_category": extract_option_value(fields.get("customfield_10676")),
        "email_address": extract_option_value(fields.get("customfield_10475")),
        "satisfaction": fields.get("customfield_10157", {}).get("rating") if isinstance(fields.get("customfield_10157"), dict) else None,
        **{f"first_response_{k}": v for k, v in extract_sla_metrics(fields.get("customfield_10328")).items()},
        **{f"time_to_resolution_{k}": v for k, v in extract_sla_metrics(fields.get("customfield_10161")).items()},
        "l3_team": extract_option_value(fields.get("customfield_11831")),

        # Metadata
        "_synced_at": raw_issue.get("_synced_at"),
        "_raw_file": None,  # Will be set by caller
    }

    return record


def transform_comments(raw_issue: dict) -> list[dict]:
    """Extract and transform comments from an issue."""
    issue_key = raw_issue.get("key")
    fields = raw_issue.get("fields", {})
    comments_data = fields.get("comment", {})
    comments = comments_data.get("comments", [])

    records = []
    for comment in comments:
        author = extract_user_info(comment.get("author"))
        update_author = extract_user_info(comment.get("updateAuthor"))

        records.append({
            "comment_id": comment.get("id"),
            "issue_key": issue_key,
            "author_email": author["email"],
            "author_name": author["name"],
            "body": extract_text_from_adf(comment.get("body")),
            "created_at": parse_datetime(comment.get("created")),
            "updated_at": parse_datetime(comment.get("updated")),
            "update_author_email": update_author["email"],
        })

    return records


def transform_attachments(raw_issue: dict, attachments_dir: Path | None = None) -> list[dict]:
    """Extract and transform attachments from an issue."""
    issue_key = raw_issue.get("key")
    fields = raw_issue.get("fields", {})
    attachments = fields.get("attachment", [])

    records = []
    for att in attachments:
        author = extract_user_info(att.get("author"))
        att_id = att.get("id")
        filename = att.get("filename")

        # Check if local file exists
        local_path = None
        if attachments_dir and issue_key:
            expected_path = attachments_dir / issue_key / f"{att_id}_{filename}"
            if expected_path.exists():
                local_path = str(expected_path)

        records.append({
            "attachment_id": att_id,
            "issue_key": issue_key,
            "filename": filename,
            "local_path": local_path,
            "size_bytes": att.get("size"),
            "mime_type": att.get("mimeType"),
            "author_email": author["email"],
            "created_at": parse_datetime(att.get("created")),
            "content_url": att.get("content"),
            "thumbnail_url": att.get("thumbnail"),
        })

    return records


def transform_changelog(raw_issue: dict) -> list[dict]:
    """Extract and transform changelog entries from an issue."""
    issue_key = raw_issue.get("key")
    changelog = raw_issue.get("changelog", {})
    histories = changelog.get("histories", [])

    records = []
    for history in histories:
        author = extract_user_info(history.get("author"))
        changed_at = parse_datetime(history.get("created"))

        for item in history.get("items", []):
            records.append({
                "change_id": history.get("id"),
                "issue_key": issue_key,
                "author_email": author["email"],
                "author_name": author["name"],
                "field_name": item.get("field"),
                "field_type": item.get("fieldtype"),
                "from_value": item.get("fromString"),
                "to_value": item.get("toString"),
                "changed_at": changed_at,
            })

    return records


def transform_issuelinks(raw_issue: dict) -> list[dict]:
    """Extract and transform issue links from an issue."""
    issue_key = raw_issue.get("key")
    fields = raw_issue.get("fields", {})
    issuelinks = fields.get("issuelinks", [])

    records = []
    for link in issuelinks:
        link_type = link.get("type", {})
        link_type_name = link_type.get("name", "")

        # Each link has either inwardIssue or outwardIssue
        if "inwardIssue" in link:
            linked = link["inwardIssue"]
            direction = "inward"
        elif "outwardIssue" in link:
            linked = link["outwardIssue"]
            direction = "outward"
        else:
            continue

        linked_fields = linked.get("fields", {})
        records.append({
            "issue_key": issue_key,
            "link_id": link.get("id"),
            "link_type": link_type_name,
            "direction": direction,
            "linked_issue_key": linked.get("key"),
            "linked_issue_summary": linked_fields.get("summary"),
            "linked_issue_status": linked_fields.get("status", {}).get("name") if linked_fields.get("status") else None,
            "linked_issue_priority": linked_fields.get("priority", {}).get("name") if linked_fields.get("priority") else None,
        })

    return records


def transform_remote_links(raw_issue: dict) -> list[dict]:
    """Extract and transform remote links from an issue.

    Remote links are embedded in the raw issue JSON as `_remote_links`
    by the fetch layer (jira_service.py / jira_backfill.py).
    """
    issue_key = raw_issue.get("key")
    remote_links = raw_issue.get("_remote_links", [])

    records = []
    for rl in remote_links:
        obj = rl.get("object", {})
        app = rl.get("application", {})
        records.append({
            "issue_key": issue_key,
            "remote_link_id": str(rl.get("id", "")),
            "url": obj.get("url"),
            "title": obj.get("title"),
            "application_name": app.get("name"),
            "application_type": app.get("type"),
        })

    return records


def get_month_key(dt: datetime | None) -> str:
    """Get month key (YYYY-MM) from datetime, defaulting to current month."""
    if dt is None:
        dt = datetime.utcnow()
    return dt.strftime("%Y-%m")


def get_attachment_path(issue_key: str, attachment_id: str, filename: str) -> str:
    """
    Generate hierarchical attachment path.

    SUPPORT-14991 -> 14/991/54908_files.zip
    """
    # Extract number from issue key (e.g., "SUPPORT-14991" -> "14991")
    match = re.search(r'(\d+)$', issue_key)
    if not match:
        return f"other/{issue_key}/{attachment_id}_{filename}"

    num = match.group(1)
    # Split into prefix (thousands) and suffix (rest)
    prefix = num[:-3] if len(num) > 3 else "0"
    suffix = num[-3:] if len(num) >= 3 else num

    return f"{prefix}/{suffix}/{attachment_id}_{filename}"


def transform_all(
    raw_dir: Path,
    output_dir: Path,
    attachments_dir: Path | None = None,
) -> dict[str, int]:
    """
    Transform all raw Jira JSON files into monthly Parquet chunks.

    Output structure:
        output_dir/
        ├── issues/
        │   ├── 2025-01.parquet
        │   └── 2026-02.parquet
        ├── comments/
        │   └── ...
        ├── changelog/
        │   └── ...
        ├── attachments/
        │   └── ...  (metadata only)
        └── attachments_files/
            └── 14/991/54908_files.zip  (hierarchical)

    Args:
        raw_dir: Directory containing raw JSON files (issues/*.json)
        output_dir: Directory for output Parquet files
        attachments_dir: Directory containing downloaded attachments

    Returns:
        Dict with counts of records per table
    """
    issues_dir = raw_dir / "issues"
    if not issues_dir.exists():
        logger.error(f"Issues directory not found: {issues_dir}")
        return {}

    # Collect records grouped by month (based on issue created_at)
    issues_by_month: dict[str, list] = {}
    comments_by_month: dict[str, list] = {}
    attachments_by_month: dict[str, list] = {}
    changelog_by_month: dict[str, list] = {}
    issuelinks_by_month: dict[str, list] = {}
    remote_links_by_month: dict[str, list] = {}

    # Process each issue file
    json_files = list(issues_dir.glob("*.json"))
    logger.info(f"Processing {len(json_files)} issue files...")

    for json_file in json_files:
        try:
            with open(json_file) as f:
                raw_issue = json.load(f)

            # Transform issue
            issue_record = transform_issue(raw_issue)
            issue_record["_raw_file"] = json_file.name

            # Determine month key based on issue creation date
            month_key = get_month_key(issue_record.get("created_at"))

            # Add to month bucket
            if month_key not in issues_by_month:
                issues_by_month[month_key] = []
                comments_by_month[month_key] = []
                attachments_by_month[month_key] = []
                changelog_by_month[month_key] = []
                issuelinks_by_month[month_key] = []
                remote_links_by_month[month_key] = []

            issues_by_month[month_key].append(issue_record)

            # Transform related data (all go to same month as parent issue)
            comments_by_month[month_key].extend(transform_comments(raw_issue))

            # Transform attachments with hierarchical paths
            issue_key = raw_issue.get("key", "unknown")
            for att_record in transform_attachments(raw_issue, attachments_dir):
                # Update local_path to hierarchical structure
                if att_record.get("local_path"):
                    att_record["hierarchical_path"] = get_attachment_path(
                        issue_key,
                        att_record["attachment_id"],
                        att_record["filename"]
                    )
                attachments_by_month[month_key].append(att_record)

            changelog_by_month[month_key].extend(transform_changelog(raw_issue))
            issuelinks_by_month[month_key].extend(transform_issuelinks(raw_issue))
            remote_links_by_month[month_key].extend(transform_remote_links(raw_issue))

        except Exception as e:
            logger.error(f"Error processing {json_file}: {e}")

    # Create output directories
    (output_dir / "issues").mkdir(parents=True, exist_ok=True)
    (output_dir / "comments").mkdir(parents=True, exist_ok=True)
    (output_dir / "attachments").mkdir(parents=True, exist_ok=True)
    (output_dir / "changelog").mkdir(parents=True, exist_ok=True)
    (output_dir / "issuelinks").mkdir(parents=True, exist_ok=True)
    (output_dir / "remote_links").mkdir(parents=True, exist_ok=True)

    # Save to monthly Parquet files
    counts = {"issues": 0, "comments": 0, "attachments": 0, "changelog": 0, "issuelinks": 0, "remote_links": 0}

    for month_key in sorted(issues_by_month.keys()):
        # Issues
        if issues_by_month[month_key]:
            table = apply_schema(pd.DataFrame(issues_by_month[month_key]), ISSUES_SCHEMA)
            pq.write_table(table, output_dir / "issues" / f"{month_key}.parquet")
            counts["issues"] += table.num_rows
            logger.info(f"Saved {table.num_rows} issues to issues/{month_key}.parquet")

        # Comments
        if comments_by_month[month_key]:
            table = apply_schema(pd.DataFrame(comments_by_month[month_key]), COMMENTS_SCHEMA)
            pq.write_table(table, output_dir / "comments" / f"{month_key}.parquet")
            counts["comments"] += table.num_rows
            logger.info(f"Saved {table.num_rows} comments to comments/{month_key}.parquet")

        # Attachments (metadata)
        if attachments_by_month[month_key]:
            table = apply_schema(pd.DataFrame(attachments_by_month[month_key]), ATTACHMENTS_SCHEMA)
            pq.write_table(table, output_dir / "attachments" / f"{month_key}.parquet")
            counts["attachments"] += table.num_rows
            logger.info(f"Saved {table.num_rows} attachments to attachments/{month_key}.parquet")

        # Changelog
        if changelog_by_month[month_key]:
            table = apply_schema(pd.DataFrame(changelog_by_month[month_key]), CHANGELOG_SCHEMA)
            pq.write_table(table, output_dir / "changelog" / f"{month_key}.parquet")
            counts["changelog"] += table.num_rows
            logger.info(f"Saved {table.num_rows} changelog entries to changelog/{month_key}.parquet")

        # Issue links
        if issuelinks_by_month[month_key]:
            table = apply_schema(pd.DataFrame(issuelinks_by_month[month_key]), ISSUELINKS_SCHEMA)
            pq.write_table(table, output_dir / "issuelinks" / f"{month_key}.parquet")
            counts["issuelinks"] += table.num_rows
            logger.info(f"Saved {table.num_rows} issue links to issuelinks/{month_key}.parquet")

        # Remote links
        if remote_links_by_month[month_key]:
            table = apply_schema(pd.DataFrame(remote_links_by_month[month_key]), REMOTE_LINKS_SCHEMA)
            pq.write_table(table, output_dir / "remote_links" / f"{month_key}.parquet")
            counts["remote_links"] += table.num_rows
            logger.info(f"Saved {table.num_rows} remote links to remote_links/{month_key}.parquet")

    logger.info(f"Created monthly chunks for {len(issues_by_month)} months")
    return counts


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Transform raw Jira JSON to Parquet")
    parser.add_argument("--raw-dir", type=Path, required=True, help="Directory with raw JSON files")
    parser.add_argument("--output-dir", type=Path, required=True, help="Output directory for Parquet files")
    parser.add_argument("--attachments-dir", type=Path, help="Directory with downloaded attachments")

    args = parser.parse_args()

    counts = transform_all(
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        attachments_dir=args.attachments_dir,
    )

    print(f"\nTransformation complete: {counts}")
