# API Reference

> Maintained alongside the code — CI checks that every public endpoint is listed here
> (`tests/test_api_docs_coverage.py`). For the always-current interactive reference, see
> [Swagger UI](/docs) and [ReDoc](/redoc) (login required).

> **Three surfaces, one source.** This guide is reachable from
> [`/documentation/api`](/documentation/api) (web), `agnes docs api` (CLI), and the
> `documentation_api` MCP tool (agent / Claude Desktop). All three render the same
> `docs/api-reference.md` so a public endpoint is documented in lockstep across
> the surfaces an analyst or agent might reach for.

---

## Contents

1. Authentication
2. Environments
3. Tables — `/api/admin/registry`
4. Data Packages — `/api/admin/data-packages`
5. Server config — `/api/admin/server-config`
6. Gotchas
7. End-to-end recipes
8. OpenAPI / Swagger
9. Endpoint inventory

---

## 1. Authentication

All admin endpoints require a Personal Access Token (PAT) sent as a Bearer header.
PATs are **per-instance** — a token issued on one deployment returns `HTTP 401 "User not found"`
on any other instance.

```bash
PAT=<your-personal-access-token>
```

Example using curl:

```bash
curl -s -X GET "https://{your-instance}/api/admin/registry" \
  -H "Authorization: Bearer $PAT"
```

---

## 2. Environments

Agnes is typically deployed as two instances: a development instance and a production
instance. Both expose the **same API surface**. Schema migrations may roll to dev first.

| Environment | Base URL | Notes |
|---|---|---|
| Dev | `https://dev.{your-instance}` | Schema migrations land here first |
| Prod | `https://{your-instance}` | Stable; catalog state may be wiped on redeploy (see Gotcha #16) |

Tokens are per-instance and are not interchangeable across dev and prod.

---

## 3. Tables — `/api/admin/registry`

A **table** is a single physical (BigQuery, Keboola, local parquet, etc.) or virtual
asset that the server knows how to query. Tables are the unit of data access; packages
are the unit of curation and user-facing discovery.

### 3.1 Endpoints

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/api/admin/registry` | — | List all registered tables (includes extended-doc + column fields) |
| `GET` | `/api/v2/catalog` | — | Public-facing catalog (same data, no admin fields) |
| `POST` | `/api/admin/register-table` | see §3.3 | Register a new table |
| `POST` | `/api/admin/register-table/precheck` | see §3.3 | Validate a registration payload without committing |
| `PUT` | `/api/admin/registry/{table_id}` | see §3.2 | Update **operational** fields (idempotent partial) |
| `PATCH` | `/api/admin/registry/{table_id}/docs` | see §3.5 | Update **extended LLM-facing docs** (grain, gotchas, …) |
| `DELETE` | `/api/admin/registry/{table_id}` | — | Unregister |
| `GET` | `/api/admin/metadata/{table_id}` | — | Get per-column metadata (see §3.6) |
| `POST` | `/api/admin/metadata/{table_id}` | see §3.6 | Save per-column metadata |
| `POST` | `/api/admin/metadata/{table_id}/push` | — | Push saved column metadata downstream (no body) |
| `POST` | `/api/admin/run-bq-metadata-refresh` | — | Refresh column metadata from BigQuery (no body) |

### 3.2 Editable fields (PUT)

| Field | Type | Notes |
|---|---|---|
| `name` | string | Display name. **Editable in-place via PUT — does NOT change the registry `id`** (the id is fixed at register-time; see §3.4 and Gotcha #11). Use this to normalize casing or rename the display name without re-registering. |
| `description` | string | Free-form blurb; LLM-facing |
| `bucket` | string | **Display-only** for BigQuery `query_mode=remote` tables. Renaming does NOT affect SQL path resolution. |
| `source_table` | string | **BARE physical table name** (e.g. `orders_daily`) — see the standard below |
| `query_mode` | enum | `remote`, `local`, `materialized` |
| `sync_strategy` | string | For local/materialized tables |
| `primary_key` | string or string[] | Accepts a bare string (coerced to `[string]`) or a list for composite keys |
| `sync_schedule` | string | cron expression |
| `profile_after_sync` | bool | |

> **`source_table` standard: BARE table name, `bucket` = dataset.**
> The server resolves the physical path as `{server-config default project}.{bucket}.{source_table}`,
> so `source_table` carries ONLY the table name (e.g. `orders_daily`) and `bucket` carries
> the dataset (e.g. `analytics`). Do NOT write the full `project.dataset.table` path —
> the full-path form is non-standard and may not resolve correctly on all builds.

> **PUT handles operational fields only.** The extended LLM-facing doc fields
> (`grain`, `things_to_know`, `gotchas`, `pairs_well_with`, `sample_questions`,
> `platforms`, `partition_col`, `history`) are returned by `GET /api/admin/registry`
> but are **not** in the `UpdateTableRequest` schema — `PUT` silently ignores them.
> Write them via `PATCH /api/admin/registry/{table_id}/docs` instead (see §3.5).
> Per-column descriptions are a separate layer (see §3.6).

### 3.3 Example — update description + bucket

```bash
curl -s -X PUT \
  "https://{your-instance}/api/admin/registry/orders_daily" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "One row per order, partitioned by order date.",
    "bucket": "analytics"
  }'
# {"id":"orders_daily","updated":["description","bucket"]}
```

> **Renaming a table's display name in place (no re-register).** A `PUT` carrying just
> `{"name": "…"}` updates the `name` field **without changing the `id`, the docs, or package
> membership** — the id is fixed at register-time and is decoupled from later name edits.
> The id-derivation in Gotcha #11 fires at register (POST) ONLY, not on subsequent PUTs.

### 3.4 Example — register a new BigQuery table

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/register-table" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "sales_orders",
    "source_type": "bigquery",
    "source_table": "sales_orders",
    "bucket": "analytics",
    "query_mode": "remote",
    "description": "Short, LLM-facing blurb (1–3 sentences)."
  }'
# response: {"id": "sales_orders", ...}
```

