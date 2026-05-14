# Session Flow — `agnes push` to Stats

End-to-end trace of a single Claude Code session JSONL: how it lands on
the server, what processes it, what tables it populates, and which
read surfaces project it back to the user.

```
┌───────────────────────────────────────────────────────────────────────┐
│  CLAUDE CODE WORKSPACE (analyst's laptop)                              │
│                                                                        │
│   ~/<workspace>/.claude/projects/.../<session>.jsonl                   │
│   │ (lines: user/assistant turns, tool_use, tool_result, etc.)         │
│   │                                                                    │
│   │ SessionEnd hook fires (.claude/settings.json from `agnes init`)    │
│   │   →  `agnes push --quiet`                                          │
│   ▼                                                                    │
│   cli/commands/push.py:_upload_one  →  POST /api/upload/sessions       │
└───────────────────────┬───────────────────────────────────────────────┘
                        │  multipart/form-data, single .jsonl per call
                        │  Authorization: Bearer <PAT>  (or session cookie
                        │  from a browser-driven re-upload)
                        ▼
┌───────────────────────────────────────────────────────────────────────┐
│  SERVER — app/api/upload.py:upload_session                             │
│                                                                        │
│  1. Validate filename regex: ^[A-Za-z0-9._-]{1,200}$                   │
│  2. Stream body to a tempfile, abort on >50 MB (OOM guard)             │
│  3. Move temp → ${DATA_DIR}/user_sessions/<user_id>/<filename>         │
│  4. audit_log row: action='session.upload',                            │
│                    params={filename, bytes},                           │
│                    user_id=<caller>, client_kind='cli'|'web'           │
│                                                                        │
│   File is now on disk + has a single audit row.                        │
└───────────────────────┬───────────────────────────────────────────────┘
                        │
                        ▼
                ${DATA_DIR}/user_sessions/<user_id>/<sessionfile>.jsonl
                        │
                        │   (asynchronous — two independent processors)
        ┌───────────────┴───────────────┐
        ▼                               ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│  USAGE PROCESSOR         │  │  VERIFICATION PROCESSOR  │
│  cadence: 5 min          │  │  cadence: 15 min         │
│  services/scheduler      │  │  services/scheduler      │
│  hits POST               │  │  hits POST               │
│  /api/admin/run-         │  │  /api/admin/run-         │
│  session-processor       │  │  session-processor       │
│  ?processor=usage        │  │  ?processor=verification │
│                          │  │                          │
│  services/session_       │  │  LLM-driven extraction   │
│  processors/usage.py     │  │  of factual claims       │
│                          │  │                          │
│  scan_unprocessed_for    │  │  scan_unprocessed_for    │
│  → walks                 │  │  → walks                 │
│  ${DATA_DIR}/            │  │  ${DATA_DIR}/            │
│  user_sessions/*/*.jsonl │  │  user_sessions/*/*.jsonl │
│  (dir name = username    │  │  (same)                  │
│  column value, in this   │  │                          │
│  codebase = user_id UUID)│  │                          │
│                          │  │                          │
│  per-file:               │  │  per-file:               │
│    parse_jsonl           │  │    parse_jsonl           │
│    iter_events           │  │    LLM call → claims     │
│    compute_summary       │  │                          │
│       (sums message.     │  │                          │
│        usage.* tokens    │  │                          │
│        per assistant     │  │                          │
│        turn)             │  │                          │
│                          │  │                          │
│  WRITES:                 │  │  WRITES:                 │
│  ─ usage_events          │  │  ─ knowledge_items       │
│  ─ usage_session_summary │  │  ─ session_processor_    │
│  ─ session_processor_    │  │       state(processor_   │
│       state(processor_   │  │       name='verification')│
│       name='usage')      │  │                          │
│  ─ usage_tool_daily      │  │  ─ items_extracted count │
│  ─ usage_plugin_daily    │  │       on state row       │
│  (rollups, incremental)  │  │                          │
└──────────────────────────┘  └──────────────────────────┘
                  │                          │
                  └────────────┬─────────────┘
                               │
                               ▼ (data is now queryable)
```

## Tables written

