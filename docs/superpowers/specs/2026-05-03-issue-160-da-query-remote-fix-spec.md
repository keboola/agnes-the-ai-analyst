# Spec — issue #160: `da query --remote` for `query_mode='remote'` BigQuery rows

**Status:** draft for review
**Target release:** 0.31.0 (minor — restored capability + new server-config knob)
**Author:** Zdenek
**Closes:** #160

---

## 1. Problem statement

The CLI rail `da query --remote "<sql>"` is documented (PR #154 + `cli/skills/agnes-data-querying.md` + root `CLAUDE.md`) as the way to run a one-shot server-side SQL probe against any registered table — including `query_mode='remote'` BigQuery-backed rows. The implementation does not match the docs:

- For `query_mode='remote'` BQ rows whose underlying entity is a `BASE TABLE`, the call works (master view exists, BQ extension's Storage Read API path serves it).
- For `query_mode='remote'` BQ rows whose underlying entity is `VIEW` or `MATERIALIZED_VIEW`, the call fails with `Catalog Error: Table with name <id> does not exist`. No master view is created on the server. The reporter's `unit_economics` (a curated finance VIEW) hits this path.

Root cause: `connectors/bigquery/extractor.py:225-258` checks `data_source.bigquery.legacy_wrap_views` (default `False`) and skips master-view creation for `VIEW`/`MATERIALIZED_VIEW` entities — directing analysts to `da fetch` instead. This was a v2-fetch-primitives design choice for cost control. PR #154's docs then promised the opposite without touching the implementation. Reporter is the first analyst to hit the gap.

Reporter's hypothesis ("the `--remote` flag is being ignored") is wrong — `cli/commands/query.py:58` does route differently. The diagnostic message Pavel posted is the correct DuckDB error from a missing master view.

## 2. Goals

1. `da query --remote "SELECT … FROM <id>"` resolves any `query_mode='remote'` BigQuery row, regardless of whether the upstream BQ entity is `BASE TABLE`, `VIEW`, or `MATERIALIZED_VIEW`.
2. Cost is bounded server-side: a query that would scan more than the operator-configured cap is rejected before execution with a clear, actionable error.
3. RBAC is preserved: callers can only query tables they have grants on; the cap-bypass workaround (`SELECT … FROM bq."ds"."tbl"` directly) is closed by the same enforcement layer.
4. Daily quota usage is tracked so `/api/query` BQ-touching calls share the budget with `/api/v2/scan`.
5. Operator can adjust the cap from the `/admin/server-config` UI without editing files or redeploying.
6. No legacy / opt-out flags. `legacy_wrap_views` is removed.

## 3. Out of scope (clarification, not deferred work)

- Hybrid `--register-bq` flow (`/api/query/hybrid`) algorithm is unchanged. The CLI render fix in §4.7 covers its error UX.
- `da fetch` (`/api/v2/scan`) flow algorithm is unchanged. Same render fix covers its UX.
- Per-row cost-cap overrides (different cap per registered table). One global cap is the design — not a deferral.

## 4. Design

### 4.1 Always create a master view for `query_mode='remote'` BQ rows

**File:** `connectors/bigquery/extractor.py`

Replace the entity-type branch at lines 225-258. Two SQL forms, one per BQ entity capability — no flag, no skip, no fallback:

```python
# Per BQ docs INFORMATION_SCHEMA.TABLES.table_type can return: BASE TABLE,
# VIEW, MATERIALIZED_VIEW, EXTERNAL, SNAPSHOT, CLONE. We support the three
# we have empirical evidence for in this codebase:
#   - BASE TABLE → catalog path (Storage Read API, predicate pushdown).
#     Proven by current code (legacy_wrap_views=False BASE TABLE branch).
#   - VIEW / MATERIALIZED_VIEW → bigquery_query() (jobs API, no pushdown).
#     Proven by current code (legacy_wrap_views=True branch).
# EXTERNAL / SNAPSHOT / CLONE behavior with the duckdb-bigquery extension
# is unverified at the time of writing this spec. Conservative: log+skip
# (same handling as a future unknown type). Operators who hit this can
# file a follow-up issue with a concrete repro; we add the supported set
# explicitly when verified.

if entity_type == "BASE TABLE":
    view_sql = (
        f'CREATE OR REPLACE VIEW "{table_name}" AS '
        f'SELECT * FROM bq."{dataset}"."{source_table}"'
    )
elif entity_type in ("VIEW", "MATERIALIZED_VIEW"):
    # `project_id` flows from the `_create_remote_attach_table` call upstream
    # (extractor.py:182), sourced from `data_source.bigquery.project` config.
    # Validate at this boundary too — `dataset` and `source_table` are
    # validated by lines 211-216 below, but project_id wasn't. Add it.
    if not validate_quoted_identifier(project_id, "BigQuery project_id"):
        raise RuntimeError(
            f"unsafe BQ project_id {project_id!r} — refusing to build view"
        )
    # The .replace("'", "''") is defense-in-depth; if the validator is ever
    # relaxed this still keeps the inline literal safe.
    bq_inner = f"SELECT * FROM `{project_id}.{dataset}.{source_table}`"
    bq_inner_escaped = bq_inner.replace("'", "''")
    view_sql = (
        f'CREATE OR REPLACE VIEW "{table_name}" AS '
        f"SELECT * FROM bigquery_query('{project_id}', '{bq_inner_escaped}')"
    )
else:
    # Unverified entity type for the duckdb-bigquery extension. Skip
    # master-view creation and the _meta INSERT. The registry row exists
    # and /api/v2/scan can still operate from it (it builds BQ SQL from
    # bucket+source_table, not from the master view).
    logger.warning(
        "Unverified BQ entity_type %r for %s.%s.%s — master view skipped. "
        "Use `da fetch` for this row, or file an issue with a repro to "
        "request native support.",
        entity_type, project_id, dataset, source_table,
    )
    continue  # next tc, no _meta INSERT (would point at non-existent view)
conn.execute(view_sql)
```

**No regression for current operators.** `BASE TABLE` and `VIEW`/`MATERIALIZED_VIEW` are the entity types we have proven paths for. Operators on default `legacy_wrap_views=False` today get NEW behavior for VIEW/MATERIALIZED_VIEW (master view appears where there was none — the headline fix). Operators who had `legacy_wrap_views=True` get IDENTICAL behavior to today for VIEW/MATERIALIZED_VIEW.

Both forms produce an inner view in extract.duckdb. The orchestrator (`src/orchestrator.py:340-355`) then creates a master analytics view from each — `da query --remote "SELECT … FROM <id>"` resolves uniformly, regardless of upstream entity type.

`_detect_table_type` is still called and still errors with `BQ entity {project}.{dataset}.{table} not found` when BQ returns no entity. New `else` branch above catches a future BQ entity type (e.g. `EXTERNAL` if anyone registers one) loud rather than silent.

**Why not let the extension dispatch internally for VIEWs:** The DuckDB BQ extension's catalog enumerates VIEWs (which is why Pavel's error message offered `bq.finance_unit_economics.unit_economics` as a fuzzy-match suggestion), but Storage Read API is a tables-only RPC — `SELECT * FROM bq."ds"."view_name"` raises `INVALID_ARGUMENT: Read sessions cannot be created on logical views`. Going via `bigquery_query()` is the only path that actually runs SELECTs on VIEW entities. The previous codebase already used this exact form behind `legacy_wrap_views=True`; it is a proven path.

### 4.2 Remove `legacy_wrap_views` config knob

The flag is the inverse of the new behavior. With "always wrap" as the only mode, the flag has no semantic meaning.

**Mandatory grep target list (verified per-file counts; merge gate: all but CHANGELOG must hit zero):**

| File | Count | Action |
|------|-------|--------|
| `connectors/bigquery/extractor.py` | 3 | rewrite the wrap-view branch per §4.1; remove `legacy_wrap_views` config read |
| `app/api/admin.py` | 3 | delete `_OPTIONAL_FIELDS` entry (line 229), delete the `_BQ_OPTIONAL_FIELD_DEFAULTS` entry (line 804), delete the comment block (line 798) |
| `config/instance.yaml.example` | 1 | delete the example line |
| `tests/test_bigquery_extractor.py` | 6 | rewrite tests per §5.1 (5 entries — BASE TABLE / VIEW / MAT_VIEW / unknown skip / overlay key ignored) |
| `tests/test_admin_server_config.py` | 10 | rewrite assertions: field NOT in payload; `max_bytes_per_remote_query` IS in payload; `billing_project` carries `placeholder_from` |
| `tests/test_admin_server_config_known_fields.py` | 3 | delete the `legacy_wrap_views` known-field assertion |
| `app/web/templates/admin_tables.html` | 2 | rewrite operator copy — master view now always exists for VIEW/MAT_VIEW |
| `src/orchestrator.py` | 1 | comment update — `_attach_and_create_views` skip branch comment at lines 330-355 references `legacy_wrap_views=False`. The skip BRANCH stays (still needed for any future `_meta`-without-inner-object case), but the comment must reflect that BQ rows now always have an inner view |
| `CHANGELOG.md` | 4 | leave historical entries; new `### Fixed` bullet cross-refs |

Total: 33 references across 9 files. Verified by per-file `grep -c` (rev3 review).

After all edits: `grep -rn 'legacy_wrap_views' connectors app src tests config cli` must return zero. `docs/` is **excluded** from the gate — `docs/superpowers/plans/2026-04-27-claude-fetch-primitives.md` (8 hits) and `docs/superpowers/specs/2026-04-27-claude-fetch-primitives-design.md` (1 hit) are historical planning artifacts that document the design decision to introduce the flag; rewriting history would be revisionist. CHANGELOG history retained on the same logic.

Verification command in CI / pre-commit: 

```
test "$(grep -rn 'legacy_wrap_views' connectors app src tests config cli | wc -l)" -eq 0
```

No DB migration. Operators who explicitly set `legacy_wrap_views: true` in their overlay get the new (equivalent) behavior automatically; the unrecognized key is ignored by the YAML loader. Operators who explicitly set `legacy_wrap_views: false` get the new behavior as a fix, not a regression.

### 4.3 Server-side cost guardrail on `/api/query`

**File:** `app/api/query.py`

Before executing the SQL, identify whether it touches any `query_mode='remote'` + `source_type='bigquery'` registered name. If yes, run a BQ dry-run to estimate scan bytes. If estimate exceeds cap, reject with 400.

#### 4.3.1 Detection of BQ-touching SQL — regex on raw SQL

Reviewer rev3 verified empirically against DuckDB 1.5.1 that the physical plan **inlines view bodies**. So a plan walker would see `SEQ_SCAN bq.<ds>.<tbl>` for every legitimate master-view query — making it impossible to distinguish "user typed `bq.*` directly" (must be RBAC-checked) from "user referenced a master view that resolves to `bq.*`" (already RBAC-checked via `get_accessible_tables`). The plan-walker design has been dropped.

**Use regex on raw user-submitted SQL.** Two narrow tasks, two narrow regexes:

```python
# 4.3.1a — direct bq.<dataset>.<source_table> references (3-part path).
# Tested against 16 cases including all quoting/casing variants, prefix
# false-positives (other_bq.x.y), middle-position bq (x.bq.y.z), 2-part
# rejection (bq.col), CTE bodies, multiple paths in one statement.
BQ_PATH = re.compile(
    r'(?<![\w.])bq\s*\.\s*("[^"]+"|\w+)\s*\.\s*("[^"]+"|\w+)(?=\W|$)',
    re.IGNORECASE,
)

# 4.3.1b — registered remote-mode names referenced as bare identifiers.
# Built per-request from `query_mode='remote' AND source_type='bigquery'`
# rows the caller can access. Word-boundary match avoids 'unit_economics_v2'
# matching 'unit_economics'.
def remote_name_pattern(names: set[str]) -> re.Pattern:
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf'\b({alt})\b', re.IGNORECASE)
```

Algorithm — runs once per `/api/query` request, only on `query_mode='remote' + source_type='bigquery'` registry rows:

1. **Cost guardrail (§4.3.2 inputs).** Compile `BQ_PATH` and `remote_name_pattern(accessible_remote_bq_names)`. Both regexes scan the raw user SQL. Each match contributes one `(bucket, source_table)` to the dry-run set — for `BQ_PATH` matches, group(1)/group(2) (stripped of quotes); for bare-name matches, the registry lookup gives bucket/source_table.

2. **RBAC patch (§4.3.4).** Same `BQ_PATH` regex. For each match:
   - Look up `(bucket=group1_unquoted, source_table=group2_unquoted)` in registry, filtered to `source_type='bigquery'`. (RBAC doesn't filter by `query_mode` — direct `bq.*` paths to materialized rows are also gated.)
   - No row → 403 `bq_path_not_registered`.
   - Row exists, caller lacks grant → 403 `bq_path_access_denied`.

**Known false-positive: bare-name match in string literal.** `WHERE comment = 'unit_economics is great'` regex-matches `unit_economics`, fires a wasted dry-run, query then executes normally (string literal doesn't actually touch BQ). Cost: one BQ dry-run RPC (~50ms). Acceptable.

**Known false-positive: bare name shadowed by CTE.** `WITH unit_economics AS (SELECT ...) SELECT FROM unit_economics` regex-matches, wasted dry-run, query executes (CTE shadows the registered name). Same cost, same acceptance.

**Known false-positive: CTE named `bq`.** `WITH bq AS (SELECT 1) SELECT * FROM bq.x.y` would have `BQ_PATH` regex match `bq.x.y` and fire RBAC check. The user is referencing a CTE column path inside a name-shadowed scope — but our RBAC check assumes `bq` always means the BigQuery catalog. Result: 403 on this rare query shape. Documented; recommend CTE rename if encountered.

**Known: 4-part path `bq."ds"."tbl"."col"`** (BigQuery struct field access). Regex matches the leading `bq."ds"."tbl"` part, treats it as a 3-part catalog reference. Functionally fine — the lookup uses the (ds, tbl) pair correctly; the trailing `."col"` is an attribute access on the matched table. No false-negative on RBAC; the dry-run also targets the correct underlying table.

**Known: BQ date-partition shard syntax `bq.ds."tbl$20231201"`.** BigQuery allows `$` in table names for date-partition shards. `$` is `\W`, so the lookahead `(?=\W|$)` matches BEFORE the `$`, capturing `tbl` (without the shard). The dry-run then runs against `bq."ds"."tbl"` (the parent partitioned table) instead of the specific shard — over-estimates scan bytes, never under-estimates. RBAC effect: caller is gated on the parent table's registration, which is the conservative choice. Documented; if shard-precision is ever needed, regex can be widened.

**Two registry rows share a `name`** (e.g. `bigquery` source + `keboola` source both named `users`) → orchestrator's `view_ownership` ensures only the canonical owner's master view exists. The bare-name match against `accessible_remote_bq_names` only includes BQ-source rows; the keboola twin doesn't appear in this set, no collision at dry-run time.

**Block `bigquery_query()` direct calls.** Add `"bigquery_query"` to the SQL blocklist at `app/api/query.py:42-65`. The function-call backdoor is a pre-existing hole (verified empirically — current blocklist passes `SELECT * FROM bigquery_query('proj', 'SELECT * FROM ds.tbl')`). Wrap views created at extract time use `bigquery_query()` internally, but those run via DuckDB's view resolution — the user-submitted SQL never contains the function name, so the blocklist doesn't break them.

**Reuse the existing forbidden-table loop.** `app/api/query.py:80-89` already iterates analytics master views and runs `re.search(r'\b' + re.escape(table.lower()) + r'\b', sql_lower)` for each. The new bare-name detection (§4.3.1 step 1) duplicates this exact pattern. **Implementation should weave into the existing loop:** for each master view name that matches AND whose registry row is `query_mode='remote' + source_type='bigquery'`, also collect `(bucket, source_table)` for the dry-run set. One regex pass, two side effects (RBAC + cost). The `BQ_PATH` regex for direct `bq.X.Y` paths runs as a separate pass — different syntax, can't share the loop.

#### 4.3.2 Dry-run

For each matching row:
1. Build the BQ dry-run SQL: `SELECT * FROM \`{billing_project}.{bucket}.{source_table}\`` — same format as `_build_bq_sql` in `app/api/v2_scan.py:110`. Reuse `_bq_dry_run_bytes` from `v2_scan.py`.
2. Sum `scan_bytes` across all matched rows.

This **over-estimates** by counting full table scan per referenced name, ignoring DuckDB-side pushdown. That is intentional: the cap is the safety ceiling, not an exact predictor. Analyst who wants accurate estimate uses `da fetch <id> --estimate` (already exists, structured).

#### 4.3.3 Enforcement

QuotaTracker API verified at `app/api/v2_quota.py:49-120`:
- `with q.acquire(user):` — context manager; raises `QuotaExceededError(KIND_CONCURRENT, ...)` on entry if at concurrent limit, decrements on exit.
- `q.check_daily_budget(user)` — raises `QuotaExceededError(KIND_DAILY_BYTES, ...)` if at-or-over daily byte cap.
- `q.record_bytes(user, n)` — never raises; commits cumulative usage post-flight.

**Quota tracker import path.** `_build_quota_tracker()` and the module-level `_quota_singleton` currently live at `app/api/v2_scan.py:254-268`. 7 test sites in `tests/test_v2_scan.py` (lines 77, 118, 143, 160, 186, 208, 250) call `v2_scan._build_quota_tracker()` directly.

Two options — pick one in implementation:

**(a) Move-and-shim (preferred).** Move the function and singleton to `app/api/v2_quota.py`. Add a thin re-export at `app/api/v2_scan.py` — **but only the function, not the singleton variable**:

```python
# app/api/v2_scan.py — at module top
from app.api.v2_quota import _build_quota_tracker  # re-export
# Do NOT re-export _quota_singleton: `from X import var` copies the
# binding at import time. Once v2_quota._build_quota_tracker() mutates
# v2_quota._quota_singleton, a re-exported v2_scan._quota_singleton
# would still hold the initial None — silent state divergence.
```

The 7 test sites in `tests/test_v2_scan.py` (lines 77, 118, 143, 160, 186, 208, 250) call `v2_scan._build_quota_tracker()` only — they don't touch `_quota_singleton` directly (verified via grep). Re-export of the function alone preserves their behavior. `/api/query` imports from `v2_quota.py` directly. Clean dep direction. ~5 LOC. Test files unchanged.

**(b) Direct import from v2_scan.** `/api/query` imports `from app.api.v2_scan import _build_quota_tracker`. Inverted dep direction (api/query → api/v2_scan), but zero refactor. Acceptable since both modules are siblings under `app.api`, not layered.

Spec recommends (a) for cleanliness; (b) is a fallback if (a) breaks anything unexpected.

Pre-flight order — only fires when there's something to dry-run (regex matches in §4.3.1 produced a non-empty set):

```python
quota = _build_quota_tracker()
quota.check_daily_budget(user)            # raises 429 if over daily cap
with quota.acquire(user):                 # raises 429 if at concurrent limit
    total_scan_bytes = sum(
        _bq_dry_run_bytes(bq, build_dry_run_sql(row))
        for row in matched_rows
    )
    if total_scan_bytes > cap:
        raise HTTPException(400, detail={
            "reason": "remote_scan_too_large",
            "scan_bytes": total_scan_bytes,
            "limit_bytes": cap,
            "tables": [r["name"] for r in matched_rows],
            "suggestion": (
                "Use `da fetch <id> --select <cols> --where <predicate> "
                "--estimate` to materialize a filtered subset, then query "
                "the snapshot locally."
            ),
        })
    result = analytics.execute(user_sql).fetchmany(limit + 1)
    quota.record_bytes(user, total_scan_bytes)  # post-flight