**Four registration rules:**

1. **The registry `id` is DERIVED from `name`** (lower-cased). A passed `id` field is **ignored**.
   To get id `sales_orders`, set `name: "sales_orders"`.
2. **`name` must be a DuckDB-safe identifier** — `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`. **No hyphens
   or special characters** → **HTTP 422** (generic check, fires first). For BigQuery remote tables,
   a space in `name` is not coerced — the BQ raw-name check rejects it with **HTTP 400**.
   Put friendly text in `description`.
3. **BigQuery remote tables require `bucket`** (the BigQuery dataset) — omitting it → `bigquery: 'bucket' is required`.
4. **`source_table` is the BARE table name** (§3.2 standard) — the dataset goes in
   `bucket`, the project comes from server config. Not the full `project.dataset.table` path.

To validate a payload without committing, POST the same body to
`/api/admin/register-table/precheck` first.

### 3.5 Extended table docs — `PATCH /api/admin/registry/{table_id}/docs`

The single `description` field is the short blurb. **Rich, LLM-facing table
documentation** lives behind a dedicated `PATCH` endpoint with the
`TableDocsRequest` schema. These are the fields returned by `GET /api/admin/registry`
that are not writable via `PUT` (see the note under §3.2).

| Field | Type | Notes |
|---|---|---|
| `grain` | string | One-line grain statement, e.g. `"1 row per order"` |
| `things_to_know` | string | Extended free-text writeup — quality filters, conventions, caveats |
| `gotchas` | object[] | Array of `{"body": "...", "key": false}` — `body` (string) required, `key` (bool) optional. **Plain strings are rejected** with `model_attributes_type`. Max 8 entries. |
| `pairs_well_with` | string[] | Related table **IDs** for cross-table analysis hints |
| `sample_questions` | string[] | Prompt seeds (table-level equivalent of a package's `example_questions`) |
| `platforms` | string[] | Applicable platforms, e.g. `["web","app"]`. Max 8 entries. |
| `partition_col` | string | Partition column name, e.g. `"event_date"` |
| `history` | string | Retention / history note |

```bash
curl -s -X PATCH \
  "https://{your-instance}/api/admin/registry/sales_orders/docs" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "grain": "1 row per order",
    "things_to_know": "Standard quality filters apply. Join on order_id across all order-grain tables.",
    "gotchas": [
      {"body": "Use DATE(order_created_at) for revenue-period filters, not event_date"},
      {"body": "Refund rows share the same order_id — deduplicate on event_type when counting orders"}
    ],
    "pairs_well_with": ["order_items", "customer_segments"],
    "sample_questions": ["How many orders were placed last week?"],
    "platforms": ["web", "app"],
    "partition_col": "event_date"
  }'
```

Notes:

- Verb is **`PATCH`** (not `PUT`) and the path carries a `/docs` suffix.
- Partial update — only the keys you send are changed; omit a field to leave it untouched.
- **`gotchas` items are objects, not strings** — `{"body": "...", "key": false}`. Sending plain
  strings returns HTTP 422 `model_attributes_type`. `pairs_well_with`, `sample_questions`,
  and `platforms` ARE plain string arrays.

### 3.6 Per-column metadata — `/api/admin/metadata/{table_id}`

A separate layer holds **per-column descriptions** (the `ColumnMetadataSave` /
`ColumnMetadataItem` schema). Distinct from the table-level docs in §3.5.

| Endpoint | Purpose |
|---|---|
| `GET /api/admin/metadata/{table_id}` | Returns `{"table_id": "...", "columns": [...]}` |
| `POST /api/admin/metadata/{table_id}` | Save the `columns` array (replaces existing) |
| `POST /api/admin/metadata/{table_id}/push` | Publish saved metadata downstream (no body) |
| `POST /api/admin/run-bq-metadata-refresh` | Re-pull column metadata from BigQuery (no body) |

Each column item:

| Field | Type | Notes |
|---|---|---|
| `column_name` | string | required |
| `basetype` | string | data type (nullable) |
| `description` | string | LLM-facing column description (nullable) |
| `confidence` | string | provenance/quality marker for the description |

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/metadata/sales_orders" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "columns": [
      {"column_name": "order_id",     "basetype": "STRING", "description": "Unique order identifier; primary join key.", "confidence": "high"},
      {"column_name": "event_date",   "basetype": "DATE",   "description": "Partition column — always filter on this.", "confidence": "high"},
      {"column_name": "order_status", "basetype": "STRING", "description": "Current status of the order lifecycle.", "confidence": "high"}
    ]
  }'
```

---

## 4. Data Packages — `/api/admin/data-packages`

A **data package** is a thematic bundle of tables exposed to end users and LLMs as
a single browsable entity, with rich metadata — short description, long description,
guardrail bullets, example questions, icon, color, and a cover image.

### 4.1 Endpoints

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/api/admin/data-packages` | — | List all packages (flat array). Accepts `?include_table_ids=true` to embed table id arrays. |
| `POST` | `/api/admin/data-packages` | see §4.3 | Create — `name` + `slug` required |
| `GET` | `/api/admin/data-packages/{pkg_id}` | — | Get one — includes `tables` array and `related_tools` |
| `PUT` | `/api/admin/data-packages/{pkg_id}` | see §4.3 | Update (idempotent partial) |
| `DELETE` | `/api/admin/data-packages/{pkg_id}` | — | Soft-delete (reversible via /restore) |
| `POST` | `/api/admin/data-packages/{pkg_id}/restore` | — | Undo a soft-delete |
| `POST` | `/api/admin/data-packages/{pkg_id}/tables` | `{"table_id": "..."}` | Attach table to package |
| `DELETE` | `/api/admin/data-packages/{pkg_id}/tables/{table_id}` | — | Detach table |
| `POST` | `/api/admin/data-packages/{pkg_id}/tools` | `{"tool_id": "..."}` | Attach MCP tool to package |
| `DELETE` | `/api/admin/data-packages/{pkg_id}/tools/{tool_id}` | — | Detach MCP tool |
| `GET` | `/api/data-packages/{slug}` | — | Public-facing view (no admin) |
| `POST` | `/api/admin/uploads/cover-image` | multipart `file` | Upload a cover image → `{"url": "/uploads/covers/<sha256>.<ext>", "content_type": "...", "size": <bytes>}`. Extension mirrors the uploaded file type (not always `.png`). Storage is content-addressed — identical bytes always produce the same path. Set the returned `url` on a package's `cover_image_url`. |

