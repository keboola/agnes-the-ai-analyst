# Telegram Notification Bot

Technical documentation for the notification engine (Phase 3, Issue #41).

## Architecture

```
┌─ Web Dashboard (Flask) ─────────────────────┐
│  "Telegram Notifications" section            │
│  POST /api/telegram/verify                   │
│  POST /api/telegram/unlink                   │
│  GET  /api/telegram/status                   │
│  Reads/writes: /data/notifications/*.json    │
└──────────────────────────────────────────────┘

┌─ Telegram Bot Service (systemd) ────────────┐
│  Telegram polling (handles /start command)   │
│  HTTP server on unix socket (send API)       │
│  Reads/writes: /data/notifications/*.json    │
└──────────────────────────────────────────────┘
        ▲ unix socket
        │ /data/notifications/bot.sock
┌───────┴──────────────────────────────────────┐
│  notify-runner (user crontab)                │
│  Runs ~/user/notifications/*.py               │
│  Sends results via socket to bot             │
└──────────────────────────────────────────────┘
```

## Components

### 1. Telegram Bot Service

**Source:** `server/telegram_bot/`

| File | Purpose |
|------|---------|
| `bot.py` | Main entry point - asyncio loop running polling + HTTP server |
| `config.py` | Configuration constants (paths, TTLs, limits) |
| `storage.py` | JSON file read/write for user mappings and verification codes |
| `sender.py` | Telegram Bot API calls (sendMessage, sendPhoto, getUpdates) |
| `status.py` | Script listing via `notify-scripts list` helper |
| `runner.py` | Script execution via `notify-scripts run` helper |
| `dispatch.py` | WebSocket gateway dispatch for desktop app notifications |
| `__main__.py` | Allows `python -m server.telegram_bot` |

**Bot behavior (English):**
- `/start` -> generates 6-digit verification code, valid 10 minutes
- Unknown commands -> "Use /start to link your account."

**Send API (unix socket):**
- `POST /send` - send text message (`user`, `text`, `parse_mode`)
- `POST /send_photo` - send photo with caption (`user`, `photo_path`, `caption`)
- `GET /health` - health check

**Systemd service:** `server/notify-bot.service`
- User: `deploy`, Group: `data-ops`
- EnvironmentFile: `/opt/data-analyst/.env` (contains `TELEGRAM_BOT_TOKEN`)
- Restarts automatically on failure

### 2. Web Dashboard Changes

**Modified files:**

| File | Changes |
|------|---------|
| `webapp/app.py` | Added 3 API endpoints + `telegram_status` in dashboard context |
| `webapp/telegram_service.py` | New file - verify/unlink/status logic using shared JSON files |
| `webapp/templates/dashboard.html` | New "Telegram Notifications" card with verify/unlink UI |

**API endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/telegram/verify` | POST | Verify code, link Telegram (`{"code": "123456"}`) |
| `/api/telegram/unlink` | POST | Unlink Telegram account |
| `/api/telegram/status` | GET | Get link status (`{"linked": true, "linked_at": "..."}`) |

All endpoints require login (Google SSO).

### 3. Notify Runner

**Source:** `server/bin/notify-runner`

Installed to `/usr/local/bin/notify-runner`. Users set up their own crontab.

**What it does:**
1. Finds `~/user/notifications/*.py`
2. For each script:
   - Checks cooldown state (`~/.notifications/state/{name}.json`)
   - Runs subprocess with 60s timeout
   - Parses stdout JSON
   - If `notify: true` and cooldown OK: sends via bot socket
   - Updates cooldown state
3. Logs to `~/.notifications/logs/runner.log`

**Dependencies:** `httpx` (for unix socket HTTP client)

### 4. Example Scripts

**Source:** `examples/notifications/`

| Script | Description |
|--------|-------------|
| `revenue_drop.py` | Text-only alert when revenue drops vs 7-day average |
| `metric_report.py` | Daily report with matplotlib chart (image notification) |
| `data_freshness.py` | Alert when local parquet data is stale |

Deployed to `/data/docs/examples/notifications/` on the server.

## Data Storage

All notification data lives on the `/data` disk (backupable):

```
/data/notifications/           # owner: deploy, group: data-ops, mode: 770
├── telegram_users.json        # username -> {chat_id, linked_at}
├── pending_codes.json         # code -> {chat_id, created_at}
└── bot.log                    # bot service log
```

Per-user state:
```
~/.notifications/
├── state/                     # cooldown state per script
│   └── {script_name}.json     # {"last_sent": unix_timestamp}
└── logs/
    ├── runner.log             # notify-runner log
    └── cron.log               # crontab output
```

## User Flow: Link Telegram

1. User logs in on dashboard (Google SSO)
2. Sees "Telegram Notifications" card
3. Messages `/start` to @YourBot
4. Bot replies with 6-digit code (valid 10 min)
5. User enters code on dashboard, clicks Verify
6. Webapp verifies code against `pending_codes.json`, saves mapping to `telegram_users.json`
7. Dashboard shows "Linked" status

## Notification Script Contract

Scripts output JSON to stdout:

```json
{
  "notify": true,
  "title": "Revenue dropped 25%",
  "message": "Today: $45k | 7d avg: $60k",
  "image_path": "/tmp/chart.png",
  "cooldown": "1h"
}
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `notify` | yes | bool | Send notification? |
| `title` | no | string | Bold header in Telegram |
| `message` | if notify=true | string | Message body (Markdown) |
| `image_path` | no | string | Absolute path to PNG/JPG |
| `cooldown` | no | string | `30m`, `1h`, `6h`, `1d` (default: `1h`) |
| `data` | no | object | Structured data (for future use) |

## Deployment

### Files deployed by `deploy.sh`

| Source | Destination |
|--------|-------------|
| `server/bin/notify-scripts` | `/usr/local/bin/notify-scripts` |
| `server/bin/notify-runner` | `/usr/local/bin/notify-runner` |
| `server/notify-bot.service` | `/etc/systemd/system/notify-bot.service` |
| `examples/notifications/*.py` | `/data/docs/examples/notifications/` |
| `docs/notifications.md` | `/data/docs/notifications.md` |

### Changes to existing deploy scripts

| File | Changes |
|------|---------|
| `server/deploy.sh` | Deploys runner, creates `/data/notifications/`, manages notify-bot service, deploys examples, includes `TELEGRAM_BOT_TOKEN` in .env |
| `server/bin/add-analyst` | Creates `~/.notifications/{state,logs}` for new users |
| `server/sudoers-deploy` | Added permissions for notify-bot service, notifications dir, examples |
| `.github/workflows/deploy.yml` | Added `TELEGRAM_BOT_TOKEN` secret to deploy env |
| `requirements.txt` | Added `httpx>=0.27.0`, `aiohttp>=3.9.0` |

### GitHub Secrets

New secret required:

| Secret | Description |
|--------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot API token from @BotFather |

### First-time setup

1. Create bot via [@BotFather](https://t.me/BotFather) on Telegram
2. Copy the bot token
3. Add `TELEGRAM_BOT_TOKEN` to GitHub repository secrets
4. Deploy (push to main or manual trigger)
5. Verify bot service is running: `sudo systemctl status notify-bot`

## Security

| Aspect | Implementation |
|--------|---------------|
| Bot token | Central, stored as GitHub Secret, deployed to server `.env` |
| User access to token | None - users communicate via unix socket only |
| Socket permissions | `deploy:data-ops`, mode `0660` |
| Script execution (crontab) | Runs under user's own account (no sudo) |
| Script execution (on-demand) | Via `notify-scripts` helper: `sudo -u <user> /usr/local/bin/notify-scripts run` -- services never access user home directories directly |
| Script timeout | 60 seconds (enforced by `notify-scripts` helper) |
| Home directory isolation | User homes are `750`, services use `notify-scripts` running as target user |
| Verification codes | Expire after 10 minutes, single-use |
| Telegram data | Stored on `/data` disk (backupable) |

## Monitoring

```bash
# Bot service status
sudo systemctl status notify-bot

# Bot logs
tail -f /data/notifications/bot.log

# Runner logs (per user)
tail -f ~/.notifications/logs/runner.log
tail -f ~/.notifications/logs/cron.log

# Linked users
cat /data/notifications/telegram_users.json | python3 -m json.tool
```

## Troubleshooting

**Bot not responding to /start:**
- Check service: `sudo systemctl status notify-bot`
- Check token: `grep TELEGRAM_BOT_TOKEN /opt/data-analyst/.env`
- Check logs: `tail -50 /data/notifications/bot.log`

**Verification code not working:**
- Codes expire after 10 minutes
- Each `/start` invalidates previous codes for that chat
- Check `cat /data/notifications/pending_codes.json`

**Runner not sending notifications:**
- Check socket exists: `ls -la /data/notifications/bot.sock`
- Check user is linked: `cat /data/notifications/telegram_users.json`
- Check cooldown: `cat ~/.notifications/state/{script_name}.json`
- Run manually: `/usr/local/bin/notify-runner`

**Permission denied on socket:**
- User must be in `dataread` group (all analysts are)
- Socket ownership: `deploy:data-ops`, mode `0660`
- Verify groups: `groups $(whoami)`
