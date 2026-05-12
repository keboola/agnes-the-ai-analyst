# Platform Setup

Operator playbook for bootstrapping and running an Agnes instance with full telemetry.

## 1. First-time bootstrap

- Clone the OSS image (`ghcr.io/keboola/agnes-the-ai-analyst:stable`) or pin a `:keboola-deploy-*` tag (see `docs/DEPLOYMENT.md` for release-train discipline).
- `config/instance.yaml` — copy from `config/instance.yaml.example`. Required fields: `instance.name`, `instance.url`, `auth.google.domain` (or `auth.email.allowed_domains`).
- Seed admin: env vars `SEED_ADMIN_EMAIL` + `SEED_ADMIN_PASSWORD` (optional — analyst can also bootstrap via `/auth/bootstrap` on first login).
- First boot: schema migrates automatically to the current version (v41). With no existing data this is fast — expect < 5 seconds.
- Register tables via the admin UI or `POST /api/admin/register-table`. Tables store `source_type`, `bucket`, `source_table`, `query_mode` in the `table_registry` DuckDB table.

## 2. Reverse proxy + TLS

- Caddy in front of uvicorn — see `docs/DEPLOYMENT.md` → **TLS** for the full setup.
- For corporate-CA deployments, mount the CA chain at `/data/state/certs` and use `docker-compose.tls.yml`:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tls.yml \
      --profile tls up -d
  ```
- `scripts/ops/agnes-tls-rotate.sh` runs daily, fetches from `TLS_FULLCHAIN_URL`, sends `SIGUSR1` to Caddy on diff, and is a no-op when unchanged. The infra repo's `startup.sh` installs this as a systemd timer automatically.
- Parquet downloads are offloaded to Caddy via `forward_auth → file_server` (see `docs/DEPLOYMENT.md` → **Caddy file_server**) — prevents a single multi-GB `agnes pull` from starving uvicorn workers.

## 3. Marketplaces (curated + flea)

### Curated marketplace (admin-managed)
- Admin registers repos via `/admin/marketplaces` or `POST /api/marketplaces`. Each entry is a git repo URL + optional PAT.
- PATs persist to `${DATA_DIR}/state/.env_overlay` (chmod 600). DuckDB stores only the env-var name (`AGNES_MARKETPLACE_<SLUG>_TOKEN`), never the secret itself.
- Scheduler clones/fetches nightly at 03:00 UTC via `POST /api/marketplaces/sync-all` (admin-only, authed via `SCHEDULER_API_TOKEN`).
- Manual re-sync: "Sync now" in the admin UI, or `POST /api/marketplaces/{id}/sync`.
- After sync, `src/marketplace.py` parses `.claude-plugin/marketplace.json` and caches plugin metadata in `marketplace_plugins`.
- Plugins are served to analysts via `/marketplace.zip` or `/marketplace.git/*` (both PAT-gated, RBAC-filtered). See `CLAUDE.md` → **Claude Code marketplace endpoint** for registration instructions.

### Flea market (community uploads)
- Analysts upload plugins via `/store/new`. Submissions go through `src/store_guardrails/` LLM-gated approval before becoming visible.
- Schema v37 tables: `store_entities`, `store_submissions`, `user_store_installs`.
- Admin approves or rejects via `/admin/store`. Approved entities appear on the flea tab and gain attribution in telemetry.
- No per-team ACL in v1 — guardrails + admin approval are the gatekeepers.

### Served marketplace composition
- Content served to each analyst = `(admin_granted ∖ opt_outs) ∪ store_installs`.
- Curated plugins take precedence over flea on same-named collision.
- Admin group requires explicit grants (no god-mode shortcut on marketplace feed).

## 4. Scheduler — env vars per processor cadence

All intervals are in seconds. Set in `.env` or compose environment.

| Env var | Default | Description |
|---|---|---|
| `SCHEDULER_VERIFICATION_DETECTOR_INTERVAL` | 900 | Memory pipeline: verification detector |
| `SCHEDULER_USAGE_PROCESSOR_INTERVAL` | 600 | Telemetry extraction from JSONLs |
| `SCHEDULER_CORPORATE_MEMORY_INTERVAL` | 1020 | Memory orchestrator |
| `SCHEDULER_SESSION_COLLECTOR_INTERVAL` | 600 | Pulls JSONLs from per-user SSH paths |
| `SCHEDULER_USAGE_PRUNE_INTERVAL` | 86400 | Daily retention prune of old events |

All scheduler tasks call back into the app over HTTP (`SCHEDULER_API_TOKEN` in environment) so the app remains the sole writer to `system.duckdb`.

## 5. Telemetry — extraction, export, retention, ask

### Extraction

`UsageProcessor` runs every `SCHEDULER_USAGE_PROCESSOR_INTERVAL` seconds:

1. Reads `${SESSION_DATA_DIR}/<user>/*.jsonl` (collected via `agnes push` / `SessionEnd` hook).
2. Parses Claude Code session events — extracts skill/agent/tool/MCP/slash-command invocations.
3. Writes to `usage_events` + `usage_session_summary`.
4. Refreshes rollup tables `usage_tool_daily` + `usage_plugin_daily` after every successful tick.
5. Tracks progress in `session_processor_state` (processor = `usage`) — only new files are processed on subsequent runs.

**Attribution** — `usage_attribution_skills`, `usage_attribution_agents`, `usage_attribution_commands` lookup tables are populated from plugin manifests at marketplace-sync time and store-entity create/update/delete. Curated > flea precedence. Builtin tools (`Bash`, `Read`, `Edit`, `Write`, `Grep`, `Glob`, `TodoWrite`, `Task`, `Agent`, `NotebookEdit`, `WebFetch`, `WebSearch`, `ExitPlanMode`) attribute to `(builtin, None)`.

### Export

Streamed downloads, audit-logged with row count.

```bash
# API
GET /api/admin/usage/export?format=csv|json|parquet&since=2026-01-01&until=2026-05-01&user_id=42&source=session

# CLI
agnes admin usage export --format csv --since 7d --out /tmp/usage.csv
agnes admin usage export --format parquet --since 30d
```

### Retention

`USAGE_EVENTS_RETENTION_DAYS` (default `0` = forever). When set > 0, the daily scheduler prune deletes `usage_events` rows older than that many days. Rollup tables (`usage_tool_daily`, `usage_plugin_daily`) are not pruned.

Manual prune:
```bash
agnes admin usage prune
# or
POST /api/admin/usage/prune
```

### Ask (LLM Text-to-SQL)

Natural-language telemetry queries via Anthropic Claude Haiku. Requires `ANTHROPIC_API_KEY`.

```bash
agnes admin ask "top 10 most-used skills last 7 days"
agnes admin ask "which users haven't run anything in 14 days"
agnes admin ask "top tools by error rate this month"
```

The server translates the question to SELECT SQL, runs it read-only, and returns the SQL + result table. A SELECT-only validator blocks any mutating statement. Both the question and the generated SQL are audit-logged with row count.

### Manual reprocess

After a `USAGE_PROCESSOR_VERSION` bump or a schema migration:

```bash
agnes admin usage reprocess
# or
POST /api/admin/usage/reprocess
```

This clears `session_processor_state` rows for the usage processor, `usage_events`, `usage_session_summary`, and rollup tables in one transaction, then triggers fresh extraction. The verification processor is untouched.

## 6. Privacy posture

- **Per-session opt-out**: `agnes mark-private` excludes the current Claude Code session from `agnes push`. The CLI statusline shows 🔒 when a session is marked private. The server never receives that session's JSONL.
- **Per-user opt-out**: not implemented in v1. If needed: env var or a `users.telemetry_opt_out` column — design parked for v2.
- **What "private" means**: the JSONL for that session is not uploaded. Previously uploaded sessions are not deleted. The opt-out is per-session, not retroactive.
- **Audit log**: every admin action, every telemetry export, and every `agnes admin ask` query is written to `audit_log`. Visible at `/admin/activity`.
- **PostHog (optional)**: opt-in via `POSTHOG_API_KEY`. Sends backend exceptions, frontend errors, and masked session replay (sensitive CSS selectors auto-masked). LLM payloads are off by default — set `POSTHOG_LLM_PAYLOADS=1` to enable.

## 7. Operator daily routine

**Morning health check:**
```bash
# Terminal
agnes admin activity health

# Browser
/admin/activity   — health pulse + audit timeline
```

**Usage insights:**
- `/marketplace` → Most Popular tab — top 8 cards by invocations over the last 30 days.
- Skill invocation chips on each card: `🔥 1,243 uses · ↑ 24%` (week-over-week trend; suppressed when prior week < 3 invocations).
- `/admin/users/<id>` → Sessions — drill into a specific analyst's session history (start time, duration, tool calls, errors, model). Per-file `.jsonl` or bulk `.zip` download (both audit-logged).

**Ad-hoc questions:**
```bash
agnes admin ask "how many sessions ran yesterday"
agnes admin ask "which skills were used more than 50 times last week"
agnes admin ask "show me error rates per tool over the last 30 days"
```

**Routine actions:**
- Check `/admin/marketplaces` after a plugin repo updates — trigger "Sync now" if the nightly job hasn't run yet.
- Review `/admin/store` approval queue if analysts have submitted flea market plugins.
- Rotate PATs: update `${DATA_DIR}/state/.env_overlay`, then trigger a marketplace sync.

## See also

- `docs/QUICKSTART.md` — first 30-minute experience
- `docs/DEPLOYMENT.md` — Docker / Caddy / release trains
- `docs/ONBOARDING.md` — analyst onboarding flow
- `docs/HEADLESS_USAGE.md` — non-interactive / CI flows
- `docs/HOWTO/` — task-oriented analyst cookbook
- `docs/RBAC.md` — full access-control reference
