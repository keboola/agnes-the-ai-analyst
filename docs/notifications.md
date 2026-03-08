# Telegram Notifications

Get alerts from your analysis scripts directly in Telegram.

## Setup

### 1. Link your Telegram account

1. Open Telegram and message `/start` to **the notification bot** (configured by your admin)
2. You'll receive a 6-digit verification code (valid for 10 minutes)
3. Go to your dashboard and log in
4. In the "Telegram Notifications" section, enter the code and click Verify

### 2. Create a notification script

Create a Python script in `~/user/notifications/` on the server:

```bash
ssh your-server
mkdir -p ~/user/notifications
nano ~/user/notifications/my_alert.py
```

Your script must print a JSON object to stdout:

```python
#!/usr/bin/env python3
import json

result = {
    "notify": True,
    "title": "My Alert Title",
    "message": "Something happened!\nDetails here.",
    "cooldown": "1h"
}

print(json.dumps(result))
```

### 3. Set up crontab

```bash
ssh your-server
crontab -e
```

Add a line to run the runner at your desired interval.
Use `~/.venv/bin/python` to ensure packages (duckdb, pandas, etc.) are available:

```crontab
# Every 5 minutes
*/5 * * * * ~/.venv/bin/python /usr/local/bin/notify-runner >> ~/.notifications/logs/cron.log 2>&1

# Every hour
0 * * * * ~/.venv/bin/python /usr/local/bin/notify-runner >> ~/.notifications/logs/cron.log 2>&1

# Every day at 9:00 AM UTC
0 9 * * * ~/.venv/bin/python /usr/local/bin/notify-runner >> ~/.notifications/logs/cron.log 2>&1
```

> **Note:** The server Python venv (`~/.venv`) is created during initial setup
> and kept in sync automatically by `sync_data.sh`. Any packages you install
> locally will be available on the server after your next sync.

## Script JSON Contract

Your script's stdout must be valid JSON:

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `notify` | yes | bool | Whether to send a notification |
| `title` | no | string | Bold header in the Telegram message |
| `message` | if notify=true | string | Message body (supports Markdown) |
| `image_path` | no | string | Absolute path to a PNG/JPG to send as photo |
| `cooldown` | no | string | Min interval between sends: `30m`, `1h`, `6h`, `1d` (default: `1h`) |
| `data` | no | object | Structured data for future use |

### Cooldown values

The cooldown prevents duplicate notifications. Supported formats:

- `1m`, `5m`, `10m`, `15m`, `30m` - minutes
- `1h`, `2h`, `4h`, `6h`, `12h` - hours
- `1d` - day

### Example: notify=false (no alert)

```json
{"notify": false}
```

### Example: text alert

```json
{
  "notify": true,
  "title": "Revenue dropped 25%",
  "message": "Today: $45k | 7d avg: $60k\nTop declining: Enterprise (-30%)",
  "cooldown": "6h"
}
```

### Example: alert with chart image

```json
{
  "notify": true,
  "title": "Daily Report",
  "message": "Revenue: $52k | Users: 1,200",
  "image_path": "/tmp/my_chart.png",
  "cooldown": "1d"
}
```

## Sending images

Generate a chart in your script (matplotlib, plotly, PIL, etc.) and save it to `/tmp/`:

```python
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import json, os, tempfile

# Create chart
fig, ax = plt.subplots(figsize=(8, 4))
ax.bar(["Mon", "Tue", "Wed", "Thu", "Fri"], [100, 120, 90, 150, 130])
ax.set_title("Daily Revenue")

# Save to temp file
chart_path = os.path.join(tempfile.gettempdir(), "my_chart.png")
plt.savefig(chart_path, dpi=150, bbox_inches="tight")
plt.close()

# Output JSON
print(json.dumps({
    "notify": True,
    "title": "Weekly Chart",
    "message": "See attached chart",
    "image_path": chart_path,
    "cooldown": "1d"
}))
```

## Debugging

### Check runner logs

```bash
cat ~/.notifications/logs/runner.log
cat ~/.notifications/logs/cron.log
```

### Test a script manually

```bash
cd ~
~/.venv/bin/python ~/user/notifications/my_alert.py
```

The output should be valid JSON. Test with:

```bash
cd ~
~/.venv/bin/python ~/user/notifications/my_alert.py | python3 -m json.tool
```

### Check cooldown state

```bash
cat ~/.notifications/state/my_alert.json
```

### Run the runner manually

```bash
/usr/local/bin/notify-runner
```

## Examples

Example scripts are available in `~/server/examples/notifications/`:

- `revenue_drop.py` - text-only alert when revenue drops significantly
- `metric_report.py` - daily report with matplotlib chart
- `data_freshness.py` - alert when local data is stale

Copy an example to get started:

```bash
cp ~/server/examples/notifications/data_freshness.py ~/user/notifications/
```

## Troubleshooting

### ModuleNotFoundError on server

If notification scripts fail with `ModuleNotFoundError`, the server venv is
missing packages. Fix by running a data sync locally:

```bash
bash server/scripts/sync_data.sh
```

This will sync your local Python packages to the server's `~/.venv`.

### Scripts run but DuckDB views fail

DuckDB views use relative paths from the home directory. The notify-runner
sets `cwd` to `~/` automatically. When testing manually, make sure you
`cd ~` first before running scripts.

## Unlink Telegram

To stop receiving notifications, either:
- Remove your crontab entry: `crontab -e` and delete the notify-runner line
- Unlink on the dashboard: go to your dashboard and click "Unlink Telegram"
