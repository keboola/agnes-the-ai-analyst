# Troubleshoot — Diagnostic procedures

## Quick Check
```bash
agnes diagnose --json
```

## Common Issues

### Data not updating
1. `agnes diagnose --component data` — check data freshness
2. `agnes server logs scheduler --since 1h` — check scheduler logs
3. Verify data source credentials: `agnes admin test-connection`

### Cannot login
1. Check server is running: `curl http://server:8000/api/health`
2. Check user exists: `agnes admin list-users` (from admin account)
3. Re-generate token: `agnes login --email your@email.com`

### DuckDB errors locally
1. Re-sync: `agnes pull` (rebuilds views)
2. Check disk space: `du -sh user/duckdb/`
3. Delete and re-create: `rm user/duckdb/analytics.duckdb && agnes pull`

### Server unresponsive
1. `docker compose ps` — check container status
2. `docker compose logs app --tail 50` — check app logs
3. `docker compose restart app` — restart app

## Escalation
If automated diagnostics don't help:
1. Collect full diagnostic: `agnes diagnose --json > /tmp/diag.json`
2. Collect server logs: `docker compose logs > /tmp/logs.txt`
3. Share both files with admin