```

The `with quota.acquire(user)` block guarantees the concurrent slot is released even if dry-run, execute, or `record_bytes` raises. `QuotaExceededError` is mapped to HTTP 429 by the existing v2 handler shape — reuse the same translation in `/api/query` so CLI render in §4.7 prints the same structured shape (`{kind, current, limit, retry_after_seconds}`).

If matched rows is empty (regular non-BQ query): skip the entire block. Zero new latency for non-BQ-touching queries.

The cap is read from `api.query.bq_max_scan_bytes` (server-config UI; see 4.4). Default `5_368_709_120` (5 GiB).

#### 4.3.4 RBAC patch — close direct `bq.*` bypass

Pre-existing hole: the forbidden-table check at `app/api/query.py:80-89` only blocks names that match a master view. `SELECT … FROM bq."ds"."tbl"` (direct catalog path) doesn't match any master view, so it bypasses the existing check entirely.

Use the `BQ_PATH` regex from §4.3.1 (verified 16/16). For each match, before execute:

```python
from app.auth.access import is_user_admin

is_admin = is_user_admin(user["id"], conn)
accessible = get_accessible_tables(user, conn)  # None for admins, list[str] otherwise

for m in BQ_PATH.finditer(user_sql):
    bucket = m.group(1).strip('"')
    source_table = m.group(2).strip('"')
    row = registry.find_by_bq_path(bucket, source_table)  # case-insensitive
    if row is None:
        raise HTTPException(403, detail={
            "reason": "bq_path_not_registered",
            "path": f'bq."{bucket}"."{source_table}"',
            "hint": (
                "Direct bq.* references must point to a registered table. "
                "Register via `da admin register-table` or use the "
                "registered name from `da catalog`."
            ),
        })
    # Admin short-circuit: accessible is None for admins (sees all). Only
    # apply per-name grant check for non-admins.
    if not is_admin and (accessible is None or row["name"] not in accessible):
        # accessible=None for non-admin would be a bug; fail closed.
        raise HTTPException(403, detail={
            "reason": "bq_path_access_denied",
            "path": f'bq."{bucket}"."{source_table}"',
            "registered_as": row["name"],
        })
