# Notifications — How notifications work

## Architecture
1. User creates a Python script (locally or via Claude Code)
2. Script queries local DuckDB and produces output
3. Output is sent via Telegram bot or WebSocket gateway

## Creating a Notification Script
```python
# user/scripts/sales_alert.py
"""Sales alert - checks daily revenue."""
import duckdb

conn = duckdb.connect('user/duckdb/analytics.duckdb', read_only=True)
result = conn.execute("SELECT sum(amount) as revenue FROM orders WHERE date = current_date").fetchone()
print(f"Today's revenue: ${result[0]:,.2f}")
```

## Running Locally
```bash
da scripts run sales_alert           # runs on your machine
```

## Deploying to Server
```bash
da scripts deploy sales_alert --schedule "0 8 * * MON"  # every Monday 8 AM
```

## Delivery Channels
- **Telegram**: Link via `da auth telegram-link`
- **Desktop app**: Via WebSocket gateway (automatic if connected)

## Managing Scripts
```bash
da scripts list                      # all deployed scripts
da scripts undeploy <script-id>      # remove from server
```
