# Troubleshoot — Diagnostic procedures

## Quick Check
```bash
da diagnose --json
```

## Common Issues

### Data not updating
1. `da diagnose --component data` — check data freshness
2. `da server logs scheduler --since 1h` — check scheduler logs
3. Verify data source credentials: `da admin test-connection`

### Cannot login
1. Check server is running: `curl http://server:8000/api/health`
2. Check user exists: `da admin list-users` (from admin account)
3. Re-generate token: `da login --email your@email.com`

### DuckDB errors locally
1. Re-sync: `da sync` (rebuilds views)
2. Check disk space: `du -sh user/duckdb/`
3. Delete and re-create: `rm user/duckdb/analytics.duckdb && da sync`

### Server unresponsive
1. `docker compose ps` — check container status
2. `docker compose logs app --tail 50` — check app logs
3. `docker compose restart app` — restart app

## Escalation
If automated diagnostics don't help:
1. Collect full diagnostic: `da diagnose --json > /tmp/diag.json`
2. Collect server logs: `docker compose logs > /tmp/logs.txt`
3. Share both files with admin