### 4.2 Editable fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes (on create) | Human-readable name |
| `slug` | string | yes (on create) | URL-safe slug; immutable after create (see Gotcha #9) |
| `description` | string | — | Short blurb; ~210 chars / two sentences works well |
| `long_description` | string | — | Extended writeup; max 4000 chars |
| `icon` | string | — | Single emoji glyph (e.g. `💰`, `🔍`) |
| `color` | string | — | 6-digit hex value (e.g. `#10b981`) — other formats return 422 |
| `cover_image_url` | string | — | URL or **data URI**. Send `""` (empty string) to clear the cover image. |
| `status` | string | — | One of `prod`, `poc`, `coming-soon`, `draft`. `coming-soon` hides the package from non-admin users. |
| `category` | string | — | Free-text category label. Send `""` to clear. |
| `owner_name` | string | — | |
| `owner_team` | string | — | |
| `tags` | string[] | — | Max 8 entries, 30 chars each |
| `when_to_use` | string[] | — | Guardrail bullets shown to LLM users; max 8, 200 chars each |
| `when_not_to_use` | string[] | — | Guardrail bullets; max 8, 200 chars each |
| `example_questions` | string[] | — | Rendered in the UI as example questions; max 12, 200 chars each |

### 4.3 Example — create a package

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/data-packages" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Core Analytics",
    "slug": "core-analytics",
    "description": "Order and revenue data for the entire platform.",
    "icon": "💰",
    "color": "#10b981"
  }'
```

### 4.4 Example — update a package

```bash
curl -s -X PUT \
  "https://{your-instance}/api/admin/data-packages/pkg_xxxxxxxxxxxxxxxx" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Every order the platform has ever processed.",
    "icon": "💰",
    "color": "#0284c7",
    "when_to_use": [
      "Revenue, margin, refund, or order-volume questions",
      "Anything that requires audit-grade per-order numbers"
    ],
    "when_not_to_use": [
      "Session-level traffic analysis — use the Traffic package instead"
    ],
    "example_questions": [
      "What was total revenue last month?",
      "How many orders were placed in Q1?"
    ]
  }'
```

The full updated package object is returned.

### 4.5 Example — attach a table to a package

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/data-packages/pkg_xxxxxxxxxxxxxxxx/tables" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{"table_id": "sales_orders"}'
```

The table must already be registered via `/api/admin/registry`.
Response: `{"added": true}` (idempotent — `{"added": false}` if already attached).

### 4.6 Generating an SVG cover image (data URI)

The `cover_image_url` field accepts data URIs, allowing self-contained inline covers
with no external hosting requirement.

```python
import urllib.parse

def build_cover(name: str, color_dark: str, color_light: str) -> str:
    # IMPORTANT: XML-escape `&`, `<`, `>` in the visible name (see Gotcha #1)
    safe = (name.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="274" height="120" viewBox="0 0 274 120">'
        '<defs>'
        f'<linearGradient id="g" x1="0" y1="0" x2="274" y2="120" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{color_dark}"/>'
        f'<stop offset="1" stop-color="{color_light}"/>'
        '</linearGradient>'
        '</defs>'
        '<rect width="274" height="120" fill="url(#g)"/>'
        f'<text x="14" y="70" font-family="Inter, sans-serif" font-size="24" '
        f'font-weight="700" fill="#ffffff">{safe}</text>'
        '</svg>'
    )
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg, safe="")
```

Then PUT it as a string field:

```python
import json, subprocess
cover = build_cover("Core Analytics", "#064e3b", "#10b981")
subprocess.run([
    "curl", "-s", "-X", "PUT",
    f"https://{{your-instance}}/api/admin/data-packages/{{pkg_id}}",
    "-H", f"Authorization: Bearer {PAT}",
    "-H", "Content-Type: application/json",
    "-d", json.dumps({"cover_image_url": cover}),
])
```

---

## 5. Server config — `/api/admin/server-config`

Platform-wide settings live here, including the data source connection configuration.

### 5.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/admin/server-config` | Return current config + `known_fields` self-documentation |
| `POST` | `/api/admin/server-config` | **Partial-patch** (preferred) — only the sections you send are changed |
| `POST` | `/api/admin/configure` | Full wizard-style setup; missing fields get nulled. Prefer the partial-patch above. |

`POST /api/admin/server-config` accepts a `sections` object keyed by section name
(`instance`, `data_source`, `email`, `telegram`, `jira`, `theme`, `server`, `auth`,
`ai`, `openmetadata`, `desktop`, `corporate_memory`, `materialize`, `guardrails`,
`marketplace`). Sections outside this allowlist are rejected with 400.

Sections `auth` and `server` are "danger zones" — mutating them requires sending
`confirm_danger: true` in the request body, since incorrect values can lock
administrators out of the instance.

### 5.2 BigQuery config shape

