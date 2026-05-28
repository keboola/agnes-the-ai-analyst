# Universal MCP — POC walkthrough

> **Status:** Proof-of-concept on branch `zs/universal-mcp-poc` (off `mf/agnes-cowork`).
> **RFC:** [keboola/agnes-the-ai-analyst#461](https://github.com/keboola/agnes-the-ai-analyst/issues/461)
> **Out of scope for the POC:** secrets vault, Policy Engine, PII redaction, write/mutating tools, per-table outbound tool generation, data_packages `related_tools` junction. See the RFC for the full design — this doc only covers what's implemented.

## What the POC delivers

Agnes can now ingest data from **any external MCP server** as a first-class source type, in two modes:

- **Materialize** — scheduled call to a bulk-list tool, result lands in `analytics.duckdb` as a regular table reachable through `agnes query`, `/api/query`, and the existing data_packages flow.
- **Passthrough** — at AI call time, the Agnes MCP server (Monika's `cli/mcp/server.py` + `app/api/mcp_http.py`) forwards the call live to the upstream MCP and returns the result. The tool surfaces on the Agnes endpoint with its upstream JSON input schema intact, so Claude Desktop / Cursor / Cline see typed parameters.

Setup happens through any of three channels — admin web UI, `agnes admin mcp` CLI, or direct REST calls — all backed by the same admin endpoints. An external AI assistant (Claude Desktop talking to a human admin) can drive the REST endpoints conversationally; no Agnes-side LLM integration required.

## Architecture map

```
UPSTREAM MCP SERVER                   AGNES (this branch)             AI CLIENTS
─────────────────────────────────────────────────────────────────────────────────
                                                                  
┌──────────────────────┐              ┌────────────────────────┐   ┌──────────────┐
│ Mock CRM MCP         │◄──────┐      │ connectors/mcp/        │   │ Claude       │
│ (or any other        │       │      │  client.py             │   │ Desktop      │
│  MCP server)         │       ├──────┤  classifier.py         │   │ Cursor       │
└──────────────────────┘       │      │  extractor.py          │   │ Cline        │
                               │      └────────────┬───────────┘   └──────┬───────┘
                               │                   │                      │
                               │                   ▼                      │
                               │      ┌────────────────────────┐          │
                               │      │ system.duckdb (v61)    │          │
                               │      │  mcp_sources           │          │
                               │      │  tool_registry         │          │
                               │      │  tool_grants           │          │
                               │      └────────────┬───────────┘          │
                               │                   │                      │
                               │                   ▼                      │
                               │      ┌────────────────────────┐          │
                               │      │ MATERIALIZE path:      │          │
                               │      │  extract.duckdb +      │          │
                               │      │  parquet → orchestrator│          │
                               │      │  → analytics.duckdb    │          │
                               │      └────────────┬───────────┘          │
                               │                   │                      │
                               │      ┌────────────┴───────────┐          │
                               │      │ Outbound MCP server    │◄─────────┘
                               │      │  (mcp_http.py)         │
                               │      │  - static: catalog,    │
                               │      │    schema, query, ...  │
                               │      │  - DYNAMIC: passthrough│
                               │      │    tools generated     │
                               │      │    from tool_registry  │
                               └──────┤                        │
                                      │ Admin REST + UI + CLI: │
                                      │ /api/admin/mcp-sources │
                                      │ /api/admin/mcp-tools   │
                                      │ /admin/mcp-sources     │
                                      │ agnes admin mcp ...    │
                                      └────────────────────────┘
```

## What's on disk

| Layer | Files |
|---|---|
| Schema (v61) | `src/db.py` — `_v60_to_v61` migration adds `mcp_sources`, `tool_registry`, `tool_grants` |
| Repos | `src/repositories/mcp_sources.py`, `src/repositories/tool_registry.py` |
| Inbound connector | `connectors/mcp/{__init__,client,classifier,extractor}.py` |
| Outbound generator | `app/api/mcp/{__init__,tools_generator}.py`, hooked into `app/api/mcp_http.py:make_sse_app()` |
| Admin REST | `app/api/admin_mcp.py` (16 routes under `/api/admin/mcp-sources` + `/api/admin/mcp-tools`) |
| Admin UI | `app/web/templates/admin_mcp_{sources,source_detail,tool_grants}.html` + `app/web/router.py` shell routes + nav entry in `app/web/templates/_app_header.html` |
| Admin CLI | `cli/commands/admin_mcp.py` — `agnes admin mcp source/tool …` |
| Mock fixture | `scripts/dev/mock_crm_mcp_server.py` — 15 mock accounts, 20 contacts; tools: `listAccounts`, `searchContacts`, `getAccount` |
| Headless e2e demo | `scripts/dev/poc_mcp_e2e.py` — runs the whole pipeline without a live server (for CI / smoke) |

## End-to-end walkthrough

### Option A — headless (no server required)

The fastest way to see the pipeline run end-to-end:

```bash
cd <agnes-checkout>
.venv/bin/python scripts/dev/poc_mcp_e2e.py
```

This:

1. Creates a fresh temp `system.duckdb` migrated to v61
2. Registers the local mock CRM as an MCP source (`stdio` subprocess)
3. Connects, runs `tools/list`, classifies the 3 tools (listAccounts → materialize, searchContacts + getAccount → passthrough)
4. Persists the proposals into `tool_registry`
5. Runs the materialize extractor — produces `extract.duckdb` with the `listaccounts` table
6. Spins up an in-process FastMCP, registers the passthrough tools from `tool_registry`, calls one via the SDK, and prints the round-tripped CRM data

Use this when you just want to verify the connector pipeline. No login, no auth, no Claude Desktop required.

### Option B — full stack with Agnes running

This is the path a real admin would follow.

**1. Boot Agnes locally:**

```bash
cp config/instance.yaml.example config/instance.yaml   # if not already present
cp config/.env.template .env                            # fill SESSION_SECRET + SEED_ADMIN_EMAIL
.venv/bin/uvicorn app.main:app --reload
```

Log in once at `http://localhost:8000` so the seed admin record is bound to your user.

**2. Mint a PAT** so the CLI can authenticate:

- Web UI: `/admin/tokens` → "Create token"
- CLI: `agnes auth token create poc-cli` and copy the printed PAT into `~/.config/agnes/token.json` (the `agnes auth login` flow handles this for you)

**3. Register the mock CRM as a source via the CLI:**

```bash
agnes admin mcp source add mock-crm \
    --transport stdio \
    --command "$(pwd)/.venv/bin/python" \
    --arg "$(pwd)/scripts/dev/mock_crm_mcp_server.py"

agnes admin mcp source list
```

(Web UI equivalent: `/admin/mcp-sources` → "Add MCP source".)

**4. Inspect what the upstream offers and what the classifier suggests:**

```bash
agnes admin mcp source test mock-crm           # ok / connect error
agnes admin mcp source introspect mock-crm     # raw tools/list from upstream
agnes admin mcp source classify mock-crm       # heuristic proposal table
```

**5. Accept the classifier suggestions** (or pick tools one by one via the UI's per-tool radios):

```bash
agnes admin mcp source register-suggested mock-crm
agnes admin mcp tool list --source mock-crm
```

You should see:
- `listaccounts` — materialize, schedule `every 6h`
- `mock-crm.searchContacts` — passthrough
- `mock-crm.getAccount` — passthrough

**6. Run the materialize once on demand** (the scheduler will pick it up on its next tick too):

```bash
agnes admin mcp source materialize mock-crm
```

This writes `data/extracts/mock-crm/extract.duckdb` + `data/mock-crm/data/listaccounts.parquet`. The orchestrator ATTACHes it into `analytics.duckdb` on its next rebuild — trigger one explicitly with `curl -X POST http://localhost:8000/api/sync/trigger`.

**7. Query the materialized table** through any of Agnes's existing surfaces:

```bash
agnes query "SELECT country, COUNT(*) FROM listaccounts GROUP BY 1"
# or curl http://localhost:8000/api/query -d '{"sql": "SELECT … FROM listaccounts"}'
```

**8. Connect Claude Desktop to the Agnes MCP endpoint** to use the passthrough tools.

In Claude Desktop's `claude_desktop_config.json` (the cowork bundle from `mf/agnes-cowork` writes this automatically when you run `agnes init`; if you're wiring it manually):

```json
{
  "mcpServers": {
    "agnes": {
      "command": "/abs/path/to/.venv/bin/agnes",
      "args": ["mcp"],
      "type": "stdio"
    }
  }
}
```

(The stdio MCP server lives in `cli/mcp/server.py`. For a remote/cowork VM use `app/api/mcp_http.py` exposed at `/api/mcp/sse` with a Bearer PAT — Monika's bundle covers that path.)

Restart Claude Desktop, open a chat, and `tools/list` should now include `mock-crm.searchContacts` and `mock-crm.getAccount` alongside the static Agnes tools. Ask Claude something like:

> "Use the mock-crm.searchContacts tool to find anyone named Tony, then mock-crm.getAccount for their account id."

Claude calls each tool, Agnes forwards live to the mock CRM, the JSON response is returned, and Claude composes an answer. The data path traversed:

```
Claude Desktop → cli/mcp/server.py (stdio) → /api/mcp REST → app/api/mcp_http.py (FastMCP)
              → app/api/mcp/tools_generator.py wrapper → connectors/mcp/client.py
              → spawns python scripts/dev/mock_crm_mcp_server.py via stdio
              → response back up the chain
```

### Replacing the mock CRM with a real one

Same flow — just point the source at a different MCP server:

```bash
# Real CRM exposed over HTTP/SSE
agnes admin mcp source add real-crm \
    --transport sse \
    --url https://your-crm-mcp.internal/sse \
    --auth-method bearer \
    --auth-secret-env REAL_CRM_PAT
```

(HTTP/SSE transport in `connectors/mcp/client.py` is now wired — `_open_session` dispatches to `mcp.client.streamable_http.streamablehttp_client` for `transport='http'` and `mcp.client.sse.sse_client` for `transport='sse'`, with bearer/basic auth headers built from `auth_method` + `auth_secret_env`. The Streamable HTTP path is the MCP 2025-03-26+ recommended transport; the SSE path covers legacy servers. See `tests/test_mcp_client_transport.py` for the routing + header tests.)

## Schema reference (v61)

```sql
CREATE TABLE mcp_sources (
    id              VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL UNIQUE,
    transport       VARCHAR NOT NULL,           -- stdio | http | sse
    command         VARCHAR,                    -- stdio: executable path
    args            JSON,                       -- stdio: arg array
    url             VARCHAR,                    -- http/sse: endpoint
    auth_method     VARCHAR,                    -- none | bearer | basic
    auth_secret_env VARCHAR,                    -- name of env var with secret
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
    updated_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE tool_registry (
    tool_id        VARCHAR PRIMARY KEY,         -- "<source_id>.<exposed_name>"
    source_id      VARCHAR NOT NULL,
    original_name  VARCHAR NOT NULL,            -- as upstream exposes
    exposed_name   VARCHAR NOT NULL,            -- as Agnes exposes
    mode           VARCHAR NOT NULL,            -- materialize | passthrough
    table_id       VARCHAR,                     -- materialize → FK to table_registry
    input_schema   JSON,                        -- MCP inputSchema verbatim
    description    VARCHAR,
    mutating       BOOLEAN NOT NULL DEFAULT false,
    pii_fields     JSON,                        -- list of column names to redact
    rate_limit_pm  INTEGER,
    schedule       VARCHAR,                     -- materialize only
    enabled        BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMP NOT NULL DEFAULT current_timestamp,
    updated_at     TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE tool_grants (
    tool_id   VARCHAR NOT NULL,
    group_id  VARCHAR NOT NULL,
    PRIMARY KEY (tool_id, group_id)
);
```

## Known limitations / follow-ups before merge

- **Per-table outbound tools** not implemented (RFC §7). Only generic `query(sql)` and the dynamic passthrough tools surface. AI can still query materialized tables via SQL.
- **No vault** — secrets go through env-var names (`auth_secret_env` pattern, same as existing Keboola/BigQuery connectors). RFC §4 vault — which should also cover **per-user credential passthrough** so that calls to upstream MCP can run under the calling analyst's identity (their Notion/Slack/Linear OAuth token), not a single shared server-wide secret — is follow-up.
- **No Policy Engine** — read-only is implicit (no admin REST endpoint accepts `mutating=true`). RFC §3 enforcement layer is follow-up.
- ~~**stdio MCP server** (`cli/mcp/server.py`) is NOT extended with passthrough tools.~~ Done: `cli/mcp/_dynamic_passthrough.py` registers them on the stdio server at startup, forwarding through the new `GET /api/mcp/passthrough/tools` + `POST /api/mcp/passthrough/tools/{tool_id}/call` REST surface (`app/api/mcp_passthrough.py`). Stdio MCP and HTTP MCP now expose the same passthrough tools.
- **data_packages `related_tools` junction** (RFC §6) — not implemented. Passthrough tools are visible to any AI client with `tool_grants`; package-driven subscription wiring is follow-up.
- **No v60→v61 migration test** — manually verified the migration ladder + `_SYSTEM_SCHEMA` parity, but a `tests/test_schema_v60_to_v61_migration.py` should be added before merge to main.
- **CHANGELOG** — not bumped (POC branch). Add an `## [Unreleased]` entry before opening a PR.