```

**Edge case — `bigquery_query()` function-call backdoor.** Closed by adding `"bigquery_query"` to the SQL blocklist (§4.3.1 last paragraph). The blocklist runs before the regex check, so a user who tries to bypass the `bq.*` regex via `SELECT * FROM bigquery_query('proj', 'SELECT * FROM ds.tbl')` gets a 400 from the existing blocklist path.

**Edge case — admin user.** Admin gets `accessible_table_names = None` from `get_accessible_tables`. The bq-path registration check still fires (the path must point to a registered row); the per-name grant check is short-circuited by the `user_id not in admin_ids` guard. Admin can register first, then query — matches v2-fetch design (registry-gated).

**Edge case — bare `bq.col` 2-part reference** (`SELECT bq.col FROM tbl`). Regex requires 3-part — verified empirically does not match.

**Edge case — string literal containing `bq.x.y`** (`WHERE c = 'bq.foo.bar'`). Regex matches; check fires; if registry doesn't have the row, returns 403. Worst case: a comment or string accidentally shaped like `bq.X.Y` triggers a 403 instead of executing. Documented; low rate; user can rephrase. Strict mode is correct here — we'd rather false-positive-deny than false-positive-allow on a security boundary.

**Method needed: `TableRegistryRepository.find_by_bq_path(bucket, source_table)`.** Returns the row where `source_type='bigquery' AND lower(bucket)=lower(?) AND lower(source_table)=lower(?)`.

**Ambiguity handling.** No unique constraint exists on `(source_type, bucket, source_table)` in `src/db.py` — multiple registry rows can in principle point at the same BQ table (e.g. an admin registers it twice with different `name` values). Resolution policy:
- Query: `SELECT * FROM table_registry WHERE source_type='bigquery' AND bucket IS NOT NULL AND source_table IS NOT NULL AND lower(bucket)=lower(?) AND lower(source_table)=lower(?) ORDER BY registered_at ASC`. The NULL guards defend against rows where bucket or source_table happen to be NULL (some legacy local rows have empty bucket); without them `lower(NULL)=lower('foo')` returns NULL (not false) and the row is excluded by SQL three-valued logic — but the explicit guard makes intent obvious to reviewers.
- 0 rows → return `None` (handled by 403 `bq_path_not_registered` above).
- 1 row → return it.
- 2+ rows → return the **oldest by `registered_at`** (deterministic, prefers the longest-lived registration). Log a warning so an admin can clean up the duplicate.

~15 LOC addition. Test fixture: insert two rows with same path, assert oldest wins.

**Why not add a UNIQUE constraint?** It would require a schema migration that could fail on existing instances with legitimate duplicates. Out of scope for this issue.

#### 4.3.5 Multiple statements / aliases

Existing blocklist already rejects `;` (multi-statement). Aliasing inside a single statement (`SELECT * FROM unit_economics ue`) is fine — the regex matches the bare name once.

### 4.4 New server-config field: `data_source.bigquery.max_bytes_per_remote_query`

Wait — the cost guardrail is for `/api/query`, not `materialize`. Naming it under `data_source.bigquery` keeps it next to `max_bytes_per_materialize` (the precedent).

**File:** `app/api/admin.py:_OPTIONAL_FIELDS["data_source"]["bigquery"]["fields"]`

Add:
```python
"max_bytes_per_remote_query": {
    "kind": "int",
    "default": 5368709120,
    "hint": (
        "Cost guardrail for `da query --remote` against query_mode='remote' "
        "BigQuery rows. Estimated scan bytes from a BQ dry-run. Exceeds → 400 "
        "with suggestion to use `da fetch`. 0 disables. Default 5368709120 = 5 GiB."
    ),
},
```

Remove `legacy_wrap_views` entry from same dict (per 4.2).

**Reader:** `app/api/query.py` reads via `from app.instance_config import get_value` → `get_value("data_source", "bigquery", "max_bytes_per_remote_query", default=5368709120)`.

The `/admin/server-config` template (`app/web/templates/admin_server_config.html`) auto-renders the new field from `_OPTIONAL_FIELDS` — no template change needed.

### 4.5 Documentation

Touch four files. Rev3 review caught the missing `agnes-table-registration.md`.

1. **`config/claude_md_template.txt`** (PR #154's analyst CLAUDE.md template). Update the "three query patterns" table:
   - `da query --remote`: keep as one-shot path. Add: "Server-side cost guardrail caps scans at 5 GiB by default (configurable). Exceeded queries are rejected with a `da fetch` suggestion."
2. **`cli/skills/agnes-data-querying.md`**: same caveat in the rails table at line 33 + decision matrix at line 104. Add an explicit note: "Cost guardrail kicks in via BQ dry-run; if the cap is hit, the response names the bytes and suggests `da fetch`."
3. **`cli/skills/agnes-table-registration.md:121`** — currently shows `da query --remote "SELECT … FROM \`<project>.<dataset>.<table>\`"` as a one-off pattern. Under §4.3.4 RBAC patch this exact form is now blocked unless the table is registered. Update the example to the registered-name form: `da query --remote "SELECT … FROM <registered_id>"`. Add note: "raw `bq.<dataset>.<table>` paths require a registered row; admins can register first or use the bare id."
4. **`CLAUDE.md`** (root, "Querying Agnes data — agent rails" section). Two specific edits:

   **(a)** Under "Choose the right tool" / `remote`: add one bullet explaining the new guardrail — "Server caps the dry-run scan at 5 GiB by default. Cheap aggregates on BASE TABLE rows (Storage Read API pushes WHERE down) typically fit; aggregates on VIEW/MATERIALIZED_VIEW rows estimate as full-scan and may be rejected — pivot to `da fetch <id> --where '<predicate>' --estimate`."

   **(b)** "When NOT to use `da fetch`" decision matrix at the bottom of the section currently lists `SELECT COUNT(*) FROM web_sessions_example` as a "cheap" `--remote` candidate. Append a parenthetical: `"(cheap for BASE TABLE rows; for VIEW/MATERIALIZED_VIEW the dry-run estimates a full scan, so the guardrail rejects unless within the 5 GiB cap. Pivot to da fetch.)"`.

   Pre-deploy verification: §5.3 manual scenario should run `SELECT COUNT(*) FROM <a-VIEW-row>` and check if the dry-run actually estimates full-scan or near-zero (BQ COUNT optimization on metadata) — if BQ returns ~0 bytes for COUNT-on-VIEW, edit (b) is overcautious and the matrix can stay simpler.

Skip `cli/skills/corporate-memory.md` and `cli/skills/security.md` — those use `--remote` against `system.knowledge_items` / `system.audit_log` which are admin-only views, not BQ rows. No change.

**Verification before merge:** `grep -rn 'bq\."\|--remote\|bigquery_query' cli/skills/ docs/ config/` and visually inspect each match still describes a working flow.

### 4.6 Insertion point in `/api/query` handler

Pin the exact placement of new logic in `app/api/query.py:execute_query` (lines 30-130 today):

```
Line 39  : sql_lower = request.sql.strip().lower()
Line 64  : blocklist check  ← ADD "bigquery_query" entry here
Line 70  : SELECT/WITH validator (rejects empty/whitespace SQL)
Line 74  : get_accessible_tables(user, conn) → allowed
Line 76  : open analytics
Line 80-89: master-view enumeration + forbidden-table re.search
            ← WEAVE: when a matched name is query_mode='remote' AND
              source_type='bigquery', also append (bucket, source_table)
              to dry_run_set