```json
{
  "sections": {
    "data_source": {
      "type": "bigquery",
      "bigquery": {
        "project":                   "your-gcp-data-project",
        "billing_project":           "your-gcp-billing-project",
        "location":                  "us-central1",
        "bq_max_scan_bytes":         5368709120,
        "max_bytes_per_materialize": 10737418240,
        "query_timeout_ms":          600000
      }
    }
  }
}
```

`billing_project` is a separate explicit field. When the service account can read from
the data project but must bill against a different project, set both. Mismatched
project/billing pair → `USER_PROJECT_DENIED` on every BigQuery call.

---

## 6. Gotchas

| # | Gotcha | Fix |
|---|---|---|
| 1 | `&`, `<`, `>` in SVG cover names break the XML parser — text truncates silently | XML-escape before URL-encoding: `&` → `&amp;`, `<` → `&lt;`, `>` → `&gt;` |
| 2 | `PUT` with `"cover_image_url": null` does NOT clear the field | Treated as no-change. Send `""` (empty string) to clear. |
| 3 | PATs are per-instance | Using a token from one instance against another → `HTTP 401 "User not found"` |
| 4 | `bucket` on a BigQuery `remote` table is display-only | Renaming `bucket` does not affect SQL path resolution; safe to rebrand freely |
| 5 | `restart_required: true` in server-config response is conservative | Description/bucket PUTs take effect immediately; the flag refers to settings that genuinely require a restart (auth providers, SMTP client, etc.) |
| 6 | OpenAPI spec lives at `/openapi.json`, NOT `/api/openapi.json` | The latter returns 404 |
| 7 | **Package IDs are per-instance** (server-generated `pkg_*`). The same slug may have different IDs on dev vs prod. | Always look up the destination package by **slug**, never reuse a source-instance ID. Table IDs ARE stable across instances. |
| 8 | `POST /api/admin/data-packages` create response may omit fields that were persisted (`icon`, `color`, `cover_image_url` returned as `null` even though saved). | Don't trust the POST echo — `GET /api/admin/data-packages/{pkg_id}` to verify. |
| 9 | `slug` is immutable after create — sending it on PUT is at best a no-op, at worst rejected. | Drop `slug` from PUT payloads. Only include it on POST create. |
| 10 | **Registry GET exposes more fields than PUT accepts.** `grain`, `things_to_know`, `gotchas`, `pairs_well_with`, `sample_questions`, `platforms`, `partition_col`, `history` come back in `GET /api/admin/registry` but are NOT in `UpdateTableRequest` — a `PUT` carrying them silently drops them. | Write extended docs via `PATCH /api/admin/registry/{id}/docs` (§3.5); write per-column docs via `POST /api/admin/metadata/{id}` (§3.6). |
| 11 | **`register-table` derives the registry `id` from `name`** (lower-cased) — a passed `id` is ignored. This derivation fires at register (POST) ONLY. A later `PUT {"name":…}` renames the display name **in place without re-keying the id**. | Set `name` to the identifier you want as the id (e.g. `name: "Sales_Orders"` → id `sales_orders`). To fix casing afterward, `PUT` a lowercase `name` — id stays put. |
| 12 | **`name` must be a DuckDB-safe identifier** `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`. Hyphens or special characters → **HTTP 422** (generic check, fires first). For BigQuery remote tables, a space in `name` is not coerced — the BQ raw-name check rejects it with **HTTP 400**. BigQuery remote tables also require `bucket` (the dataset) — omitting it → HTTP 422. | Use an underscore identifier for `name`; pass `bucket` = BQ dataset on register. |
| 13 | **`DELETE /api/admin/registry/{id}` can return HTTP 500** for a table whose package was deleted out from under it (dangling membership). | Detach from packages first, or remove via the admin UI. |
| 14 | **Some builds echo list-type doc fields as JSON-encoded strings.** After a `PATCH .../docs`, `GET` may return `platforms` as `'["web"]'` (a string) instead of `["web"]` (a list). | Parse the field with `json.loads` when it comes back as a string before comparing. |
| 15 | **Transient 5xx (502/503/504) are routine**, especially under concurrent publishes. A single failed call is NOT a signal that the content is wrong. | Retry with exponential backoff (e.g. 4 attempts at 1/2/4s). |
| 16 | **Prod redeploys may wipe all admin catalog state** — packages, extended docs, covers, memberships are re-seeded from the bundled default. Dev deploys typically persist state; prod deploys on some configurations don't. | All content should live in a version-controlled presentation layer and be re-applied via a publish pipeline after each deploy. Anything published only by hand is lost on the next prod redeploy. |
| 17 | **`status` has four allowed values**, not two. | `status` accepts four values: `prod`, `poc`, `coming-soon`, `draft`. |

---

## 7. End-to-end recipes

### 7.1 Onboard a new BigQuery table into an existing package

```bash
PAT=<your-personal-access-token>
BASE="https://{your-instance}"

# 1. Register the physical table
curl -s -X POST "$BASE/api/admin/register-table" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -d '{
    "name": "new_table",
    "source_type": "bigquery",
    "source_table": "new_table",
    "bucket": "analytics",
    "query_mode": "remote",
    "description": "What it is + when to use it."
  }'
# id derives from `name`; source_table is BARE + bucket=dataset (§3.2/§3.4 rules)

# 2. Attach it to a package (look up pkg_id by slug first — see Gotcha #7)
curl -s -X POST "$BASE/api/admin/data-packages/pkg_xxxxxxxxxxxxxxxx/tables" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -d '{"table_id": "new_table"}'
```

### 7.2 Mirror packages between instances (slug-keyed, idempotent upsert)

