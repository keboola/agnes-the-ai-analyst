# Jira Support Tickets Schema

This document describes the schema of transformed Jira data available for analysis.

## Data Location

```
/data/src_data/parquet/jira/     # Transformed Parquet files (monthly chunks)
├── issues/                      # Main issues table
│   ├── 2025-01.parquet
│   ├── 2025-02.parquet
│   └── ...
├── comments/                    # Issue comments
│   └── YYYY-MM.parquet
├── attachments/                 # Attachment metadata with local paths
│   └── YYYY-MM.parquet
├── changelog/                   # Change history
│   └── YYYY-MM.parquet
├── issuelinks/                  # Links between issues
│   └── YYYY-MM.parquet
└── remote_links/                # External links (Confluence, Slack, etc.)
    └── YYYY-MM.parquet

/data/src_data/raw/jira/         # Raw data (JSON + files)
├── issues/                      # Raw JSON per issue
├── attachments/                 # Downloaded attachment files
│   └── {issue_key}/            # By issue key (e.g., SUPPORT-15051/)
│       └── {id}_{filename}     # e.g., 56340_screenshot.png
└── webhook_events/              # Raw webhook payloads (audit)
```

**Monthly Partitioning:** Parquet files are partitioned by month based on `created_at` timestamp. This enables efficient rsync (only changed months sync) and keeps individual file sizes manageable for ~15,000 tickets.

**DuckDB Query Pattern:** Use glob patterns to query all months:
```sql
SELECT * FROM 'server/parquet/jira/issues/*.parquet';
```

## Tables

### issues

Main table with support ticket information.

| Column | Type | Description |
|--------|------|-------------|
| `issue_key` | string | Unique issue identifier (e.g., "SUPPORT-15190") |
| `issue_id` | string | Jira internal ID |
| `issue_url` | string | Direct URL to issue in Jira |
| `summary` | string | Issue title/summary |
| `description` | string | Full description (plain text, extracted from ADF) |
| `issue_type` | string | Type (Service Request, Bug, etc.) |
| `status` | string | Current status (New, Under Review, Resolved, etc.) |
| `status_category` | string | Status category (To Do, In Progress, Done) |
| `priority` | string | Priority level (Lowest, Low, Medium, High, Highest) |
| `resolution` | string | Resolution type if resolved |
| `project_key` | string | Project key (SUPPORT) |
| `project_name` | string | Project name (e.g., your Jira project name) |
| `creator_email` | string | Email of ticket creator |
| `creator_name` | string | Display name of creator |
| `reporter_email` | string | Email of reporter |
| `reporter_name` | string | Display name of reporter |
| `assignee_email` | string | Email of assigned agent |
| `assignee_name` | string | Display name of assignee |
| `created_at` | datetime | When ticket was created |
| `updated_at` | datetime | Last update timestamp |
| `resolved_at` | datetime | When ticket was resolved (null if open) |
| `due_date` | string | Due date if set |
| `labels` | string (JSON) | Array of labels as JSON |
| `attachment_count` | int | Number of attachments |
| `comment_count` | int | Number of comments |
| `issuelink_count` | int | Number of linked issues |
| `request_type` | string | Service Desk request type name |
| `request_status` | string | Service Desk specific status |
| `severity` | string | Severity level (custom field) |
| `triage` | string (JSON) | Triage multi-select (renamed from team_tier) |
| `configuration_item` | string (JSON) | Configuration item multi-select (renamed from categories) |
| `participants` | string (JSON) | List of participant emails |
| `organizations` | string (JSON) | Related organizations |
| `spam` | string | Spam flag (True/null) |
| `context` | string | Context field (renamed from root_cause; maps to customfield_10330) |
| `keboola_platform_url` | string | Keboola platform URL (renamed from resolution_summary) |
| `slack_link` | string | Slack link (renamed from customer_type) |
| `technical_issue_category` | string | Technical issue category (renamed from satisfaction_rating) |
| `email_address` | string | Email address field (renamed from context; maps to customfield_10475) |
| `satisfaction` | int | Customer satisfaction rating (1-5) |
| `first_response_breached` | string | SLA: whether first response SLA was breached (True/False) |
| `first_response_goal_millis` | int | SLA: first response goal duration in milliseconds |
| `first_response_elapsed_millis` | int | SLA: actual first response time in milliseconds |
| `time_to_resolution_breached` | string | SLA: whether resolution SLA was breached (True/False) |
| `time_to_resolution_goal_millis` | int | SLA: resolution goal duration in milliseconds |
| `time_to_resolution_elapsed_millis` | int | SLA: actual resolution time in milliseconds |
| `l3_team` | string | L3 team assignment (new) |
| `_synced_at` | string | When data was synced from Jira |
| `_raw_file` | string | Source JSON filename |

