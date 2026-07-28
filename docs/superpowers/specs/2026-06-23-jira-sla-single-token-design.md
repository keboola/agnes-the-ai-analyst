# Jira single-token generic field refresh — design

**Date:** 2026-06-23
**Status:** Draft (awaiting review) — supersedes the earlier two-slot SLA variant.
**Scope:** `connectors/jira/` field-refresh path. Webhook setup, history backfill,
table registration, and RBAC are out of scope (operator runbook).

## 1. Summary (read first)

**Goal:** keep an operator-chosen set of Jira custom fields *fresh on the tickets*,
using the single primary token. The ticket already has these fields (just stale); a
periodic poll re-fetches them and overwrites them in place. This generalises — and
**replaces** — the earlier hard-coded two-slot SLA approach: SLA is not special, it is
just one or two entries in the configured field list.

Each operator lists the fields they care about in `JIRA_REFRESH_FIELDS`. The poll
(every N minutes) re-fetches those fields for open tickets with the primary token and
overwrites them on the ticket JSON; the transform emits **one column per configured
field on the `issues` table** (value as JSON text). No side table, no join — the value
lives on the ticket row, keyed by the existing `issue_key`.

This is vendor-agnostic: one customer refreshes SLA fields, another refreshes a
"lunch" field — same mechanism, no code change, no baked-in semantics.

## 2. Decisions (locked with the operator)

1. **Generic configured field list, no defaults** — `JIRA_REFRESH_FIELDS`. Unset → no
   refresh (graceful no-op).
2. **Single primary token** (`JIRA_EMAIL` / `JIRA_API_TOKEN`); domain URL by default,
   `api.atlassian.com` gateway when `JIRA_CLOUD_ID` is set (scoped token). Verified
   against Atlassian docs (carried over from the single-token work).
3. **Values overwritten on the ticket and surfaced as columns on `issues`**, value =
   JSON text. No side table, no join. Schema evolution is safe because the parquet
   views already use `union_by_name=true` (verified `extract_init.py`).
4. **SLA is not special.** Remove `JIRA_SLA_FIELD_FIRST_RESPONSE` /
   `JIRA_SLA_FIELD_RESOLUTION`, `sla_field_ids()`, the flat SLA columns
   (`first_response_*` / `time_to_resolution_*`) and `extract_sla_metrics`. To track
   SLA, list its field id in `JIRA_REFRESH_FIELDS` and `json_extract` the parts you
   want.
5. **Poll refreshes open tickets** (`status_category != 'Done'`); the existing status
   self-heal stays. (Drops the previous "has SLA data" filter.)
6. **Preflight verifies the configured fields** and can discover custom fields
   (`--list-fields`).

## 3. Contract

`JIRA_REFRESH_FIELDS`: comma-separated entries, each `field_id` or
`field_id:column_name`.
- `field_id` — any Jira field key (e.g. `customfield_10328`).
- `column_name` — optional; the column on `issues`. Validated against
  `^[A-Za-z_][A-Za-z0-9_]*$`; when omitted it defaults to the (sanitised) field id.
- Example: `customfield_10328:first_response,customfield_10161:resolution,customfield_10999:lunch`.

Stored value: the field's raw value, `json.dumps`-serialised into a `string` column.
A field absent on a ticket → that column is `null`.

## 4. Design

### 4.1 Field-list resolver (single source of truth)
`connectors/jira/service.py`:
```python
def refresh_fields() -> list[tuple[str, str]]:
    """[(field_id, column_name), ...] parsed from JIRA_REFRESH_FIELDS at call time.

    No defaults (instance-specific). Lazy so CLI scripts that load .env at runtime
    see the value. Invalid column names fall back to the sanitised field id; entries
    without a usable id are skipped.
    """
```
Resolved at call time (R1: never frozen at import — `.env` is loaded at runtime by the
CLI scripts).

### 4.2 Fetch + overlay (single token), `service.py`
- `fetch_refresh_fields(issue_key) -> dict | None`: resolve `refresh_fields()`; if none
  → `None`. Use `self.auth` (primary) + domain URL (gateway when `JIRA_CLOUD_ID` set),
  request exactly the configured ids, return the `fields` dict on 200.