Because package IDs are per-instance (see Gotcha #7), you cannot copy
`pkg_*` IDs across environments. The reliable pattern is:

1. Read the source list and the destination list.
2. Index the destination by `slug`.
3. For each source package: if its slug exists on the destination → `PUT` (update),
   otherwise → `POST` (create).
4. Mirror table memberships separately by calling
   `POST /api/admin/data-packages/{dest_pkg_id}/tables` with the same `table_id`s
   (table IDs ARE stable across instances).

Direction-agnostic recipe:

```python
import json, subprocess

PAT_SRC  = "<source-instance-token>"
PAT_DST  = "<destination-instance-token>"
SRC_BASE = "https://dev.{your-instance}"
DST_BASE = "https://{your-instance}"

COPY_FIELDS = [
    "name", "description", "long_description", "icon", "color",
    "cover_image_url", "status", "category", "owner_name", "owner_team",
    "tags", "when_to_use", "when_not_to_use", "example_questions",
]   # NOTE: `slug` deliberately excluded — it's set on create only (Gotcha #9)

def call(method, url, pat, body=None):
    cmd = ["curl", "-s", "-X", method, url,
           "-H", f"Authorization: Bearer {pat}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    return json.loads(subprocess.check_output(cmd, text=True))

src_pkgs = call("GET", f"{SRC_BASE}/api/admin/data-packages", PAT_SRC)
dst_pkgs = call("GET", f"{DST_BASE}/api/admin/data-packages", PAT_DST)
dst_by_slug = {p["slug"]: p["id"] for p in dst_pkgs}

for src_pkg in src_pkgs:
    slug = src_pkg["slug"]
    # Always GET the full source object — list endpoint may omit some fields
    full = call("GET", f"{SRC_BASE}/api/admin/data-packages/{src_pkg['id']}", PAT_SRC)

    if slug in dst_by_slug:
        dst_id = dst_by_slug[slug]
        payload = {f: full.get(f) for f in COPY_FIELDS}
        call("PUT", f"{DST_BASE}/api/admin/data-packages/{dst_id}", PAT_DST, payload)
    else:
        payload = {f: full.get(f) for f in COPY_FIELDS}
        payload["slug"] = slug
        resp = call("POST", f"{DST_BASE}/api/admin/data-packages", PAT_DST, payload)
        dst_id = resp["id"]
        # Gotcha #8: POST echo may be incomplete — GET to verify if needed.

    # Mirror table membership (table IDs are stable cross-instance)
    src_tables = [t["id"] for t in full.get("tables", [])]
    dst_after  = call("GET", f"{DST_BASE}/api/admin/data-packages/{dst_id}", PAT_DST)
    already    = {t["id"] for t in dst_after.get("tables", [])}
    for tid in src_tables:
        if tid in already:
            continue
        call("POST", f"{DST_BASE}/api/admin/data-packages/{dst_id}/tables",
             PAT_DST, {"table_id": tid})
```

### 7.2.1 Mirror table registry (descriptions + buckets)

Table IDs are the same across instances, so this is simpler — no slug indirection:

```python
for t in call("GET", f"{SRC_BASE}/api/admin/registry", PAT_SRC)["tables"]:
    if t.get("source_type") != "bigquery":
        continue
    call("PUT", f"{DST_BASE}/api/admin/registry/{t['id']}", PAT_DST, {
        "description": t.get("description") or "",
        "bucket":      t.get("bucket"),
    })
```

---

## 8. OpenAPI / Swagger

| Path | Returns |
|---|---|
| `/openapi.json` | Full OpenAPI 3 spec |
| `/docs` | Swagger UI (HTML) |
| `/redoc` | ReDoc (HTML) |
| `/api/openapi.json` | **404 — common mistake** |

Grep the OpenAPI spec for new endpoints:

```bash
curl -s "https://{your-instance}/openapi.json" \
  -H "Authorization: Bearer $PAT" | \
  python3 -c "
import json, sys
spec = json.load(sys.stdin)
for path, methods in spec['paths'].items():
    for m in methods:
        if m in ('get','post','put','delete','patch'):
            print(f'{m.upper():6s} {path}')"
```

---

## 9. Endpoint inventory

Generated from `app.openapi()['paths']` at build time. Every `/api/*` path in the
running application appears here exactly once. This is the list `tests/test_api_docs_coverage.py`
checks against.

### `/api/admin/registry` — Table registry

- /api/admin/registry
- /api/admin/registry/{table_id}
- /api/admin/registry/{table_id}/docs

### `/api/admin/register-table` — Table registration

- /api/admin/register-table
- /api/admin/register-table/precheck

### `/api/admin/metadata` — Per-column metadata

- /api/admin/metadata/{table_id}
- /api/admin/metadata/{table_id}/push

### `/api/admin/data-packages` — Data packages

- /api/admin/data-packages
- /api/admin/data-packages/{pkg_id}
- /api/admin/data-packages/{pkg_id}/restore
- /api/admin/data-packages/{pkg_id}/tables
- /api/admin/data-packages/{pkg_id}/tables/{table_id}
- /api/admin/data-packages/{pkg_id}/tools
- /api/admin/data-packages/{pkg_id}/tools/{tool_id}

### `/api/admin/server-config` and `/api/admin/configure` — Instance configuration

- /api/admin/server-config
- /api/admin/configure

### `/api/admin/uploads` — File uploads

- /api/admin/uploads/cover-image

### `/api/admin/discover-tables` and `/api/admin/discover-and-register` — Table discovery

- /api/admin/discover-tables
- /api/admin/discover-and-register

### `/api/admin/users` — User management

- /api/admin/users/{user_id}/activity
- /api/admin/users/{user_id}/effective-access
- /api/admin/users/{user_id}/memberships
- /api/admin/users/{user_id}/memberships/{group_id}
- /api/admin/users/{user_id}/sessions
- /api/admin/users/{user_id}/sessions/download-all
- /api/admin/users/{user_id}/sessions/{session_file}/download

### `/api/admin/groups` — User groups

- /api/admin/groups
- /api/admin/groups/{group_id}
- /api/admin/groups/{group_id}/members
- /api/admin/groups/{group_id}/members/{user_id}

### `/api/admin/grants` — Resource grants

- /api/admin/grants
- /api/admin/grants/{grant_id}

### `/api/admin/access-overview` — Access overview

- /api/admin/access-overview

### `/api/admin/resource-types` — Resource type registry

- /api/admin/resource-types

### `/api/admin/mcp-sources` — MCP source management

- /api/admin/mcp-sources
- /api/admin/mcp-sources/{source_id}
- /api/admin/mcp-sources/{source_id}/classify
- /api/admin/mcp-sources/{source_id}/introspect
- /api/admin/mcp-sources/{source_id}/materialize
- /api/admin/mcp-sources/{source_id}/secret
- /api/admin/mcp-sources/{source_id}/test

### `/api/admin/mcp-tools` — MCP tool management

- /api/admin/mcp-tools
- /api/admin/mcp-tools/{tool_id}
- /api/admin/mcp-tools/{tool_id}/grants
- /api/admin/mcp-tools/{tool_id}/grants/{group_id}

### `/api/admin/memory-domains` — Knowledge domain management (admin)

- /api/admin/memory-domains
- /api/admin/memory-domains/{domain_id}
- /api/admin/memory-domains/{domain_id}/items
- /api/admin/memory-domains/{domain_id}/items/{item_id}
- /api/admin/memory-domains/{domain_id}/restore

### `/api/admin/memory-domain-suggestions` — Domain suggestion review (admin)

- /api/admin/memory-domain-suggestions
- /api/admin/memory-domain-suggestions/count-pending
- /api/admin/memory-domain-suggestions/{sid}/approve
- /api/admin/memory-domain-suggestions/{sid}/reject

### `/api/admin/authoring-suggestions` — Authoring studio suggestion review (admin)

Generic non-admin suggestion queue for the authoring studio (data-package / mcp /
marketplace / corporate-memory). Non-admins submit a proposed create payload from
the `/admin/studio/{domain}` builder; admins approve/reject (guarded state
transitions — turning an approved suggestion into the real resource is a deferred
follow-up that must re-validate through the domain endpoint, never replay).

- /api/studio/suggestions
- /api/studio/suggestions/mine
- /api/admin/authoring-suggestions
- /api/admin/authoring-suggestions/{sid}/approve
- /api/admin/authoring-suggestions/{sid}/reject

### `/api/studio/memory-mining` — Corporate-memory mining (privacy-gated)

Opt-in (per design spec §4.4): a user consents to having their session
transcripts mined into shared corporate memory; an admin triggers a run that
PII-scans candidates, tags provenance, and routes them through the
authoring-suggestions queue (never an admin-direct write).

- /api/studio/memory-mining/consent
- /api/admin/memory-mining/run

### `/api/admin/metrics` — Metric definitions (admin)

- /api/admin/metrics
- /api/admin/metrics/import
- /api/admin/metrics/{metric_id}

### `/api/admin/recipes` — Recipe management (admin)

- /api/admin/recipes
- /api/admin/recipes/{recipe_id}
- /api/admin/recipes/{recipe_id}/restore

### `/api/admin/observability` — Observability views

- /api/admin/observability/facets
- /api/admin/observability/kpis
- /api/admin/observability/views
- /api/admin/observability/views/{view_id}

### `/api/admin/adoption` — Adoption dashboard (admin)

- /api/admin/adoption/kpis
- /api/admin/adoption/series
- /api/admin/adoption/top-skills
- /api/admin/adoption/top-users
- /api/admin/adoption/users/{user_id}/kpis
- /api/admin/adoption/users/{user_id}/series
- /api/admin/adoption/users/{user_id}/top-skills
- /api/admin/adoption/users/{user_id}/top-tools

### `/api/admin/reports` — Marketplace usage digest (admin)

- /api/admin/reports/marketplace-digest

  One consolidated, report-shaped JSON payload for an external rendering
  pipeline (e.g. an n8n workflow). `?period=daily|weekly[&date=YYYY-MM-DD]`.
  Returns headline KPIs (with prior-period deltas), a per-day trend series,
  usage by source, top items, rising/falling movers, failures,
  installs/adoption, zero-usage curated plugins, and per-marketplace sync
  health. Admin-only; PAT-gated for headless callers.

### `/api/admin/telemetry` — Query telemetry

- /api/admin/telemetry/ask
- /api/admin/telemetry/export
- /api/admin/telemetry/facets
- /api/admin/telemetry/kpis
- /api/admin/telemetry/prune
- /api/admin/telemetry/query
- /api/admin/telemetry/reprocess
- /api/admin/telemetry/summary

### `/api/admin/sessions` — Session management (admin)

- /api/admin/sessions/facets
- /api/admin/sessions/kpis
- /api/admin/sessions/list
- /api/admin/sessions/{username}/{session_file}/download
- /api/admin/sessions/{username}/{session_file}/transcript

### `/api/admin/activity` — Activity feed

- /api/admin/activity
- /api/admin/activity/health
- /api/admin/activity/sync

### `/api/admin/news` — News / announcements

- /api/admin/news/current
- /api/admin/news/draft
- /api/admin/news/preview
- /api/admin/news/publish
- /api/admin/news/unpublish/{version}
- /api/admin/news/versions
- /api/admin/news/versions/{version}

### `/api/admin/initial-workspace` — Initial workspace template

Admin-only (web UI at `/admin/initial-workspace`; no analyst CLI/MCP analogue).
`/sync` is the manual "Sync now" action (errors loudly when no repo is
registered). `/sync-if-configured` is the nightly-scheduler wrapper: it always
returns 200, short-circuiting to `{"skipped": true, "reason": "not_configured"}`
when no IWT repo is registered, so the nightly job is a no-op on instances
without one. Cadence is configurable via `SCHEDULER_INITIAL_WORKSPACE_SCHEDULE`
or `instance.yaml` `initial_workspace.sync_schedule` (default `daily 03:30`).

- /api/admin/initial-workspace
- /api/admin/initial-workspace/sync
- /api/admin/initial-workspace/sync-if-configured

### `/api/admin/welcome-template` — Welcome message template

- /api/admin/welcome-template
- /api/admin/welcome-template/preview

### `/api/admin/workspace-prompt-template` — Workspace prompt template

- /api/admin/workspace-prompt-template
- /api/admin/workspace-prompt-template/preview

### `/api/admin/prompts` — Managed prompts (admin, #622)

Unified admin surface for the install + workspace prompts (`kind ∈
install|workspace`), each with an explicit Git ⇄ Editor `source_mode` toggle.
Editor mode keeps the DB override editable; Git mode binds the prompt to a file
in the Initial Workspace Template clone. Backs the `/admin/prompts` page.
`iwt-files` (read-only) lists the repo-root-relative bindable files in the
synced IWT clone for the bind-git file picker.

- /api/admin/prompts/iwt-files
- /api/admin/prompts/{kind}
- /api/admin/prompts/{kind}/source
- /api/admin/prompts/{kind}/bind-git
- /api/admin/prompts/{kind}/preview

### `/api/admin/bigquery` — BigQuery diagnostics

- /api/admin/bigquery/test-connection

### `/api/admin/keboola` — Keboola diagnostics

- /api/admin/keboola/test-connection

### `/api/admin/datasource-secrets` — Datasource credential management

Admin-only, write-only vault for datasource secrets (`KEBOOLA_STORAGE_TOKEN`, `BIGQUERY_SERVICE_ACCOUNT_JSON`). Values are encrypted via `AGNES_VAULT_KEY`; the GET endpoint returns presence/source status only, never the value.

- /api/admin/datasource-secrets
- /api/admin/datasource-secrets/{name}

`POST /api/admin/validate-gws-credentials` format-checks a Google Workspace OAuth `client_id` (no network call, no persistence) for the UI "Test" button; returns `{"valid": bool}`.

- /api/admin/validate-gws-credentials

### `/api/admin/slack-secrets` — Slack secret management

- /api/admin/slack-secrets
- /api/admin/slack-secrets/{name}

### `/api/admin/db` — Database state and migration

- /api/admin/db/cancel/{job_id}
- /api/admin/db/job/{job_id}
- /api/admin/db/migrate
- /api/admin/db/state

### `/api/admin/cache-warmup` — Cache warmup

- /api/admin/cache-warmup/run
- /api/admin/cache-warmup/status
- /api/admin/cache-warmup/stream

### `/api/admin/store` — Marketplace store submissions (admin)

- /api/admin/store/submissions
- /api/admin/store/submissions/{submission_id}
- /api/admin/store/submissions/{submission_id}/bundle.zip
- /api/admin/store/submissions/{submission_id}/override
- /api/admin/store/submissions/{submission_id}/rescan
- /api/admin/store/submissions/{submission_id}/retry

### `/api/admin/run-*` — Background job triggers

- /api/admin/run-blocked-purge
- /api/admin/run-bq-metadata-refresh
- /api/admin/run-corporate-memory
- /api/admin/run-jira-consistency-check
- /api/admin/run-jira-sla-poll
- /api/admin/run-knowledge-migration
- /api/admin/run-reap-stuck-reviews
- /api/admin/run-session-collector
- /api/admin/run-session-processor

### `/api/auth` — Authentication

- /api/auth/exchange-setup-token

### `/api/catalog` — Public catalog

- /api/catalog/metrics/{metric_path}
- /api/catalog/profile/{table_name}
- /api/catalog/profile/{table_name}/refresh
- /api/catalog/tables

### `/api/chat` — Chat sessions

- /api/chat/sessions
- /api/chat/sessions/{chat_id}
- /api/chat/sessions/{chat_id}/messages
- /api/chat/sessions/{chat_id}/ticket
- /api/chat/{session_id}/fork
- /api/chat/{session_id}/invite
- /api/chat/{session_id}/join-ticket
- /api/chat/{session_id}/leave
- /api/chat/{session_id}/messages

### `/api/collections` — File collections (bring-your-files)

- /api/collections
- /api/collections/search
- /api/collections/{collection_id}
- /api/collections/{collection_id}/files
- /api/collections/{collection_id}/files/{file_id}

### `/api/connectors` — Connector manifest

- /api/connectors/manifest
- /api/connectors/params

### `/api/data-packages` — Public data packages

- /api/data-packages/{slug}

### `/api/data` — Table data access

- /api/data/{table_id}/check-access
- /api/data/{table_id}/download

### `/api/debug` — Debug utilities

- /api/debug/throw

### `/api/health` — Health checks

- /api/health
- /api/health/detailed

### `/api/initial-workspace` — Initial workspace (user-facing)

- /api/initial-workspace
- /api/initial-workspace.zip
- /api/initial-workspace/applied

### `/api/marketplace` and `/api/marketplaces` — Marketplace

- /api/marketplace/categories
- /api/marketplace/curated/{marketplace_id}/{plugin_name}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/asset/{path}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/doc/{path}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/install
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/mirrored/{key}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}
- /api/marketplace/flea/{entity_id}/agent/{agent_name}
- /api/marketplace/flea/{entity_id}/detail
- /api/marketplace/flea/{entity_id}/skill/{skill_name}
- /api/marketplace/items
- /api/marketplaces
- /api/marketplaces/sync-all
- /api/marketplaces/{marketplace_id}
- /api/marketplaces/{marketplace_id}/plugins
- /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/system
- /api/marketplaces/{marketplace_id}/sync