### comments

Issue comments from support conversations.

| Column | Type | Description |
|--------|------|-------------|
| `comment_id` | string | Unique comment ID |
| `issue_key` | string | Parent issue key (FK to issues) |
| `author_email` | string | Comment author email |
| `author_name` | string | Comment author display name |
| `body` | string | Comment text (plain text, extracted from ADF) |
| `created_at` | datetime | When comment was created |
| `updated_at` | datetime | When comment was last edited |
| `update_author_email` | string | Who last edited the comment |

### attachments

Attachment metadata with local file paths.

| Column | Type | Description |
|--------|------|-------------|
| `attachment_id` | string | Unique attachment ID |
| `issue_key` | string | Parent issue key (FK to issues) |
| `filename` | string | Original filename |
| `local_path` | string | Server path to downloaded file |
| `hierarchical_path` | string | Hierarchical path for future use (e.g., `15/051/56340_file.png`) |
| `size_bytes` | int | File size in bytes |
| `mime_type` | string | MIME type (image/png, application/pdf, etc.) |
| `author_email` | string | Who uploaded the attachment |
| `created_at` | datetime | When attachment was uploaded |
| `content_url` | string | Jira API URL to download |
| `thumbnail_url` | string | Jira API URL for thumbnail (images only) |

### changelog

History of all field changes on issues.

| Column | Type | Description |
|--------|------|-------------|
| `change_id` | string | Change history ID |
| `issue_key` | string | Parent issue key (FK to issues) |
| `author_email` | string | Who made the change |
| `author_name` | string | Display name of who made change |
| `field_name` | string | Name of changed field |
| `field_type` | string | Type of field (jira, custom) |
| `from_value` | string | Previous value (as string) |
| `to_value` | string | New value (as string) |
| `changed_at` | datetime | When change occurred |

### issuelinks

Links between Jira issues (blocks, duplicates, relates to, etc.).

| Column | Type | Description |
|--------|------|-------------|
| `issue_key` | string | Source issue key (FK to issues) |
| `link_id` | string | Unique link ID |
| `link_type` | string | Link type name (Blocks, Duplicate, Relates, etc.) |
| `direction` | string | Link direction: "inward" or "outward" |
| `linked_issue_key` | string | Target issue key |
| `linked_issue_summary` | string | Summary of linked issue |
| `linked_issue_status` | string | Status of linked issue |
| `linked_issue_priority` | string | Priority of linked issue |

### remote_links

External links attached to issues (Confluence pages, Slack threads, external URLs).

| Column | Type | Description |
|--------|------|-------------|
| `issue_key` | string | Parent issue key (FK to issues) |
| `remote_link_id` | string | Unique remote link ID |
| `url` | string | External URL |
| `title` | string | Link title/label |
| `application_name` | string | Application name (e.g., "Confluence", "Slack") |
| `application_type` | string | Application type identifier |

## Relationships

All child tables reference `jira_issues` via the `issue_key` column:

```
jira_issues (PK: issue_key)
├── jira_comments       (FK: issue_key → jira_issues.issue_key)
├── jira_attachments    (FK: issue_key → jira_issues.issue_key)
├── jira_changelog      (FK: issue_key → jira_issues.issue_key)
├── jira_issuelinks     (FK: issue_key → jira_issues.issue_key)
│                       (FK: linked_issue_key → jira_issues.issue_key)
└── jira_remote_links   (FK: issue_key → jira_issues.issue_key)
```

These relationships are used by the Data Profiler to populate the Relationships tab in the catalog UI. They enable navigation between related table profiles.

**Join examples:**

```sql
-- Issues with their comments
SELECT i.issue_key, i.summary, c.body, c.created_at
FROM 'server/parquet/jira/issues/*.parquet' i
JOIN 'server/parquet/jira/comments/*.parquet' c ON i.issue_key = c.issue_key;

-- Issues with linked issues
SELECT i.issue_key, i.summary, l.link_type, l.direction, l.linked_issue_key
FROM 'server/parquet/jira/issues/*.parquet' i
JOIN 'server/parquet/jira/issuelinks/*.parquet' l ON i.issue_key = l.issue_key;
```

