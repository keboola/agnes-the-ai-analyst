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
│  ~/keboola-analysis/                                                        │
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

**File:** `webapp/jira_webhook.py`

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

**File:** `webapp/jira_service.py`

Handles Jira API communication and data persistence:

```python
class JiraService:
    def fetch_issue(issue_key) -> dict
        # GET /rest/api/3/issue/{key}?expand=renderedFields,changelog&fields=*all

    def fetch_sla_fields(issue_key) -> dict | None
        # GET via cloud API with JSM service account
        # Returns SLA fields (first_response_time, time_to_resolution)

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

**File:** `src/incremental_jira_transform.py`

Called automatically by webhook handler after saving issue JSON and attachments. Updates only the affected monthly Parquet file.

```python
# Called from jira_service.py after save_issue()
from src.incremental_jira_transform import transform_single_issue

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

**File:** `src/jira_transform.py`

Used for initial historical load or to rebuild all Parquet from raw JSON.

```bash
python src/jira_transform.py \
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

In `/opt/data-analyst/.env`:

```bash
# Jira webhook integration
JIRA_WEBHOOK_SECRET=<random 64-char hex string>
JIRA_DOMAIN=your-org.atlassian.net
JIRA_EMAIL=integration-user@your-domain.com
JIRA_API_TOKEN=<API token from Atlassian>

# Jira SLA service account (JSM Agent licence for SLA fields)
JIRA_SLA_EMAIL=<JSM service account email>
JIRA_SLA_API_TOKEN=<API token from 1Password>
JIRA_CLOUD_ID=f0f7a244-4fb4-41f9-b1f0-b79e24a20f11
```

### GitHub Secrets

| Secret | Description |
|--------|-------------|
| `JIRA_WEBHOOK_SECRET` | HMAC secret for webhook verification |
| `JIRA_DOMAIN` | Jira Cloud domain |
| `JIRA_EMAIL` | Email for API authentication |
| `JIRA_API_TOKEN` | API token from Atlassian account |
| `JIRA_SLA_EMAIL` | JSM service account email (for SLA fields) |
| `JIRA_SLA_API_TOKEN` | JSM service account API token |
| `JIRA_CLOUD_ID` | Atlassian Cloud site ID |

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
tail -f /opt/data-analyst/logs/webapp-error.log | grep -i jira

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
   python src/jira_transform.py \
       --raw-dir /data/src_data/raw/jira \
       --output-dir /data/src_data/parquet/jira \
       --attachments-dir /data/src_data/raw/jira/attachments
   ```
2. Check for errors in transformation output
3. Verify raw JSON files exist in `raw/jira/issues/`
4. Note: Output files are partitioned by month (e.g., `issues/2026-01.parquet`)

## Schema Reference

See [docs/jira_schema.md](jira_schema.md) for detailed table schemas and example queries.

## Historical Backfill

For initial setup or recovery, use the backfill script to download all historical issues.

**File:** `scripts/jira_backfill.py`

```bash
# Download all SUPPORT tickets (idempotent, skips existing)
python scripts/jira_backfill.py --parallel 4

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

**SLA backfill** (separate script, uses JSM service account):

**File:** `scripts/jira_backfill_sla.py`

```bash
# Fetch SLA fields for all issues (uses JIRA_SLA_* env vars)
python scripts/jira_backfill_sla.py --parallel 8

# Dry run (count files needing update):
python scripts/jira_backfill_sla.py --dry-run
```

The personal API token lacks JSM Agent licence needed for SLA fields.
This script uses the JSM service account via the
Atlassian Cloud API (`api.atlassian.com`) to fetch and embed SLA data
into existing raw JSON files.

**After backfill, run batch transform:**
```bash
python src/jira_transform.py \
    --raw-dir /data/src_data/raw/jira \
    --output-dir /data/src_data/parquet/jira \
    --attachments-dir /data/src_data/raw/jira/attachments