### `/api/mcp` — MCP passthrough and per-table query

- /api/mcp/passthrough/tools
- /api/mcp/passthrough/tools/{tool_id}/call
- /api/mcp/query-table/{table_id}
- /api/mcp/sources/{source_id}/my-secret

### `/api/mcp-connect` — Headless MCP client setup

Issues a PAT for headless AI editors (Cursor, GitHub Copilot) that cannot complete the
interactive OAuth browser flow. The token is returned once and must be saved by the caller.

- /api/mcp-connect/token

### `/api/me` — Current user self-service

- /api/me/effective-access
- /api/me/home-stats
- /api/me/onboarded
- /api/me/stats/queries
- /api/me/stats/sessions
- /api/me/stats/sync
- /api/me/stats/tokens

### `/api/memory` — Corporate memory (knowledge base)

- /api/memory
- /api/memory-domain-suggestions
- /api/memory-domain-suggestions/mine
- /api/memory/admin/approve
- /api/memory/admin/audit
- /api/memory/admin/batch
- /api/memory/admin/bulk-update
- /api/memory/admin/contradictions
- /api/memory/admin/contradictions/{contradiction_id}/resolve
- /api/memory/admin/duplicate-candidates
- /api/memory/admin/duplicate-candidates/resolve
- /api/memory/admin/edit
- /api/memory/admin/mandate
- /api/memory/admin/pending
- /api/memory/admin/reject
- /api/memory/admin/revoke
- /api/memory/admin/{item_id}
- /api/memory/bundle
- /api/memory/domains
- /api/memory/domains/{slug}
- /api/memory/items/{item_id}/mark-mandatory
- /api/memory/items/{item_id}/mark-unmandatory
- /api/memory/my-contributions
- /api/memory/my-votes
- /api/memory/stats
- /api/memory/tree
- /api/memory/{item_id}/dismiss
- /api/memory/{item_id}/personal
- /api/memory/{item_id}/provenance
- /api/memory/{item_id}/vote