- `save_issue` overlay: write each configured field from the fetch result onto
  `issue_data["fields"]`. (Replaces `fetch_sla_fields` + the SLA overlay.)

### 4.3 Transform, `transform.py`
- In `transform_issue`, after the fixed record: for each `(field_id, column)` in
  `refresh_fields()`, `record[column] = json.dumps(fields.get(field_id)) if present
  else None`. Extend `ISSUES_SCHEMA` with each `column: "string"` at runtime (R1).
- Remove the flat SLA columns from `ISSUES_SCHEMA` (`:104-109`), the SLA lines
  (`:401-406`), and `extract_sla_metrics` (`:292`).

### 4.4 Poll, `scripts/poll_sla.py`
- `find_open_issues(parquet_dir)` → keys where `status_category != 'Done'` (drop the
  SLA-column dependency). Refresh the configured fields + status self-heal per issue.
- `run()` reads config (primary token + base url) and the configured fields; if none
  configured, no-op.

### 4.5 Backfill, `scripts/backfill_sla.py`
- `load_config()` keeps single-token config; returns the configured `refresh_fields`.
- `needs_update` / `process_file` operate over the configured fields.

### 4.6 Preflight, `scripts/verify_sla_access.py`
- `--list-fields`: list custom fields (id + name + type) from `/rest/api/3/field`.
- default verify (`--issue KEY`): fetch the configured fields with the primary token on
  the available URL(s); report per-field present / permission-error / null; exit 0 when
  at least one URL returns a valid (non-error) value for a configured field. Never
  prints secrets.

## 5. Test strategy ("runnable, not guessed")

### 5.1 Live preflight (operator-run): `verify_sla_access --list-fields` then
`--issue <KEY>` against the filled `.env`.

### 5.2 Unit tests (mocked, deterministic):
- `refresh_fields()`: `id` only; `id:alias`; multiple; invalid alias → fallback to id;
  empty/unset → `[]`.
- `fetch_refresh_fields`: primary auth + domain URL + exactly the configured ids;
  gateway when `JIRA_CLOUD_ID` set; `None` when nothing configured or unconfigured.
- `save_issue` overlay writes the configured fields; skips when fetch returns `None`.
- `transform_issue`: one JSON column per configured field (alias honoured); absent
  field → null column; no flat SLA columns remain.
- `poll`: `find_open_issues` returns non-Done keys; refresh overlays configured fields.
- `verify_sla_access`: list-fields filter, per-field classification, exit codes, no
  secret in output.

### 5.3 Full suite: `.venv/bin/pytest tests/ -n auto -q` + connector tests.

## 6. Removed / migration

- **BREAKING (config):** `JIRA_SLA_EMAIL`, `JIRA_SLA_API_TOKEN`, `JIRA_SLA_FIELD_*` are
  gone; replaced by `JIRA_REFRESH_FIELDS`.
- **BREAKING (schema):** the flat SLA columns (`first_response_*`,
  `time_to_resolution_*`) no longer exist; SLA arrives as a JSON column via the field
  list. Downstream SLA queries switch to `json_extract(<column>, '$.ongoingCycle.elapsedTime.millis')` etc.
- Docs (`README.md`, `instance.yaml.example`, env template) + CHANGELOG updated.

## 7. Risks

- **R1 (import-time vs runtime env):** resolve the field list lazily.
- **R2 (column-name safety):** operator aliases become SQL/parquet column names —
  validate `^[A-Za-z_][A-Za-z0-9_]*$`; fall back to the sanitised field id otherwise.
- **R3 (refresh cost):** refreshing all open tickets every N minutes hits the Jira API;
  the poll keeps the existing per-issue pacing and open-only scope. A max-age bound can
  be added later.
- **R4 (schema evolution):** changing the field list changes the `issues` columns;
  `union_by_name=true` views tolerate added/removed columns (older months → null).

## 8. Out of scope / parked
- Webhook config, historical backfill run, table registration, RBAC (runbook).
- Typed extraction of specific field shapes (e.g. flat SLA numerics) — analysts use
  `json_extract` on the JSON column instead.
- The Keboola `_remote_attach` trailing-slash bug (separate `investigation.md`).
