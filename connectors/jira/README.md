# Jira Integration

Real-time sync of Jira support tickets for AI-powered analysis.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           JIRA CLOUD                                        │
│                    (your-org.atlassian.net)                                 │
│                                                                             │
│  Issue created/updated/deleted  ───►  Webhook POST                          │
│  Comment added/updated          ───►  with HMAC signature                   │
│  Attachment uploaded            ───►                                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        DATA BROKER SERVER                                   │
│                    (your-instance.example.com)                              │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Flask Webapp (/webhooks/jira)                                      │   │
│  │                                                                     │   │
│  │  1. Verify HMAC-SHA256 signature                                    │   │
│  │  2. Log raw webhook event                                           │   │
│  │  3. Extract issue key from payload                                  │   │
│  │  4. Fetch complete issue data via Jira REST API                     │   │
│  │  5. Overlay SLA fields via JSM service account (cloud API)          │   │
│  │  6. Save issue JSON to disk                                         │   │
│  │  7. Download all attachments                                        │   │
│  │  8. Trigger incremental Parquet transform                           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                        │                                    │
│                                        ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  /data/src_data/raw/jira/                                           │   │
│  │  ├── issues/              # Raw JSON per issue                      │   │
│  │  │   ├── SUPPORT-15186.json                                         │   │
│  │  │   └── SUPPORT-15190.json                                         │   │
│  │  ├── attachments/         # Downloaded files                        │   │
│  │  │   └── SUPPORT-15190/                                             │   │
│  │  │       └── 56340_image.png                                        │   │
│  │  └── webhook_events/      # Audit log                               │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                        │                                    │
│                                        ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  incremental_jira_transform.py (called automatically)               │   │
│  │                                                                     │   │
│  │  • Load saved issue JSON                                            │   │
│  │  • Extract fields, convert ADF to plain text                        │   │
│  │  • Upsert into monthly Parquet (only affected month)                │   │
│  │  • Copy to distribution directory                                   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                        │                                    │
│                                        ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  /data/src_data/parquet/jira/    (monthly partitioned)              │   │
│  │  ├── issues/              # 49 columns, clean schema                │   │
│  │  │   ├── 2025-01.parquet                                            │   │
│  │  │   └── 2025-02.parquet                                            │   │
│  │  ├── comments/            # Extracted comment text                  │   │
│  │  ├── attachments/         # Metadata + local paths                  │   │
│  │  ├── changelog/           # Field change history                    │   │
│  │  ├── issuelinks/          # Links between issues                    │   │
│  │  └── remote_links/        # External links (Confluence, Slack)      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        │ rsync
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ANALYST MACHINE                                     │
│                                                                             │
│  ~/data-analysis/                                                           │
│  └── server/                                                                │
│      └── parquet/                                                           │
│          └── jira/           # Synced Parquet + attachments                 │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Claude Code + DuckDB                                               │   │
│  │                                                                     │   │
│  │  -- Query all months with glob pattern                              │   │
│  │  SELECT * FROM 'server/parquet/jira/issues/*.parquet'               │   │
│  │  WHERE severity LIKE '%Medium%';                                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Jira Webhook Configuration

**Location:** https://your-org.atlassian.net/plugins/servlet/webhooks

| Setting | Value |
|---------|-------|
| URL | `https://your-instance.example.com/webhooks/jira` |
| Secret | Same as `JIRA_WEBHOOK_SECRET` in server `.env` |
| JQL Filter | `project = "Your Project"` |

**Subscribed Events:**
- Issue: created, updated, deleted
- Comment: created, updated
- Attachment: created
- Issue link: created

### 2. Webhook Receiver

**File:** `connectors/jira/webhook.py`

Flask blueprint that handles incoming webhooks:

```python
@jira_bp.route("/jira", methods=["POST"])
def receive_jira_webhook():
    # 1. Verify HMAC signature
    # 2. Parse JSON payload
    # 3. Log event to webhook_events/
    # 4. Call jira_service.process_webhook_event()
```

**Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhooks/jira` | POST | Receive webhooks from Jira |
| `/webhooks/jira/health` | GET | Health check, shows config status |
| `/webhooks/jira/test` | POST | Manual issue fetch (debug mode only) |

### 3. Jira Service

**File:** `connectors/jira/service.py`

Handles Jira API communication and data persistence:

```python
class JiraService:
    def fetch_issue(issue_key) -> dict
        # GET /rest/api/3/issue/{key}?expand=renderedFields,changelog&fields=*all

    def fetch_refresh_fields(issue_key) -> dict | None
        # GET the configured JIRA_REFRESH_FIELDS with the primary token
        # (domain URL, or api.atlassian.com gateway when JIRA_CLOUD_ID is set)

    def save_issue(issue_data) -> Path
        # 1. Fetch remote links
        # 2. Overlay SLA fields from service account
        # 3. Save to /data/src_data/raw/jira/issues/{key}.json
        # 4. Download attachments

    def download_attachment(attachment, issue_key) -> Path
        # GET attachment content URL with auth
        # Save to attachments/{issue_key}/{id}_{filename}
```

**Why fetch after webhook?**
- Webhook payload contains minimal data
- Full issue data requires API call with `fields=*all`
- Ensures we have complete, consistent data

**Why two API tokens?**
- Personal token fetches all fields except SLA (lacks JSM Agent licence)
- JSM service account token fetches SLA fields via Atlassian Cloud API
- SLA data is overlayed into the issue JSON before saving

### 4. Data Transformation

Two transformation modes are available:

#### 4a. Incremental Transform (Real-Time)

**File:** `connectors/jira/incremental_transform.py`

Called automatically by webhook handler after saving issue JSON and attachments. Updates only the affected monthly Parquet file.

```python
# Called from jira_service.py after save_issue()
from connectors.jira.incremental_transform import transform_single_issue

transform_single_issue(
    issue_key="SUPPORT-1234",
    deleted=False,  # or True for deletion events
)
```

**How it works:**
1. Loads the saved JSON for the issue
2. Determines the month from `created_at` date
3. Loads existing Parquet for that month (if any)
4. Upserts issue data (removes old, adds new)
5. Saves updated Parquet
6. Copies to distribution directory for rsync

**Benefits:**
- Data available within seconds of Jira change
- Only updates one monthly file (~50-100KB)
- Rsync transfers only changed files

#### 4b. Batch Transform (Initial Load / Recovery)

**File:** `connectors/jira/transform.py`

Used for initial historical load or to rebuild all Parquet from raw JSON.

```bash
python -m connectors.jira.transform \
    --raw-dir /data/src_data/raw/jira \
    --output-dir /data/src_data/parquet/jira \
    --attachments-dir /data/src_data/raw/jira/attachments
```

**Common transformations (both modes):**
- Extracts plain text from ADF (Atlassian Document Format)
- Maps custom field IDs to human-readable names
- Normalizes nested structures into flat tables
- Links attachments to local file paths
- Enforces explicit PyArrow schema for consistent types across months

### 5. Data Distribution

Analysts sync data via rsync (same as other data):

```bash
bash server/scripts/sync_data.sh
```

This syncs:
- `server/parquet/jira/` - Parquet tables (issues, comments, attachments metadata, changelog, issuelinks, remote_links)

For attachment files, see [Attachment Access](#attachment-access) section below.

## Data Flow Timeline (Real-Time)

```
T+0ms    Jira: Issue updated
T+50ms   Jira: Webhook POST to our server
T+100ms  Server: Verify signature, log event
T+150ms  Server: GET /rest/api/3/issue/{key} from Jira API
T+400ms  Server: GET SLA fields via JSM service account (cloud API)
T+500ms  Server: Save JSON (with SLA overlay) to raw/jira/issues/
T+600ms  Server: Download attachments (parallel)
T+800ms  Server: Incremental transform → update monthly Parquet
T+900ms  Server: Copy to distribution directory
T+1000ms Server: Return 200 OK to Jira

(analyst sync - any time)
T+Xsec   Analyst: bash sync_data.sh
T+Xsec   Analyst: rsync downloads only changed monthly file (~50KB)
T+Xsec   Analyst: Query with DuckDB - sees latest data
```

**Key improvement:** Incremental transform runs immediately after webhook processing, so data is available for sync within seconds of the Jira change.

## Configuration

### Server Environment Variables

In `<install-dir>/.env` (typically the directory you run `docker compose` from):

```bash
# Jira webhook integration (single token)
JIRA_WEBHOOK_SECRET=<random 64-char hex string>
JIRA_DOMAIN=your-org.atlassian.net
JIRA_EMAIL=integration-user@your-domain.com
JIRA_API_TOKEN=<API token from Atlassian; the account needs a JSM Agent licence for SLA>