### `/api/metrics` — Metric catalog (user-facing)

- /api/metrics
- /api/metrics/{metric_id}

### `/api/my-stack` — User stack subscriptions

- /api/my-stack
- /api/my-stack/curated/{marketplace_id}/{plugin_name}

### `/api/query` — Data queries

- /api/query
- /api/query/hybrid

### `/api/recipes` — Recipes (user-facing)

- /api/recipes
- /api/recipes/{slug}

### `/api/scripts` — Scheduled scripts

- /api/scripts
- /api/scripts/deploy
- /api/scripts/run
- /api/scripts/run-due
- /api/scripts/{script_id}
- /api/scripts/{script_id}/run

### `/api/settings` — User settings

- /api/settings
- /api/settings/dataset

### `/api/slack` — Slack integration

- /api/slack/bind
- /api/slack/commands
- /api/slack/events
- /api/slack/interactivity

### `/api/stack` — Stack subscriptions

- /api/stack
- /api/stack/browse
- /api/stack/subscribe
- /api/stack/subscription/{resource_type}/{resource_id}

### `/api/store` — Marketplace flea-market store

- /api/store/bundle.zip
- /api/store/categories
- /api/store/entities
- /api/store/entities/dryrun
- /api/store/entities/preview
- /api/store/entities/{entity_id}
- /api/store/entities/{entity_id}/docs/{filename}
- /api/store/entities/{entity_id}/files
- /api/store/entities/{entity_id}/install
- /api/store/entities/{entity_id}/photo
- /api/store/entities/{entity_id}/rate
- /api/store/entities/{entity_id}/versions/{version_no}/restore
- /api/store/import-bundle
- /api/store/owners