Line 90  : ↓ NEW BLOCK INSERTED HERE ↓

            # 4.3.4 — RBAC patch: BQ_PATH regex on raw SQL, registry-gated
            for m in BQ_PATH.finditer(request.sql): ...

            # 4.3.3 — cost guardrail + quota
            if dry_run_set:
                quota.check_daily_budget(user_id)
                with quota.acquire(user_id):
                    total_bytes = sum(_bq_dry_run_bytes(...) for ...)
                    if total_bytes > cap: raise 400
                    result = analytics.execute(request.sql).fetchmany(...)
                    quota.record_bytes(user_id, total_bytes)
            else:
                result = analytics.execute(request.sql).fetchmany(...)

Line 92  : (existing) result fetching → REPLACED by the conditional above
Line 109+: (existing) materialized-hint exception path → unchanged
```

**Empty/whitespace SQL** is rejected by line 70 BEFORE any new code runs (`^(select|with)\s` regex requires content). The new pre-flight only fires after that gate. Verified: empty input passes blocklist (no keywords), fails SELECT/WITH check at line 70, returns 400.

### 4.7 CLI: structured BQ error rendering (covers reporter's secondary nit)

**The reporter's `USER_PROJECT_DENIED` complaint is a CLI render bug, not a config bug.** The server side already maps BQ Forbidden into a typed `BqAccessError(kind='cross_project_forbidden')` with a `hint` field (`connectors/bigquery/access.py:88-107`). The hint reaches the FastAPI response as `detail.hint`. The CLI side has **multiple non-shared rendering paths**, all of which truncate or hide the structured shape.

#### 4.7.1 Inventory of CLI error-rendering paths

**Scope:** only commands that surface BigQuery typed errors today or after this PR. Other CLI commands (`auth`, `metrics`, `tokens`, `setup`) have their own error-formatting logic that doesn't touch BQ paths and is out of scope.

| Path | File | Current rendering | Fix |
|------|------|-------------------|-----|
| `da query --remote` | `cli/commands/query.py:90-92` (`_query_remote`) | `resp.json().get('detail', resp.text)` — flattens dict → `str(dict)` | Replace with shared renderer |
| `da query --register-bq` | `cli/commands/query.py:139-145` (`_query_hybrid`) | `RemoteQueryError` with own `__str__` | Extract structured detail before raising |
| `da fetch`, `da schema`, `da explore` (v2 endpoints) | `cli/v2_client.py:21` (`V2ClientError`) | `f"HTTP {status}: {message[:200]}"` | Replace with shared renderer |

**Sync drift caveat (renderer in two languages):** Python `cli/error_render.py` renders for CLI; JS `renderBqError(detail)` in `admin_server_config.html` renders for the Test Connection result inline. They format the SAME structured shape (`{kind, hint, billing_project, data_project, ...}`). Keeping them in sync is a manual responsibility — flag this in the PR description so future maintainers know to update both. Tests assert each formatter independently against fixture inputs.

#### 4.7.2 Shared renderer

**New file:** `cli/error_render.py` — single source of truth for "given a parsed JSON response body, format it for the user".

```python
def render_error(status_code: int, body: dict | str) -> str:
    """Format an HTTP error body for stderr.

    Recognized shapes (pretty-printed when matched):
    - {'detail': {'kind': str, 'hint': str, ...}}                  — typed BqAccessError
    - {'detail': {'reason': str, 'suggestion': str, ...}}          — guardrail rejections
    - {'detail': {'reason': str, 'kind': str, ...}}                — RBAC denies
    Anything else: fallback to truncated str(body)[:500].
    """
```

Format:
```
Error: <kind-or-reason> (HTTP <code>)
  <key1>: <value1>
  <key2>: <value2>
  ...
  <hint-or-suggestion-key>: <wrapped to 80 cols, indented under the key>
```

#### 4.7.3 Wiring all three paths

**`cli/v2_client.py`:**
- Stop pre-truncating in `V2ClientError.__init__`: store `body` as-is (dict or string). Drop `message` field (or keep for back-compat, populate from renderer at access).
- `__str__` calls `render_error(self.status_code, self.body)`.
- Wrappers `api_post_json`/`api_post_arrow` populate `body` from `resp.json()` when content-type is JSON, fall back to `resp.text`.

**`cli/commands/query.py:_query_remote`:**
- Before: `typer.echo(f"Query failed: {resp.json().get('detail', resp.text)}", err=True)`.
- After: parse JSON if possible, call `render_error(resp.status_code, parsed)`, echo to stderr, exit 1.

**`cli/commands/query.py:_query_hybrid`:**
- `RemoteQueryError` at `src/remote_query.py:99-116` already carries `details: Optional[Dict[str, Any]]` — rev3 review verified, my earlier claim "add this field" was wrong. The BqAccessError-wrapping path at lines 425/435 already populates it.
- **Real gap:** 13 raise sites total in `src/remote_query.py`. Lines 422 and 432 already populate `details` (the BqAccessError-wrap path — done). The remaining **11 sites** at lines 134, 142, 167, 173, 259, 264, 282, 289, 313, 322, 375 raise without `details`. Audit each:
  - For sites that wrap an external error (BadRequest from BQ etc.): pass through that error's relevant fields as `details={"upstream": str(e), ...}`.
  - For sites that raise on local validation (e.g. "alias already registered"): `details=None` is correct; the `error_type` + message suffice.
  - Output of audit: a 11-row table in the PR description listing each raise site and the disposition.
- `_query_hybrid` catches `RemoteQueryError`, calls `render_error(status_code=400, body={'detail': {'kind': e.error_type, 'message': str(e), **(e.details or {})}})`. Status code 400 is a client-error placeholder for local engine errors; the renderer's signature already accepts arbitrary status codes for label-only display.

#### 4.7.4 Server-side: ensure all BQ paths surface typed `detail`

Audit (verify before merge):
- `/api/query/hybrid:36, 40` raises `HTTPException(400, detail=f"BQ '{alias}': {e.error_type}: {e}")` — **flattens to string**, loses structured fields. Change to `detail={'kind': e.error_type, 'message': str(e), **(e.details or {})}`.
- `/api/v2/scan`, `/scan/estimate`, `/sample`, `/schema` already raise typed `BqAccessError` → translated to FastAPI HTTPException with dict detail by their existing handlers (verify via `grep -n translate_bq_error app/api/v2_*.py`).
- `/api/query` raw DuckDB exceptions go through `_materialized_hint_for_query_error` (existing materialized hint) and the new remote-mode hint (4.3); both must use dict `detail`, not string. Ensure consistency.

#### 4.7.5 Server-config placeholder for `billing_project`

**Two-part change.** `placeholder_from` is a NEW key — the template doesn't understand it today. Both halves required:

**Part 1 — server side, `app/api/admin.py`.** In `_OPTIONAL_FIELDS["data_source"]["bigquery"]["fields"]["billing_project"]` add `"placeholder_from": ["data_source", "bigquery", "project"]`.

**Part 2 — client side, `app/web/templates/admin_server_config.html`.** The renderer at `renderLeafInput` (lines 281-320) returns HTML strings, NOT DOM elements — so the placeholder must be injected into the HTML at construction time, before the string is set as innerHTML. The module-level `original` variable (declared line 246, populated line 1071 from the GET payload) is the loaded config dict — it's in closure scope from `renderLeafInput`.

**Diff inside the default text branch at line 315-319 of `admin_server_config.html`:**

```javascript
  // Default: text. Use the registry's default when unset, else the value.
  const v = isUnset
    ? (opts && opts.spec && opts.spec.default != null ? String(opts.spec.default) : "")
    : (value == null ? "" : value);