# Custom fields to refresh onto tickets — generic, no defaults (per instance).
# field_id or field_id:column, comma-separated. Discover with:
#   python -m connectors.jira.scripts.verify_sla_access --list-fields
JIRA_REFRESH_FIELDS=customfield_10328:first_response,customfield_10161:resolution

# Optional: set ONLY for a scoped API token (forces the api.atlassian.com
# gateway). Classic tokens use the site domain URL and need nothing here.
JIRA_CLOUD_ID=
```

### GitHub Secrets

| Secret | Description |
|--------|-------------|
| `JIRA_WEBHOOK_SECRET` | HMAC secret for webhook verification |
| `JIRA_DOMAIN` | Jira Cloud domain |
| `JIRA_EMAIL` | Email for API authentication |
| `JIRA_API_TOKEN` | Primary API token (account needs a JSM Agent licence for SLA) |
| `JIRA_REFRESH_FIELDS` | Custom fields to refresh onto tickets (field_id or field_id:column) |
| `JIRA_CLOUD_ID` | Optional; set only for a scoped API token |

### Getting Jira API Token

1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click "Create API token"
3. Name it (e.g., "Data Analyst Integration")
4. Copy token to `JIRA_API_TOKEN`

**⚠️ IMPORTANT: API tokens expire after 365 days maximum (Atlassian limitation).**

Set a calendar reminder to rotate the token before expiration. When rotating:
1. Create new token in Atlassian
2. Update `JIRA_API_TOKEN` in GitHub Secrets and server `.env`
3. Restart webapp: `sudo systemctl restart webapp`
4. Test: `curl https://your-instance.example.com/webhooks/jira/health`

## Directory Structure

```
/data/src_data/
├── raw/
│   └── jira/                      # Raw data from webhooks
│       ├── issues/                # One JSON file per issue
│       │   ├── SUPPORT-15186.json
│       │   ├── SUPPORT-15189.json
│       │   └── SUPPORT-15190.json
│       ├── attachments/           # Downloaded files (by issue key)
│       │   ├── SUPPORT-15189/
│       │   │   ├── 56337_image.png
│       │   │   └── 56338_image-20260203-110549.png
│       │   └── SUPPORT-15190/
│       │       └── 56340_image.png
│       └── webhook_events/        # Audit log of all webhooks
│           ├── 20260203_105203_jira_issue_updated.json
│           └── 20260203_110457_comment_created.json
│
└── parquet/
    └── jira/                      # Transformed data (monthly partitioned)
        ├── issues/                # Main issues table
        │   ├── 2025-01.parquet
        │   ├── 2025-02.parquet
        │   └── ...
        ├── comments/              # Issue comments
        │   └── YYYY-MM.parquet
        ├── attachments/           # Attachment metadata
        │   └── YYYY-MM.parquet
        ├── changelog/             # Field change history
        │   └── YYYY-MM.parquet
        ├── issuelinks/            # Links between issues
        │   └── YYYY-MM.parquet
        └── remote_links/          # External links (Confluence, Slack, etc.)
            └── YYYY-MM.parquet
```

**Monthly Partitioning Benefits:**
- Efficient rsync: only changed months are transferred
- Better performance: smaller files for ~15,000 total tickets
- Incremental updates: new months don't rewrite old data

## Monitoring

### Health Check

```bash
curl https://your-instance.example.com/webhooks/jira/health
```

Response:
```json
{
  "status": "ok",
  "configured": true,
  "webhook_secret_set": true,
  "jira_domain": "your-org.atlassian.net"
}
```

### Logs

```bash
# Webapp logs (webhook processing)
docker compose logs app --tail 200 | grep -i jira

# Recent webhook events
ls -lt /data/src_data/raw/jira/webhook_events/ | head -20

# Issue count
ls /data/src_data/raw/jira/issues/ | wc -l

# Attachment count
find /data/src_data/raw/jira/attachments/ -type f | wc -l
```

## Security