### `/api/sync` — Data sync (CLI)

- /api/sync/manifest
- /api/sync/pull-confirm
- /api/sync/settings
- /api/sync/status
- /api/sync/table-subscriptions
- /api/sync/trigger

### `/api/telegram` — Telegram integration

- /api/telegram/status
- /api/telegram/unlink
- /api/telegram/verify

### `/api/upload` — Session and artifact upload

- /api/upload/artifacts
- /api/upload/local-md
- /api/upload/sessions

### `/api/user` — User setup tokens

- /api/user/cowork-bundle
- /api/user/setup-tokens
- /api/user/setup-tokens/{token_id}

### `/api/users` — User administration

- /api/users
- /api/users/{user_id}
- /api/users/{user_id}/activate
- /api/users/{user_id}/deactivate
- /api/users/{user_id}/reset-password
- /api/users/{user_id}/set-password

### `/api/v2` — v2 catalog and query APIs

- /api/v2/catalog
- /api/v2/marketplace/skills
- /api/v2/metadata-cache/refresh
- /api/v2/metadata-cache/status
- /api/v2/sample/{table_id}
- /api/v2/scan
- /api/v2/scan/estimate
- /api/v2/schema/{table_id}

### `/api/version` and `/api/welcome` — Instance info

- /api/version
- /api/welcome

### Config surface & marketplace plugin controls (admin)

- /api/admin/config-surface — read this instance's complete configurable surface: every config knob with its resolved value + source (env/yaml/default), the registered Initial Workspace Template, the registered marketplaces, and `infra_repo_url`. Also exposed as `agnes admin config-surface` and an MCP tool.
- /api/marketplaces/{marketplace_id}/plugins — admin-only: list a marketplace's plugins. Each row includes `admin_disabled`, which drives the `/admin/marketplaces` Details-modal switch and the DISABLED pill.
- /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/disable — admin-only: disable any registered plugin (not just built-ins) instance-wide. The plugin is then hidden from every served and admin surface for all callers — served feed, browse page, my-stack, synthetic served marketplace, the `/admin/access` grant UI, and v2 `/skills` — except the Details modal, where it can be re-enabled. Disabling also clears `is_system`.
- /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/enable — admin-only: re-enable a previously disabled plugin. Does **not** restore a previously-cleared `is_system`. The disabled state persists across restarts / sync re-seed until explicitly re-enabled.