| Table | Writer | Grain | Key columns for this flow |
|-------|--------|-------|---------------------------|
| `audit_log` | upload endpoint + manifest endpoint + every processor run | one row per action | `user_id`, `action` (e.g. `session.upload`, `manifest.fetch`), `params`, `client_kind`, `timestamp` |
| `usage_events` | UsageProcessor | one row per event inside the JSONL | `session_file`, `username` (= user_id), `event_type` (`tool_use`/`skill`/`subagent`/`mcp_call`/`slash_command`), `tool_name`, `cwd`, `model`, `occurred_at` |
| `usage_session_summary` | UsageProcessor | one row per JSONL | `session_file` (PK), `username`, `started_at`, `ended_at`, `user_messages`, `tool_calls`, `primary_model`, **`input_tokens`**, **`output_tokens`**, **`cache_read_tokens`**, **`cache_creation_tokens`** (v44) |
| `usage_tool_daily`, `usage_plugin_daily` | UsageProcessor (rollup phase) | daily aggregate | drives admin telemetry trend charts |
| `session_processor_state` | runner (both processors) | one row per `(processor_name, session_file)` | `processed_at`, `items_extracted`, `file_hash` — drives "pending vs processed" per processor |
| `knowledge_items` | VerificationProcessor | one row per extracted claim | populates `/corporate-memory` |
| `users.last_pull_at` | `/api/sync/manifest` UPDATE | column on users row | drives /home status frame "Last sync" card |

## Read surfaces

Every read joins these tables; none has its own ingestion path.

| Surface | Audience | Tables read | Filter |
|---------|----------|-------------|--------|
| `/profile/sessions` | self | FS scan + `session_processor_state` + `usage_session_summary` | `user_id` (dir name) |
| `/me/stats` Sessions | self | same as above (`get_user_sessions_view`) | `user_id` |
| `/me/stats` Tokens | self | `usage_session_summary` | `username = user_id` |
| `/me/stats` Data access | self | `audit_log` | `user_id=? AND action LIKE 'query.%'` |
| `/me/stats` Sync activity | self | `audit_log` + `users.last_pull_at` | `user_id=? AND action IN ('sync.*','manifest.*')` |
| `/home` status frame | self | `usage_session_summary` + `usage_events` + `users.last_pull_at` | `username=? AND started_at>=now()-window` |
| `/admin/sessions` | admin | `usage_session_summary` | none (cross-user); facets group by `username` |
| `/admin/telemetry` | admin | `usage_events` + `usage_tool_daily` + `usage_plugin_daily` | none |
| `/admin/activity` | admin | `audit_log` + `sync_history` + `session_processor_state` (verification slice) | optional filters |

**Single source of truth invariant:** every per-user view and every
cross-user view that talks about the same metric reads from the same
column on the same table. Self-views add `WHERE user_id=?` (audit_log)
or `WHERE username=?` (usage_*). Cross-user admin views skip the
filter.

If you build a new view that needs session data, call
`app.api._session_view.get_user_sessions_view(conn, user_id)` rather
than rolling a new FS+DB join — it's the shared helper the three
self/admin readers all converge on.

## Cadences in production

Wired in `services/scheduler/__main__.py`:

| Job | Cadence | Endpoint |
|-----|---------|----------|
| Usage processor | 5 min (300 s) | `POST /api/admin/run-session-processor?processor=usage` |
| Verification processor | 15 min (900 s) | `POST /api/admin/run-session-processor?processor=verification` |

The two are independent: usage typically reaches "processed" well
before verification on any given session. A "pending" badge on
`/profile/sessions` for **Verification** while **Usage** shows
"processed" is the expected steady state for the first 0-10 minutes
after upload — it does NOT mean the pipeline is broken.

## Local-dev validation recipe

To validate the full path end-to-end:

```bash
# 1. Boot dev server (worktree-local data dir, isolated from main checkout)
export DATA_DIR=/tmp/agnes-validate-data
export SESSION_DATA_DIR=${DATA_DIR}/user_sessions
mkdir -p ${DATA_DIR}/{state,analytics,extracts,store}
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8005

# 2. Onboard the dev user so /home renders the status frame
curl -X POST http://127.0.0.1:8005/api/me/onboarded \
  -H 'Content-Type: application/json' \
  -d '{"source":"self_acknowledged","onboarded":true}'

# 3. Upload a realistic JSONL via /api/upload/sessions (what agnes push does)
curl -X POST http://127.0.0.1:8005/api/upload/sessions \
  -F "file=@/path/to/session.jsonl"

# 4. Trigger the usage processor (scheduler does this every 5 min in prod)
curl -X POST 'http://127.0.0.1:8005/api/admin/run-session-processor?processor=usage'

# 5. Verify the data shows up on every read surface
curl -s http://127.0.0.1:8005/api/me/stats/sessions          | jq .total
curl -s http://127.0.0.1:8005/api/me/stats/tokens?days=7     | jq .totals
curl -s http://127.0.0.1:8005/api/me/home-stats?window=7d    | jq .
curl -s http://127.0.0.1:8005/api/admin/sessions/list        | jq '.rows|length'
curl -s http://127.0.0.1:8005/api/admin/sessions/kpis        | jq .
curl -s http://127.0.0.1:8005/api/admin/telemetry/summary    | jq .top_tools
```

Self and admin numbers MUST match for a single-user instance because
they read the same physical tables filtered differently.