# Copy to distribution directory
cp -r /data/src_data/parquet/jira/* ~/server/parquet/jira/
```

## SLA Polling (Open Tickets)

SLA elapsed values (`first_response_elapsed_millis`, `time_to_resolution_elapsed_millis`) only update when a webhook fires. For idle open tickets (~49 tickets, ~0.3% of dataset), these values go stale and no longer reflect the actual current elapsed time.

**File:** `scripts/jira_poll_sla.py`

The SLA polling job runs every 15 minutes via systemd timer (`jira-sla-poll.timer`) as `root:data-ops` and:

1. Reads Parquet to find open issues with SLA data
2. Fetches fresh SLA **and status** fields via JSM service account (cloud API)
3. Updates raw JSON atomically (`tempfile.mkstemp()` + `os.fchmod(fd, 0o660)` + `os.replace()`)
4. Triggers incremental Parquet transform (inside advisory file lock)

**Self-healing:** The poll fetches `status`, `resolution`, `resolutiondate`, and `updated` alongside the SLA fields. If a ticket is resolved in Jira but still appears "open" in Parquet (e.g. due to a missed webhook), the poll automatically corrects the status in JSON and re-transforms to Parquet. Log output: `Self-healing: SUPPORT-XXXX is resolved in Jira`. This was added in response to [#203](https://github.com/keboola/internal_ai_data_analyst/issues/203) where 12 tickets were permanently stale after a permission bug prevented webhooks from updating JSON files.

**File locking:** The entire read-modify-write + Parquet transform is wrapped in a per-issue advisory file lock (`src/jira_file_lock.py`) to prevent races with the webhook handler. The webhook handler (`webapp/jira_service.py`) uses the same lock. Different issue keys don't block each other.

**Important — `mkstemp` and ACL:** The `issues/` directory uses POSIX ACLs with `default:mask::rwx`. `tempfile.mkstemp()` creates files with mode `0600`, which overrides the ACL mask to `---` and breaks group access for www-data (webhook handler) and deploy (batch transform). The `os.fchmod(fd, 0o660)` call immediately after `mkstemp()` restores the mask to `rw-`, preserving ACL-based access. See [#203](https://github.com/keboola/internal_ai_data_analyst/issues/203) for the full incident report.

```bash
# Manual run
python scripts/jira_poll_sla.py

# Dry run (count open issues)
python scripts/jira_poll_sla.py --dry-run

# Verbose logging
python scripts/jira_poll_sla.py --verbose
```

**Return states:**
- `updated` — SLA fields refreshed, status unchanged
- `healed` — status corrected (ticket was resolved in Jira but stale locally)
- `skipped` — no valid SLA data and ticket not resolved
- `failed` — API error or transform failure

**Note:** `sla_cycle_type` (ongoing/completed) is not stored in Parquet — compute it on-the-fly in DuckDB:
```sql
SELECT issue_key,
    CASE WHEN status_category = 'Done' THEN 'completed' ELSE 'ongoing' END AS sla_cycle_type,
    first_response_elapsed_millis,
    time_to_resolution_elapsed_millis
FROM 'server/parquet/jira/issues/*.parquet'
WHERE first_response_elapsed_millis IS NOT NULL
```

## Analyst Sync Configuration

Jira data is an **optional dataset** - not synced by default to save bandwidth.

**Enable Jira sync:**
```bash
# Edit local config (created on first sync_data.sh run)
nano ~/.config/keboola-analyst/sync.yaml

# Change:
datasets:
  jira: true              # Enable parquet data (~50MB)
  jira_attachments: false # Keep false unless you need actual files
```

**Then sync:**
```bash
bash server/scripts/sync_data.sh
```

DuckDB views for Jira tables are created automatically if data exists:
- `jira_issues` - main issues table
- `jira_comments` - issue comments
- `jira_attachments` - attachment metadata (filenames, sizes, URLs)
- `jira_changelog` - field change history
- `jira_issuelinks` - links between issues (blocks, duplicates, relates to)
- `jira_remote_links` - external links (Confluence, Slack, etc.)

## Attachment Access

Attachments (images, logs, PDFs) are stored separately from parquet data.

### Option 1: Download per-ticket (recommended)

Download attachments for a specific ticket to local temp folder:

```bash
# Download all attachments for one ticket
rsync -avz data-analyst:server/jira_attachments/SUPPORT-1234/ /tmp/SUPPORT-1234/

# View locally
ls /tmp/SUPPORT-1234/
open /tmp/SUPPORT-1234/screenshot.png  # macOS
```

This is fast (only downloads files for one ticket) and keeps your local machine clean.

### Option 2: Sync attachments locally (for heavy analysis)

If you need frequent access to attachments, enable full sync:

```yaml
# ~/.config/keboola-analyst/sync.yaml
datasets:
  jira: true
  jira_attachments: true   # Syncs ~500MB+ of files
```

Then `sync_data.sh` will rsync attachments to `./server/jira_attachments/`.

### Finding attachment path from parquet

The `jira_attachments` table has a `local_path` column with the server path:

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

To access locally (if synced): replace `/data/src_data/raw/jira/attachments/` with `./server/jira_attachments/`.

## Future Improvements

- [x] ~~Automatic Parquet regeneration after each webhook~~ (Implemented: incremental transform)
- [x] ~~Incremental Parquet updates~~ (Implemented: upsert by issue_key)
- [x] ~~Full historical sync from Jira~~ (Implemented: jira_backfill.py)
- [x] ~~SLA polling for open tickets~~ (Implemented: jira_poll_sla.py, 15min timer)
- [ ] Comment attachment extraction (inline images in ADF)
- [ ] Custom field name resolution from Jira metadata API
- [ ] Attachment binary sync to analysts (currently metadata only)