+ // placeholder_from: walk the loaded config dict and show the resolved
+ // fallback as placeholder text when the field has no value of its own.
+ // Used by data_source.bigquery.billing_project to surface its fallback
+ // to data_source.bigquery.project per access.py:339-340.
+ let placeholderAttr = "";
+ if (isUnset && opts && opts.spec && Array.isArray(opts.spec.placeholder_from)) {
+   const resolved = opts.spec.placeholder_from.reduce(
+     (cur, k) => (cur && typeof cur === "object" ? cur[k] : undefined),
+     original,
+   );
+   if (resolved !== undefined && resolved !== null && resolved !== "") {
+     placeholderAttr = ` placeholder="(defaults to ${escHtml(String(resolved))})"`;
+   }
+ }
- return `<input id="${fieldId}" type="text" data-section="${section}" data-key="${escHtml(dottedKey)}" data-path="${dataPath}" value="${escHtml(v)}">`;
+ return `<input id="${fieldId}" type="text" data-section="${section}" data-key="${escHtml(dottedKey)}" data-path="${dataPath}" value="${escHtml(v)}"${placeholderAttr}>`;
```

`opts.spec` is the field-spec dict from `_OPTIONAL_FIELDS` (the renderer already passes `spec` through `opts`, see how `opts.spec.options` and `opts.spec.default` are read). Adding `opts.spec.placeholder_from` reuses the same plumbing.

If `placeholder_from` walks to a missing key (operator hasn't set `data_source.bigquery.project` either), `resolved` is `undefined` — `placeholderAttr` stays empty, no `placeholder=` rendered. Correct fallback: nothing to suggest.

`isUnset` already gates the branch — once user types something, the field becomes "set" and the placeholder doesn't render.

**Test:** §5.1 `tests/test_admin_server_config_placeholder.py` covers the server-side payload (asserts `placeholder_from` lands in the GET response). Manual scenario §5.3 item 6 covers the visual rendering — admin opens server-config with `data_source.bigquery.project` set but `billing_project` empty, sees `(defaults to <project>)` in the input.

### 4.8 Coverage check: every BQ error path returns the typed shape

Audit point so the §4.7 renderer never silently falls through:

- `/api/v2/scan`, `/scan/estimate`, `/sample`, `/schema` already use `translate_bq_error` (PR #138). Verify with `grep -n translate_bq_error app/api/v2_*.py`.
- `/api/query/hybrid` uses `RemoteQueryEngine`, which lazily resolves `BqAccess` and re-raises `BqAccessError` (`src/remote_query.py:418-432`). Already typed.
- The new `/api/query` cost-guardrail dry-run (4.3.2) calls `_bq_dry_run_bytes` from `v2_scan.py`, which already wraps with `translate_bq_error`. Already typed.
- The new wrap-view path at query time (4.1) — when DuckDB BQ extension hits a BQ error during `SELECT * FROM bq.…`, the error surfaces as a DuckDB exception, NOT a Google API exception. `translate_bq_error` has a string-match fallback for HTTP 403/400 patterns (`access.py:117-148`). Verify by integration test: revoke SA grant → expect `cross_project_forbidden` (typed), not raw 500.

If a path leaks raw 500, fix it at the source (wrap in `translate_bq_error`) — don't paper over in CLI render.

### 4.9 BQ connection test button on `/admin/server-config`

Closes the loop on Pavel's `USER_PROJECT_DENIED` symptom: admin saves `data_source.bigquery` config, immediately verifies it works, no analyst ever needs to discover the misconfig via a failed query.

**Decision: explicit "Test connection" button, not a probe-on-save.** Probe-on-save adds 1–3s to every save and blocks legitimate saves on transient BQ hiccups. A button is admin-controlled, fast to implement, and matches the pattern used by other config UIs (Slack/Stripe webhooks, etc.).

**New endpoint:** `POST /api/admin/bigquery/test-connection`

- Auth: `Depends(require_admin)`.
- Body: empty (uses currently-saved config from instance.yaml + overlay; no body needed since admin would have just clicked Save).
- Behavior:
  1. Resolve `BqAccess` via existing `get_bq_access()`. If config is incomplete (e.g. `data_source.bigquery.project` empty), return 400 with the existing `not_configured` typed error.
  2. Run a minimal BQ query: `SELECT 1 AS ok` via `bq.client().query("SELECT 1 AS ok")`, then `.result(timeout=10)`. 10s polling timeout.
  3. On success: 200 with `{ok: true, billing_project: "<resolved>", data_project: "<resolved>", elapsed_ms: <n>}`.
  4. On `BqAccessError`: translate via existing path, return 502 with the typed `detail` shape (renderer in §4.7 picks it up).
  5. On `concurrent.futures.TimeoutError` (10s elapsed): best-effort `client.cancel_job(query_job.job_id, location=query_job.location)` (swallow exceptions from cancel — the test endpoint has already failed, surfacing the cancel failure on top would just confuse). Return 504 with `{kind: "timeout", elapsed_ms: 10000, hint: "BigQuery did not respond in 10s. Check network and SA permissions. The job was best-effort cancelled."}`.

**Caveat:** `result(timeout=...)` is a polling-loop timeout client-side; the BQ job continues running until cancelled or completed. For `SELECT 1` the cost is negligible. Documented in CHANGELOG.

**File:** new `app/api/admin_bigquery_test.py` (~50 LOC), registered in `app/main.py`.

**UI side:** `app/web/templates/admin_server_config.html` — add a button next to the `data_source.bigquery` section's Save button:

```html
<button class="btn-secondary" data-action="test-bigquery">Test connection</button>
<span class="bq-test-result" hidden></span>
```

JS handler: POST to `/api/admin/bigquery/test-connection`, populate `.bq-test-result` with green check + elapsed_ms on success, red X + the typed detail on failure (reuse the renderer logic from §4.7 in JS — extract a small `renderBqError(detail)` helper that handles `error_kind`, `hint`, `billing_project`, `data_project`).

The button is only enabled when at least `data_source.bigquery.project` is saved (otherwise the test would just return `not_configured`).

**Why no dashboard-level health badge:** the button gives admins explicit verification on demand. Continuous health-check would mean a periodic background BQ call (cost, complexity, alerts that page someone). Out of scope.

### 4.10 CHANGELOG

Under `## [Unreleased]`:

```markdown
### Added
- `data_source.bigquery.max_bytes_per_remote_query` server-config knob
  (default 5 GiB). Caps the BigQuery scan that `da query --remote` will
  issue against `query_mode='remote'` BQ rows. Exceeds → 400 with a
  `da fetch` suggestion. Configurable via /admin/server-config.

### Fixed
- `da query --remote` now resolves `query_mode='remote'` BigQuery rows
  whose underlying entity is a `VIEW` or `MATERIALIZED_VIEW` (issue #160).
  The BQ extractor creates a master view via Storage Read API path
  (`bq."<dataset>"."<source_table>"`) for `BASE TABLE`, and via jobs API
  (`bigquery_query()`) for `VIEW` / `MATERIALIZED_VIEW`. Other BQ entity
  types (`EXTERNAL`, `SNAPSHOT`, `CLONE`) are not (yet) supported — the
  extractor logs a warning and skips master-view creation; analysts use
  `da fetch` for those. Cost is bounded by the new server-side guardrail
  (see Added).
- **BREAKING (config-only): `data_source.bigquery.legacy_wrap_views`
  removed.** Keys in operator overlays are silently ignored — no action
  required. Replaces the prior CHANGELOG entry that introduced the flag
  (kept in history for context).
- `/api/query` now blocks direct `bq."<dataset>"."<table>"` references
  for callers without a registry grant on the corresponding row (closes
  RBAC hole that pre-dates this issue).
- CLI commands (`da query --remote`, `da fetch`, `da query --register-bq`,
  schema/sample/estimate) now pretty-print structured BigQuery errors —
  `cross_project_forbidden`, `bq_forbidden`, `auth_failed`, etc. — instead
  of dumping the truncated JSON body. The hint that explains how to fix
  `USER_PROJECT_DENIED` (set `data_source.bigquery.billing_project` in
  `/admin/server-config`) is now actually visible to the operator.

### Changed
- `/admin/server-config` shows the resolved fallback for empty
  `data_source.bigquery.billing_project` as placeholder text (e.g.
  `(defaults to <data_project_value>)`), making the fallback chain
  visible in the UI.
- `/admin/server-config` adds a **Test connection** button next to the
  `data_source.bigquery` section. Hits `POST /api/admin/bigquery/test-connection`
  (admin-only), runs a 10s-timeout `SELECT 1` against BQ, renders typed
  success/failure inline using the same renderer that the CLI uses (§4.7).
  Closes the loop on `USER_PROJECT_DENIED` and `auth_failed` — admin
  verifies BQ reachability before any analyst hits a query failure.
```

## 5. Tests

### 5.0 JS-side placeholder rendering (§4.7.5)

The placeholder rendering is JS that lives in `admin_server_config.html` — the project today has **no JS test infrastructure** (no Playwright, no jsdom, no Selenium). Adding either to the dep graph for one ~15-line diff is disproportionate. The JS change is verified by:

- **Server-side test** (§5.1 `tests/test_admin_server_config_placeholder.py`): asserts `placeholder_from: ["data_source", "bigquery", "project"]` lands in the GET `/api/admin/server-config` response payload. This is the contract the JS reads — if the contract holds, the JS has the data it needs.
- **Manual scenario** §5.3 item 6: admin opens server-config with `data_source.bigquery.project` set but `billing_project` empty, sees `(defaults to <project>)` greyed in the input. One-time visual check pre-deploy.

If the JS sketch in §4.7.5 doesn't render correctly, manual scenario catches it before merge. Documented limitation: future PR could add Playwright if the admin UI grows enough to justify it.

### 5.1 Unit

- `tests/test_bigquery_extractor.py` — **rewrite** the existing `legacy_wrap_views=True/False` tests at lines 319/343/590/612-641. New assertions:
  - `entity_type='BASE TABLE'` → master view uses `bq."<ds>"."<tbl>"` (Storage Read API path).
  - `entity_type='VIEW'` → master view uses `bigquery_query('<project>', '<sql>')` (jobs API).
  - `entity_type='MATERIALIZED_VIEW'` → same as VIEW.
  - `entity_type='EXTERNAL'` → Storage Read API path (NEW — was previously skipped under default flag).
  - `entity_type='SNAPSHOT'` / `'CLONE'` → Storage Read API path (NEW).
  - Unknown `entity_type` → `_meta` row NOT inserted, `logger.warning` emitted, no exception.
  - `legacy_wrap_views: true` in instance overlay is silently ignored — extractor produces same SQL as without the key.
  - `legacy_wrap_views: false` in instance overlay is silently ignored — extractor produces same SQL as without the key.
- `tests/test_admin_server_config.py` — **rewrite** existing assertions at lines 810-944 that expected `legacy_wrap_views` to be in `_OPTIONAL_FIELDS`. New assertions:
  - GET `/server-config` returns `max_bytes_per_remote_query` with default `5368709120` and `kind='int'`.
  - GET `/server-config` does NOT return `legacy_wrap_views` under any section.
  - GET `/server-config`'s `data_source.bigquery.billing_project` carries `placeholder_from: ["data_source", "bigquery", "project"]`.
- `tests/test_admin_server_config_known_fields.py:179-181` — **delete** the `legacy_wrap_views` known-field assertion.
- `tests/test_api_query_guardrail.py` (new): SQL referencing a `query_mode='remote'` BQ row with mocked dry-run returning `<cap` bytes → executes; with `>cap` bytes → 400 with structured detail.
- `tests/test_api_query_guardrail.py`: CTE shadow — `WITH unit_economics AS (SELECT 1) SELECT * FROM unit_economics` does NOT trigger dry-run.
- `tests/test_api_query_guardrail.py`: query references registered name in string literal → dry-run fires (acceptable false-positive), executes with no BQ touch.
- `tests/test_api_query_rbac.py` (new or extend): caller without grant on `unit_economics` issuing `SELECT * FROM bq."finance_unit_economics"."unit_economics"` → 403.
- `tests/test_api_query_quota.py` (new): successful `--remote` BQ query records bytes against the same daily cap as `/api/v2/scan`. Pre-flight check fires too: user already over daily cap → 429 BEFORE dry-run, BEFORE execute. Concurrent slot acquired and released around execute.
- `tests/test_query_bq_regex.py` (new): tests the §4.3.1 `BQ_PATH` regex against the full 16-case matrix verified empirically (`bq."ds"."tbl"`, `bq.ds.tbl`, mixed quoting, case-insensitive, with WHERE / trailing semicolon / inside CTE body / two paths in one statement; rejection: bare registered name, quoted bare name, 2-part `bq.col`, prefix `other_bq.x.y`, middle `x.bq.y.z`, aggregate on bare; accepted false-positive: string literal containing `bq.foo.bar`).
- `tests/test_query_bigquery_query_blocked.py` (new): `POST /api/query` with `SELECT * FROM bigquery_query('proj', 'SELECT * FROM ds.tbl')` returns 400. Mixed case `BigQuery_Query` also blocked (existing blocklist lowercases `sql.strip().lower()` at app/api/query.py:39 before matching — verified).
- `tests/test_v2_client_render.py` (new): `V2ClientError.__str__` for body=`{detail: {error_kind: 'cross_project_forbidden', hint: '…', billing_project: '', data_project: 'foo'}}` produces a multi-line block with `Error:`, key/value pairs, wrapped `hint:`. Body without recognizable shape → falls back to truncated form.
- `tests/test_v2_client_render.py`: same renderer pretty-prints the new `/api/query` cost-guardrail rejection (`{reason: 'remote_scan_too_large', scan_bytes, limit_bytes, tables, suggestion}`).
- `tests/test_cli_query_render.py` (new): `da query --remote` against a server returning typed `cross_project_forbidden` produces the structured stderr output (kind line + key/value lines + wrapped hint), exit code 1. `da query --register-bq` (hybrid) same end-to-end via `RemoteQueryError.details`. `da fetch` same.
- `tests/test_remote_query_error_details.py` (new): `src/remote_query.py:RemoteQueryError` carries `details: dict | None` populated from wrapped `BqAccessError.details`. `_query_hybrid` builds the synthetic `{'detail': {'kind': ..., **details}}` and calls renderer.
- `tests/test_admin_bigquery_test_connection.py` (new): `POST /api/admin/bigquery/test-connection`. Cases: (a) admin + reachable BQ (mocked client) → 200 with `ok=true` and resolved projects; (b) admin + `BqAccessError(not_configured)` → 400; (c) admin + simulated `cross_project_forbidden` → 502 with typed detail; (d) admin + 10s timeout → 504 with `kind=timeout`; (e) non-admin → 403; (f) unauthenticated → 401.
- `tests/test_admin_server_config_placeholder.py` (new): GET `/server-config` payload for `data_source.bigquery.billing_project` includes `placeholder_from: ["data_source", "bigquery", "project"]` so the template renderer can resolve the fallback display.

### 5.2 Integration