| Layer | Protection |
|-------|------------|
| Webhook | HMAC-SHA256 signature verification |
| API Auth | HTTP Basic Auth (email + API token) |
| Storage | Server directories with `data-ops` group permissions |
| Transport | HTTPS only (Let's Encrypt certificate) |

**Webhook Signature Verification:**
```python
expected = hmac.new(
    secret.encode('utf-8'),
    request.get_data(),
    hashlib.sha256
).hexdigest()

if not hmac.compare_digest(signature, expected):
    abort(401)
```

## Troubleshooting

### Webhook not received

1. Check Jira webhook is enabled and URL is correct
2. Verify JQL filter matches the issue's project
3. Check server firewall allows HTTPS from Atlassian IPs

### Signature verification fails

1. Verify `JIRA_WEBHOOK_SECRET` matches in both Jira and server `.env`
2. Check for trailing whitespace in secret
3. Restart webapp after changing `.env`

### Attachments not downloading

1. Check `JIRA_API_TOKEN` is valid
2. Verify API token has read access to attachments
3. Check disk space on `/data` partition
4. Large attachments (>50MB) are skipped by design

### Missing data in Parquet

1. Run transformation manually:
   ```bash
   python -m connectors.jira.transform \
       --raw-dir /data/src_data/raw/jira \
       --output-dir /data/src_data/parquet/jira \
       --attachments-dir /data/src_data/raw/jira/attachments
   ```
2. Check for errors in transformation output
3. Verify raw JSON files exist in `raw/jira/issues/`
4. Note: Output files are partitioned by month (e.g., `issues/2026-01.parquet`)

## Schema Reference

The Jira tables and their columns are described in [`docs/DATA_SOURCES.md`](../../docs/DATA_SOURCES.md). At runtime, inspect the live schema with `agnes schema <table>` and `agnes describe <table>`.

## Historical Backfill

For initial setup or recovery, use the backfill script to download all historical issues.

**File:** `connectors/jira/scripts/backfill.py`

```bash
# Download all SUPPORT tickets (idempotent, skips existing)
python -m connectors.jira.scripts.backfill --parallel 4

# Environment variables required:
JIRA_DOMAIN=your-org.atlassian.net
JIRA_EMAIL=integration-user@your-domain.com
JIRA_API_TOKEN=<API token>
JIRA_DATA_DIR=/data/src_data/raw/jira  # optional, default path
```

**Features:**
- Uses new Jira Cloud API (`POST /rest/api/3/search/jql` with `nextPageToken`)
- Parallel downloads (configurable workers)
- Downloads all attachments
- Idempotent - skips already downloaded issues
- Handles rate limiting gracefully

**Field backfill** (separate script, primary token):

**File:** `connectors/jira/scripts/backfill_sla.py`

```bash
# Fetch the configured JIRA_REFRESH_FIELDS for all issues
python -m connectors.jira.scripts.backfill_sla --parallel 8

# Dry run (count files needing update):
python -m connectors.jira.scripts.backfill_sla --dry-run
```

The configured fields (`JIRA_REFRESH_FIELDS`, no defaults) are ordinary issue
custom fields, fetched with the primary token and embedded into existing raw JSON
files — the site domain URL, or the `api.atlassian.com` gateway when
`JIRA_CLOUD_ID` is set (scoped token). The account needs whatever read permission
each field requires (e.g. a JSM Agent licence for SLA fields). Discover field ids
via `verify_sla_access --list-fields`.

**After backfill, run batch transform:**
```bash
python -m connectors.jira.transform \
    --raw-dir /data/src_data/raw/jira \
    --output-dir /data/src_data/parquet/jira \
    --attachments-dir /data/src_data/raw/jira/attachments

# Copy to distribution directory
cp -r /data/src_data/parquet/jira/* ~/server/parquet/jira/
```

## Field Refresh Polling (Open Tickets)

Configured field values (`JIRA_REFRESH_FIELDS`) only update on the ticket when a webhook fires. For idle open tickets these values go stale, so a poll re-fetches them periodically.

**File:** `connectors/jira/scripts/poll_sla.py`

The polling job runs every 15 minutes via systemd timer (`jira-sla-poll.timer`) as `root:data-ops` and:

1. Reads Parquet to find open issues (`status_category != 'Done'`)
2. Fetches the configured fields **and status** with the primary token
3. Updates raw JSON atomically (`tempfile.mkstemp()` + `os.fchmod(fd, 0o660)` + `os.replace()`)
4. Triggers incremental Parquet transform (inside advisory file lock)

**Self-healing:** The poll fetches `status`, `resolution`, `resolutiondate`, and `updated` alongside the SLA fields. If a ticket is resolved in Jira but still appears "open" in Parquet (e.g. due to a missed webhook), the poll automatically corrects the status in JSON and re-transforms to Parquet. Log output: `Self-healing: SUPPORT-XXXX is resolved in Jira`. This was added in response to [#203](https://github.com/keboola/agnes-the-ai-analyst/issues/203) where 12 tickets were permanently stale after a permission bug prevented webhooks from updating JSON files.

**File locking:** The entire read-modify-write + Parquet transform is wrapped in a per-issue advisory file lock (`connectors/jira/file_lock.py`) to prevent races with the webhook handler. The webhook handler (`connectors/jira/service.py`) uses the same lock. Different issue keys don't block each other.

**Important — `mkstemp` and ACL:** The `issues/` directory uses POSIX ACLs with `default:mask::rwx`. `tempfile.mkstemp()` creates files with mode `0600`, which overrides the ACL mask to `---` and breaks group access for www-data (webhook handler) and deploy (batch transform). The `os.fchmod(fd, 0o660)` call immediately after `mkstemp()` restores the mask to `rw-`, preserving ACL-based access. See [#203](https://github.com/keboola/agnes-the-ai-analyst/issues/203) for the full incident report.

```bash
# Manual run
python -m connectors.jira.scripts.poll_sla

# Dry run (count open issues)
python -m connectors.jira.scripts.poll_sla --dry-run

# Verbose logging
python -m connectors.jira.scripts.poll_sla --verbose
```

**Return states:**
- `updated` — configured fields refreshed, status unchanged
- `healed` — status corrected (ticket was resolved in Jira but stale locally)
- `skipped` — no fresh field data and ticket not resolved
- `failed` — API error or transform failure

**Querying refreshed fields:** each configured field is a JSON-text column on
`issues` (column = the alias from `JIRA_REFRESH_FIELDS`). Extract parts with
DuckDB's JSON functions — e.g. for an SLA field aliased `first_response`:
```sql
SELECT issue_key,
    json_extract(first_response, '$.ongoingCycle.elapsedTime.millis') AS first_response_elapsed_millis
FROM 'server/parquet/jira/issues/*.parquet'
WHERE first_response IS NOT NULL
```

## Analyst Sync Configuration

Whether an analyst sees Jira tables locally is decided server-side: an admin
must register the Jira tables and grant the analyst's group access via
`resource_grants(resource_type='table')`. Once granted, the manifest
advertises the tables and `agnes pull` downloads the parquets to the
analyst's workspace on the next session.

DuckDB views for Jira tables are created automatically if data exists:
- `jira_issues` — main issues table
- `jira_comments` — issue comments
- `jira_attachments` — attachment metadata (filenames, sizes, URLs)
- `jira_changelog` — field change history
- `jira_issuelinks` — links between issues (blocks, duplicates, relates to)
- `jira_remote_links` — external links (Confluence, Slack, etc.)

## Attachment Access

Attachments (images, logs, PDFs) are stored on the server alongside parquet
data and are **not** distributed via `agnes pull` (the manifest only
advertises parquet tables). The `jira_attachments` table has a `local_path`
column with the server-side filesystem path:

```sql
SELECT
    issue_key,
    filename,
    local_path,
    size_bytes
FROM jira_attachments
WHERE issue_key = 'SUPPORT-1234';
```

Result:
```
issue_key     | filename        | local_path                                           | size_bytes
SUPPORT-1234  | screenshot.png  | /data/src_data/raw/jira/attachments/SUPPORT-1234/... | 45678
```

To pull the actual file to a workstation, operators with SSH access to the
host can `scp` / `rsync` from the path above. Public OSS does not ship a
client-side attachment-fetch primitive — wire one up per deployment if
attachment access is required (e.g. a thin admin endpoint that streams the
file with the same RBAC gate as the parquet table).

## Future Improvements

- [x] ~~Automatic Parquet regeneration after each webhook~~ (Implemented: incremental transform)
- [x] ~~Incremental Parquet updates~~ (Implemented: upsert by issue_key)
- [x] ~~Full historical sync from Jira~~ (Implemented: jira_backfill.py)
- [x] ~~SLA polling for open tickets~~ (Implemented: jira_poll_sla.py, 15min timer)
- [ ] Comment attachment extraction (inline images in ADF)
- [ ] Custom field name resolution from Jira metadata API
- [ ] Attachment binary sync to analysts (currently metadata only)