## Example Queries (DuckDB)

**Note:** Use glob patterns (`*.parquet`) to query all monthly chunks at once.

### Active tickets by status

```sql
SELECT status, COUNT(*) as count
FROM 'server/parquet/jira/issues/*.parquet'
WHERE resolved_at IS NULL
GROUP BY status
ORDER BY count DESC;
```

### Average resolution time by severity

```sql
SELECT
    severity,
    COUNT(*) as tickets,
    AVG(EXTRACT(EPOCH FROM (resolved_at - created_at)) / 3600) as avg_hours
FROM 'server/parquet/jira/issues/*.parquet'
WHERE resolved_at IS NOT NULL
GROUP BY severity;
```

### Most active commenters

```sql
SELECT
    author_email,
    author_name,
    COUNT(*) as comments
FROM 'server/parquet/jira/comments/*.parquet'
GROUP BY author_email, author_name
ORDER BY comments DESC
LIMIT 10;
```

### Tickets with attachments

```sql
SELECT
    i.issue_key,
    i.summary,
    a.filename,
    a.local_path
FROM 'server/parquet/jira/issues/*.parquet' i
JOIN 'server/parquet/jira/attachments/*.parquet' a ON i.issue_key = a.issue_key
WHERE a.local_path IS NOT NULL;
```

### Field change frequency

```sql
SELECT
    field_name,
    COUNT(*) as changes
FROM 'server/parquet/jira/changelog/*.parquet'
GROUP BY field_name
ORDER BY changes DESC;
```

### Query specific month only

```sql
-- Query only January 2026 data
SELECT * FROM 'server/parquet/jira/issues/2026-01.parquet';
```

## Data Freshness

- Data is synced in **real-time** via Jira webhooks
- Each issue update triggers: webhook → fetch → save JSON → download attachments → **incremental Parquet transform**
- Parquet files are updated within seconds of Jira change (only affected month is rewritten)
- Raw JSON is kept for audit and debugging
- Historical data can be loaded via `scripts/jira_backfill.py`

## Viewing Attachments

Attachments are stored on the server at `/data/src_data/raw/jira/attachments/{issue_key}/`.
Analysts can access them via symlink at `~/server/jira_attachments/`.

**Download attachments for a specific ticket:**
```bash
# Rsync one ticket's attachments to local temp folder
rsync -avz data-analyst:server/jira_attachments/SUPPORT-1234/ /tmp/SUPPORT-1234/

# View locally
ls /tmp/SUPPORT-1234/
open /tmp/SUPPORT-1234/screenshot.png  # macOS
```

**Find attachment info from parquet:**
```sql
SELECT issue_key, filename, size_bytes, local_path
FROM jira_attachments
WHERE issue_key = 'SUPPORT-1234';
```

## Custom Field Reference

| Field ID | Column Name | Description |
|----------|-------------|-------------|
| customfield_10004 | severity | Severity: 1-Highest to 5-Lowest |
| customfield_10323 | triage | Triage multi-select (renamed from team_tier) |
| customfield_10511 | configuration_item | Configuration item multi-select (renamed from categories) |
| customfield_10365 | spam | Spam flag: True/null |
| customfield_10010 | request_type_info | Service Desk request type metadata |
| customfield_10330 | context | Context field (renamed from root_cause) |
| customfield_10325 | keboola_platform_url | Keboola platform URL (renamed from resolution_summary) |
| customfield_10350 | slack_link | Slack link (renamed from customer_type) |
| customfield_10475 | email_address | Email address (renamed from context) |
| customfield_10676 | technical_issue_category | Technical issue category (renamed from satisfaction_rating) |
| customfield_10157 | satisfaction | Customer satisfaction rating (1-5) |
| customfield_10328 | first_response_* | SLA: first response (breached, goal_millis, elapsed_millis) |
| customfield_10161 | time_to_resolution_* | SLA: resolution time (breached, goal_millis, elapsed_millis) |
| customfield_11831 | l3_team | L3 team assignment (new) |
| customfield_10156 | participants | Participant user list |
| customfield_10002 | organizations | Organizations |