- `tests/integration/test_query_remote_e2e.py` (new, gated on `BIGQUERY_INTEGRATION_TEST=1`): with a real BQ project + a registered VIEW row, `POST /api/query` with `SELECT COUNT(*) FROM <id>` returns the count, hits jobs API via `bigquery_query()`. Cost guardrail at high cap (TB) does not trigger.
- Same with cap set to 1 byte → 400.
- **Wrap-view runtime correctness for VIEW** (rev4 review test plan #1): the registry expects a 3-part `(project, dataset=bucket, source_table)` shape. INFORMATION_SCHEMA is NOT a regular dataset and the `_detect_table_type` query reads `<dataset>.INFORMATION_SCHEMA.TABLES` — meta-views don't appear there, so an INFORMATION_SCHEMA target would fail the entity-detect step. Use a real public-BQ VIEW instead. Implementer should run `bq ls --max_results=1000 bigquery-public-data:utility_us` (or similar dataset) and pick a confirmed VIEW entity; `bigquery-public-data.utility_us.us_states_area` is a known candidate. The test then: register that VIEW row; run extractor; ATTACH the resulting `extract.duckdb`; execute `SELECT COUNT(*) FROM <table_name>` against the master view. Assert: returns a non-negative integer without raising. Cleanup: deregister the test row. Closes the runtime-evidence gap for the `bigquery_query()` path on VIEW entities.
- **`find_by_bq_path` ambiguity** (rev4 review test plan #2): `tests/test_table_registry_find_by_bq_path.py` (new). Insert two rows with `source_type='bigquery'` and identical `(bucket, source_table)`, different `id`/`name`/`registered_at`. Assert `find_by_bq_path(...)` returns the OLDEST row by `registered_at`. Edge: zero rows → returns `None`.

### 5.3 Manual on dev VM (post-deploy)

**Precondition:** before running these scenarios, regenerate the BQ extract so the new wrap-view code path produces master views for VIEW/MATERIALIZED_VIEW entities. Either wait for the next scheduler tick (~5 min) or trigger explicitly: `curl -X POST -H "Authorization: Bearer $AGNES_PAT" https://<dev-host>/api/sync/trigger?source=bigquery`. Without this step, scenario 1 still returns the OLD catalog error and looks like the fix didn't ship.

1. `da query --remote "SELECT COUNT(*) FROM unit_economics"` → returns count, no error. (BQ optimizes COUNT on VIEW via metadata; dry-run reports near-zero bytes; well under 5 GiB cap.)
2. `da query --remote "SELECT * FROM unit_economics LIMIT 100"` → 400 (full SELECT * exceeds 5 GiB on a multi-GB view) with `tables: ["unit_economics"]` + `suggestion: "Use 'da fetch ..."`. CLI renders multi-line block, not raw JSON.
3. `da fetch unit_economics --select cnt --where "date = CURRENT_DATE()"` → unaffected (separate path).
4. `/admin/server-config` UI shows `max_bytes_per_remote_query` field, can be edited and persists.
5. `/admin/server-config` UI does NOT show `legacy_wrap_views` field anywhere.
6. `/admin/server-config` UI shows `(defaults to <project>)` placeholder under empty `billing_project` field.
7. Caller without grant on `unit_economics` issuing direct `bq."finance_unit_economics"."unit_economics"` → 403.
8. `/admin/server-config` → click "Test connection" with valid config → green status, elapsed_ms shown.
9. `/admin/server-config` → unset `billing_project`, click "Test connection" — if SA can't bill on data project, red status with the structured `cross_project_forbidden` rendering (same shape as CLI).
10. Configure invalid `billing_project` → click "Test connection" → 504 timeout or 502 typed forbidden, depending on which BQ side responds.

## 6. Implementation plan — Test-Driven (RED → GREEN → REFACTOR)

**Iron rule: no production code without a failing test first.** All §5 tests are ENTRY criteria for the corresponding §4 implementation, not a finishing touch. Each unit must be witnessed RED before any implementation lands.

Branch: `zs/fix-160-remote-query-view-entities` in a worktree (per repo convention — `feedback_always_work_in_worktree.md`).

Six phases, each a self-contained RED→GREEN→REFACTOR cycle. PR can be opened in draft after Phase 1 lands; reviewers see incremental commits.

### Phase 1 — Extractor wrap views (§4.1)

**RED tests (4 — must fail before §4.1 implementation):**
- `entity_type='BASE TABLE'` (no flag set) → asserts SQL is `CREATE OR REPLACE VIEW ... SELECT * FROM bq."<ds>"."<tbl>"`. Today fails: existing default branch creates the same SQL but only for BASE TABLE — actually this WOULD pass today. **Re-check during implementation:** if today's BASE TABLE branch already produces this exact SQL, this test is regression-green and should move to REFACTOR. Implementer verifies during Phase 1.
- `entity_type='VIEW'` (no flag set) → asserts SQL uses `bigquery_query('<project>', 'SELECT * FROM ...')`. Today fails: default branch logs "Skipping wrap view" and creates no view. ✅ truly RED.
- `entity_type='MATERIALIZED_VIEW'` (no flag set) → same as VIEW. ✅ truly RED.
- `entity_type='EXTERNAL'` → log warning, NO `_meta` insert, NO view creation. Today fails: existing default branch silently skips with no log AND DOES insert `_meta` row (current behavior — `_meta` insert at extractor.py:260-263 runs after the entity_type branch unconditionally). ✅ truly RED.

**Regression-green tests (2 — must STAY green through implementation, do NOT count as RED):**
- `entity_type=None` → existing "BQ entity not found" RuntimeError. Already passes today; assert behavior unchanged. Belongs in REFACTOR phase as a regression guard.
- overlay sets `legacy_wrap_views: true` → ignored, produces same SQL as without the key. Already passes today (existing flag-on path produces `bigquery_query()` SQL; new code produces same SQL ignoring flag). Forward-compat regression guard for stale operator yamls. Belongs in REFACTOR phase.

Run: `pytest tests/test_bigquery_extractor.py -k wrap_view -v`. Expected RED: 3 of the 4 RED tests fail (VIEW, MATERIALIZED_VIEW, EXTERNAL); BASE TABLE may already pass — confirm by running. Existing `legacy_wrap_views=True/False` toggle-tests still pass.

**GREEN:** rewrite `connectors/bigquery/extractor.py:225-258` per §4.1. Drop `legacy_wrap_views` config read.

Re-run: all 5 NEW tests pass; existing toggle-tests now FAIL (expected — they tested the removed flag).

**REFACTOR:** delete the now-failing existing toggle-tests (legacy_wrap_views=True/False fixtures). Update `app/api/admin.py:229-240, 798-805` per §4.2 (delete `_OPTIONAL_FIELDS` entry + `_BQ_OPTIONAL_FIELD_DEFAULTS` entry + comment block). Re-run full `tests/test_bigquery_extractor.py` and `tests/test_admin_server_config*.py`.

Phase 1 exit: `grep -rn 'legacy_wrap_views' connectors app src tests config cli` returns 0.

### Phase 2 — `find_by_bq_path` repo method (§4.3.4 dependency)

**RED:** `tests/test_table_registry_find_by_bq_path.py` (new):
- 0 rows matching → returns `None`
- 1 row matching → returns it
- 2+ rows matching → returns oldest by `registered_at`
- bucket=NULL excluded by guard
- case-insensitive bucket+source_table match

Run: `pytest tests/test_table_registry_find_by_bq_path.py -v`. RED: AttributeError, method doesn't exist.

**GREEN:** add ~15 LOC `find_by_bq_path` to `src/repositories/table_registry.py` per §4.3.4 SQL.

Phase 2 exit: 5/5 new tests green.

### Phase 3 — `/api/query` cost guardrail + RBAC patch + blocklist (§4.3, §4.6)

**Conventions for Phase 3 tests:**
- Quota helpers imported from `app.api.v2_quota` (NOT `v2_scan`). After GREEN step 1 below, `v2_quota` is the canonical source; `v2_scan` re-export is for backward compat of existing test files only.
- Shared fixtures live in `tests/conftest.py`: `mocked_bq_access` (provides a `BqAccess` stub with controllable `_bq_dry_run_bytes` returns), `registry_with_remote_bq_row` (inserts a `query_mode='remote'`, `source_type='bigquery'` row with known bucket+source_table). Add these to existing conftest before writing test files.
- Mocking: prefer real DuckDB analytics + real registry repo + mocked `_bq_dry_run_bytes` only. Don't mock the SQL parser or the regex.

**RED:** add tests:
- `tests/test_query_bq_regex.py` (new) — 16 case BQ_PATH regex matrix per §4.3.1.
- `tests/test_query_bigquery_query_blocked.py` (new) — `SELECT * FROM bigquery_query(...)` returns 400.
- `tests/test_api_query_guardrail.py` (new) — small-cap dry-run rejects 400; large-cap accepts; CTE shadow runs wasted dry-run; string-literal false-positive accepted.
- `tests/test_api_query_quota.py` (new) — daily-budget pre-flight 429; concurrent slot acquire/release; record_bytes post-flight; quota shared across `/api/query` and `/api/v2/scan`.
- `tests/test_api_query_rbac_bq_path.py` (new) — direct `SELECT FROM bq."ds"."tbl"` for unregistered → 403 `bq_path_not_registered`; for registered + caller without grant → 403 `bq_path_access_denied`; for admin → bypass per-name check but still requires registration.

Run: `pytest tests/test_query_*.py tests/test_api_query_*.py -v`. RED: all fail (regex unimported, blocklist passes the function, no guardrail/RBAC code exists yet).

**GREEN:** in this order to keep dependency chain clean:
1. Move `_build_quota_tracker` from `v2_scan.py:254-268` to `app/api/v2_quota.py` + function-only re-export per §4.3.3 option (a). Run `pytest tests/test_v2_scan.py` — must stay green.
2. Add `bigquery_query` to blocklist at `app/api/query.py:42-65`.
3. Implement BQ_PATH regex constant + the new pre-flight block at the §4.6 insertion point (after line 89, before line 92).
4. Wire in `find_by_bq_path` (Phase 2 dep), `is_user_admin`, `_bq_dry_run_bytes` (reuse from v2_scan).

Phase 3 exit: all Phase 3 RED tests now GREEN; existing `tests/test_v2_scan.py` + `tests/test_api_query.py` still GREEN.

### Phase 4 — CLI render shared module (§4.7)

**RED:** add tests:
- `tests/test_cli_error_render.py` (new) — typed BqAccessError dict → multi-line output; `remote_scan_too_large` dict → multi-line output; unrecognized shape → falls through to truncated form.
- `tests/test_cli_query_render.py` (new) — `da query --remote` against mocked 502 with typed body produces structured stderr; `da query --register-bq` (hybrid) same end-to-end via `RemoteQueryError.details`; `da fetch` same.
- `tests/test_remote_query_error_details.py` (new) — `RemoteQueryError.details` populated for the 11 raise sites that wrap external errors.

Run: RED — `cli/error_render.py` doesn't exist.

**GREEN:** create `cli/error_render.py`. Refactor `cli/v2_client.py:V2ClientError` (drop pre-truncate). Update `cli/commands/query.py:_query_remote` and `_query_hybrid` to call shared renderer. Audit 11 `RemoteQueryError` raise sites in `src/remote_query.py` per §4.7.3 (populate `details` where wrapping external errors).

Server side: §4.7.4 audit — fix `/api/query/hybrid:36, 40` flatten-to-string bug to use dict `detail`.

Phase 4 exit: all Phase 4 RED tests GREEN.

### Phase 5 — Admin Test Connection + placeholder UI (§4.7.5, §4.9)

**Depends on Phase 1 having merged.** Phase 5 adds `placeholder_from` to `_OPTIONAL_FIELDS["data_source"]["bigquery"]["fields"]["billing_project"]`. Phase 1 REFACTOR modifies the same `_OPTIONAL_FIELDS` block (deleting `legacy_wrap_views` entry, adding `max_bytes_per_remote_query` per §4.4). Land Phase 1's changes first to avoid merge conflicts on the same dict literal.

**RED:** add tests:
- `tests/test_admin_bigquery_test_connection.py` (new) — 6 cases per §5.1 (admin success / not_configured / cross_project_forbidden / timeout / non-admin 403 / unauth 401).
- `tests/test_admin_server_config_placeholder.py` (new) — GET payload includes `placeholder_from`.

Run: RED — endpoint doesn't exist; `placeholder_from` not in payload.

**GREEN:** create `app/api/admin_bigquery_test.py` per §4.9. Add `placeholder_from` to `_OPTIONAL_FIELDS["data_source"]["bigquery"]["fields"]["billing_project"]`. Apply the JS diff to `admin_server_config.html:315-319` per §4.7.5.

Phase 5 exit: server tests GREEN. JS placeholder rendering verified by **manual** §5.3 item 6 (no JS test infra — see §5.0).

### Phase 6 — Documentation + manual verification

No new code. Apply `config/claude_md_template.txt`, `cli/skills/agnes-data-querying.md`, `cli/skills/agnes-table-registration.md:121`, root `CLAUDE.md` edits per §4.5. Update CHANGELOG.md per §4.10.

Run full test suite: `pytest tests/ -v`. All green.

Deploy to dev VM. Run §5.3 manual scenarios 1-10 in order. **Each scenario records the expected output** in the PR description as the closure evidence.

Integration test (gated on `BIGQUERY_INTEGRATION_TEST=1`): use `bigquery-public-data.utility_us.us_states_area` (must be confirmed VIEW via `bq ls` first) per §5.2.

PR opens for review after Phase 6 with all phase checkpoints visible in the commit graph.

### Commit granularity

Each phase splits into multiple commits for reviewability. PR shows the TDD cycle in the commit graph.

**Phase 1** — 2 commits:
- `1a` GREEN: add new entity-type branches in `extractor.py` (flag still readable but ignored). New tests pass.
- `1b` REFACTOR: delete `legacy_wrap_views` config knob across 9 files (33 references per §4.2 grep table). Update existing toggle-tests. Forward-compat regression test added.

**Phase 2** — 1 commit. RED + GREEN together (small footprint, ~15 LOC + 5 tests).

**Phase 3** — 3 commits:
- `3a` Quota relocation: move `_build_quota_tracker` + `_quota_singleton` to `v2_quota.py`; function-only re-export from `v2_scan.py`. Run existing `tests/test_v2_scan.py` — must stay green. Mechanical, easy to revert.
- `3b` RED: add 5 test files + conftest fixtures. All fail with the expected import / 404 / wrong-shape errors.
- `3c` GREEN: implement `/api/query` pre-flight (RBAC + guardrail + quota wiring) + add `bigquery_query` to blocklist. All 5 test files green. Existing `test_api_query.py` stays green.

**Phase 4** — 2 commits:
- `4a` RED: 3 test files fail with ImportError (`cli/error_render.py` missing).
- `4b` GREEN: create renderer + refactor `V2ClientError` + update `_query_remote`/`_query_hybrid` + audit 11 `RemoteQueryError` raise sites + fix `/api/query/hybrid` flatten-to-string bug.

**Phase 5** — 2 commits:
- `5a` RED: 2 test files fail (endpoint missing, `placeholder_from` missing from payload).
- `5b` GREEN: create `app/api/admin_bigquery_test.py`; add `placeholder_from` to `_OPTIONAL_FIELDS`; apply JS diff to `admin_server_config.html`.

**Phase 6** — 1 commit (docs only).

Total: ~11 commits. PR opens for review after Phase 6. Reviewer sees clean RED→GREEN cycles in the commit graph.

### Rollout (post-merge)

1. Cut release 0.31.0.
2. Existing instances pick up the new code on next deploy. Master views regenerate via the next BQ scheduler tick OR `POST /api/sync/trigger?source=bigquery`.
3. **No `rm -rf /data/extracts/bigquery/` required.** `init_extract` writes to a tmp file and atomically `shutil.move`s over `extract.duckdb` (`extractor.py:277-285`); next sync produces fresh wrap views. Old views vanish, new views appear. Idempotent.
4. Verify with §5.3 scenarios on production. Close #160 with the closure-comment per §8.

## 7. Open questions

(none — scoped to closure with reviewer feedback addressed)

## 8. PR description + issue closure messaging

Reporter (`pcernik-grpn`) filed #160 narrowly as a CLI bug. Spec adds collateral fixes (cost guardrail, RBAC patch, render fix). To avoid confusion when Pavel re-tests and hits expected-but-unfamiliar 400/403s, the **PR description** must call out:

1. **First-run requires extract regeneration.** Before re-running the original repro command, hit `POST /api/sync/trigger?source=bigquery` (or wait for the scheduler tick). The wrap-view code only takes effect after the next BQ sync rebuilds extract.duckdb.

2. **`SELECT *` against VIEWs may hit the new 5 GiB cap** — by design. `bigquery_query()` does not push DuckDB-side WHERE/LIMIT down into the BQ view body, so the dry-run estimates a full scan. Cheap aggregates (COUNT, MIN/MAX with metadata pruning) typically fit; broad SELECTs may not. The structured 400 names the bytes and suggests `da fetch` with predicates. **Not a regression** — the prior catalog error meant zero work happened; the new behavior at least runs cheap aggregates and rejects expensive ones with actionable guidance. Document this in the issue closure comment so Pavel doesn't perceive it as a side-grade.

3. **`da fetch` still requires `data_source.bigquery.billing_project`** to be set if the SA can't bill on the data project — that's the operator side of Pavel's "Related" note, scoped out of #160 by Pavel's own framing. The CLI render fix and Test Connection button make discovery easier; they do not auto-fix the misconfig. Closure comment should say: "Closes #160 (the CLI bug). The `USER_PROJECT_DENIED` mentioned in 'Related' remains an operator task — set `billing_project` via /admin/server-config (use the new Test Connection button to verify); separate issue if the operator-side workflow needs more polish."

4. **CHANGELOG bullets** should mention the cost cap as `### Added` with a note that prior queries that returned the catalog error may now return `remote_scan_too_large` — same set of users, different but equally-actionable error.
