# Table Access Policies — Design

**Date:** 2026-08-11
**Status:** Draft for review (rewritten after security / UX / architecture review)
**Verified against:** `0.83.4` (HEAD `4cdd2c824`), schema `v115`, DuckDB 1.5.2, sqlglot 30.6.0
**Supersedes:** [#698](https://github.com/keboola/agnes-the-ai-analyst/issues/698) (row-level security), [#964](https://github.com/keboola/agnes-the-ai-analyst/issues/964) (column masking)
**Depends on:** [#1264](https://github.com/keboola/agnes-the-ai-analyst/pull/1264) (SQL-as-string table functions), [#1265](https://github.com/keboola/agnes-the-ai-analyst/pull/1265) (server-side distribution gate)

## 1. Context & goal

Agnes enforces access at **table grain**: a data package grants a table to a
group, and every member sees every row and every column of it. There is no way
to say "these people read this table, but only their own slice" — the normal
shape of a shared operational table (invoices per cost centre, tickets per
team, contracts per region).

Row-scoping exists in exactly one place today: the **internal** connector
(`agnes_sessions` / `agnes_telemetry` / `agnes_audit`), where
`build_filter_clause` (`connectors/internal/access.py:144`) derives a `WHERE`
from the authenticated user. It is hard-coded to three tables and two filter
kinds.

**Goal:** let an admin attach **one SQL policy per registered table** that
Agnes substitutes for that table on every server-side read, with the caller's
identity available as bound variables. One primitive covers both directions the
two open issues asked for separately:

```sql
SELECT * EXCLUDE (national_id, email), md5(email) AS email
FROM invoices
WHERE list_contains($user_groups, cost_center)
```

(Note the `EXCLUDE` list carries `email` too, not just `national_id` — a
column re-derived under its own name must also be excluded from the star, or
the output carries two columns named `email` and every serializer downstream
picks the plaintext one under the plain name. Agnes rejects a policy whose
resolved output has a duplicate column name at save time,
`policy_duplicate_output_column` — see §14.6.)

The policy is *data* — authored, versioned and audited — not a code change and
not a separate registered table per audience.

### 1.1 What changed in this rewrite

The first draft was reviewed for security, UX, and factual accuracy against the
codebase. Three of its load-bearing claims were wrong and are corrected here;
they are called out where they occur rather than listed, but the two that
change the design are:

- **The reason given for rejecting `TEMP VIEW` did not hold** (§5.1). The
  bleed-across-requests hazard is real for the `system.duckdb` singleton, not
  for the analytics connection, which is opened fresh per request
  (`src/db.py:2901`). The mechanism chosen here still is not a TEMP VIEW, but
  for a different and narrower reason.
- **There is no chokepoint, and half the enforcement points cannot be reached
  by an AST rewrite at all** (§5, §8). Three surfaces read `read_parquet` off
  the file and never name the table in SQL. This is the single largest change:
  the design now has one resolver with two shapes, not one rewrite.

Two findings were pre-existing product vulnerabilities rather than spec
defects, and ship as standalone fixes this design then builds on: analyst SQL
could reach any table through `query('…')` (#1264), and `server_only` was
advice to the CLI rather than a server-side gate (#1265).

### 1.2 Non-goals (v1)

- **Multiple policies per table.** One table, one policy; per-audience
  differences live *inside* the SQL. This removes the "user is in two groups"
  resolution rule entirely.
- **Per-group unmask grants** (the second half of #964). A policy can branch on
  `$user_groups`; a grant type that *lifts* masking is separate.
- **Policy authoring by non-admins.**
- **Policies on distributed tables.** See §3 — this is not a deferral, it is
  the boundary the feature is defined by.

## 2. Terms

| Term | Meaning |
|---|---|
| **Policy** | The admin-authored `SELECT` stored on a table, in DuckDB dialect. |
| **Base relation** | What the policy reads from: the master view (DuckDB paths) or the physical BQ path (remote path). |
| **Policied table** | A registered table with a non-NULL policy. |
| **Distributed table** | A table whose parquet `agnes pull` downloads to a laptop. |
| **Resolver** | `policied_relation()` — the single place that turns (table, principal) into something readable. §5. |

## 3. Scope: tables that never leave the server

A policy is only enforceable where Agnes evaluates the read. Once a parquet is
on a laptop the analyst holds unfiltered bytes and queries them locally with no
server in the loop.

**A policy may only be attached to a table that is not distributed:**

```
query_mode = 'remote'   OR   server_only = TRUE
```

Both states are in scope (decided; the initial request was "remote only").
Excluding `server_only` would push operators onto the path with the hard 120s
QueryService cap on Keboola remote queries
([#678](https://github.com/keboola/agnes-the-ai-analyst/issues/678)) for no
security gain — both are equally undistributed, so the operator should pick by
table size, not by which one the feature happens to support.

### 3.1 The interlock

`server_only` was, until #1265, a flag `agnes pull` honoured and the server did
not: `GET /api/data/{id}/download` gated on `can_access_table` alone, and on
Caddy deployments `forward_auth` → `file_server` served the parquet without the
app seeing the request at all. The first draft of this spec asserted an
interlock in `_build_manifest_for_user` that did not exist.

With #1265 the gate is real and server-side (`_assert_distributable`, called
from both `check-access` and `download`, allowlisting
`query_mode IN ('local','materialized')` and not `server_only`). This design
adds the remaining two arms:

1. **Attach-time.** `PUT /api/admin/registry/{table_id}` (`app/api/admin.py:3574`
   — *not* `PATCH /api/admin/tables/{id}`, which does not exist) rejects a
   policy on a table that is neither `remote` nor `server_only`, alongside the
   `server_only ↔ query_mode` invariant that already lives there.
2. **Flip-time.** Clearing `server_only`, or moving `query_mode` to `local`, on
   a policied table is rejected by the same validator. Without this the
   interlock is one toggle away from publishing the raw table.

### 3.2 The physical-source twin

`table_registry` is unique on `id` only. Nothing stops a second row pointing at
the **same physical source** as a policied table:

```
query_mode='materialized', source_query='SELECT * FROM `proj.finance.invoices`'
```

That row materializes to a parquet and — without `server_only` — `agnes pull`
distributes it to every granted analyst, unfiltered, with no policy involved.
This is not exotic: §3 pushes operators toward materializing remote tables, so
"make a materialized copy" is a workflow this design recommends. One forgotten
flag publishes the table.

**Rule:** a registry write is rejected when its `source_query` or
`(bucket, source_table)` / `bq_fqn` resolves to the physical source of a
policied table, unless the new row is itself non-distributed. The check runs on
the same validator as §3.1 so there is one place to read.

### 3.3 Adopting a table that is already distributed

The sensitive tables are the ones already in someone's `parquet/` directory.
Flipping to `server_only` makes the table undistributable, and `agnes pull`
already prunes local copies of tables that became de-authorized or
`server_only` (`cli/lib/pull.py:1199-1218`). No new client mechanism.

Two limits belong in the admin dialog, not only here:

- **Not immediate** — the copy clears on the analyst's *next* pull.
- **Not a recall** — snapshots, notebooks and exports are out of reach by
  construction.

Attaching a policy stops the next disclosure; it does not undo one.

### 3.4 Snapshots

`agnes snapshot create` deliberately puts a remote table's rows on the laptop,
bypassing the scan cap. A policied slice therefore *does* land locally and
would keep serving after the policy tightens. Rather than carve this out of §3,
snapshots carry a **policy fingerprint** (§10.3) and go stale when it changes —
reusing `snapshot_views_blocked` (`cli/lib/pull.py:102-106`), which already
exists to withhold view names that would otherwise resolve to stale rows.

## 4. Data model

Three nullable columns plus one flag on `table_registry` — no new table,
because there is one policy per table:

| Column | Type | Default | Meaning |
|---|---|---|---|
| `access_policy_sql` | `VARCHAR` | `NULL` | The policy `SELECT`, DuckDB dialect. `NULL` = no policy, table behaves exactly as today. |
| `access_policy_note` | `VARCHAR` | `NULL` | **Required when `access_policy_sql` is set.** Why this policy exists. |
| `access_policy_updated_at` | `TIMESTAMP` | `NULL` | Last edit. |
| `access_policy_updated_by` | `VARCHAR` | `NULL` | Who (convenience; `audit_log` is authoritative). |
| `policy_mapping` | `BOOLEAN` | `FALSE` | This table may be referenced from another table's policy body (§15). |

`access_policy_note` is mandatory for the same reason the preview is (§13): a
feature that is destructive by omission earns a mandatory "why". The inheriting
admin who finds forty lines of SQL joining `user_access` otherwise has no way
to learn whether it encodes a legal requirement or a hunch — and the safe move
is always "leave it alone", so bad policies calcify.

**No policy = no behaviour change.** Every path below short-circuits on
`access_policy_sql IS NULL`.

## 5. Mechanism: one resolver, two shapes

The first draft proposed a single AST rewrite. That covers the SQL surfaces and
is inapplicable to the rest: `/api/v2/sample`, `/api/v2/scan` (local branch)
and `/api/v2/schema` open a throwaway DuckDB and `read_parquet` the file
directly — there is no table reference to rewrite. Any design with one shape
leaves those three unenforced, which is where `agnes describe`,
`agnes snapshot create` and every agent's first three commands live.

So there is **one resolver** and two consumers:

```
policied_relation(table_id, principal) -> Relation
    .sql          # the SELECT to read from, already policy-wrapped (or the raw source)
    .params       # bound values for it
    .policied     # bool — feeds §10 disclosure and §11 effective schema
```

- **SQL surfaces** substitute the resolver's relation for the table's node in
  the parsed tree.
- **table_id surfaces** build their `FROM` from the resolver instead of
  `read_parquet(...)` / the BQ path.

`src/access_policy.py` owns it. It is the chokepoint the codebase currently
lacks: the only shared helper today is `_enforce_non_admin_sql_rbac`
(`app/api/query.py:412`), called from two places, while the table_id surfaces
use `can_access_table` directly.

### 5.1 Why not `TEMP VIEW`

The first draft said TEMP VIEWs bleed across concurrent requests because the
handler pool shares a connection. **That is wrong for the analytics database.**
`get_analytics_db_readonly()` opens a fresh connection per call
(`src/db.py:2901`, "open-file-and-re-ATTACH-every-request path"); the RW
singleton is statically banned from request paths. The bleed hazard documented
in `connectors/internal/access.py` is about the `system.duckdb` singleton.

The reason to reject it is narrower and survives: a TEMP VIEW shadowing the
table's own name cannot read that name (`CREATE TEMP VIEW invoices AS SELECT *
FROM main.invoices` → `Binder Error: infinite recursion detected`), so the
policy body would have to reference something other than what the admin wrote —
and, decisively, **a TEMP VIEW does nothing for the three table_id surfaces**,
which never consult the catalog. Choosing it would mean building the resolver
anyway and having two mechanisms.

Credit where due: a TEMP VIEW *does* shadow `main.`-qualified references, which
is the one thing a bare CTE wrap does not. That property is why §5.2 rule 3
exists.

### 5.2 The AST rewrite (SQL surfaces)

`sqlglot` is already used on analyst SQL in this very module
(`app/api/query.py:901`, the file-table-source guard). Verified on sqlglot
30.6.0, including the qualified-and-aliased form a CTE wrap would miss:

```
IN : SELECT * FROM analytics.main.invoices i JOIN dim d ON d.k = i.k
OUT: SELECT * FROM (<policy>) AS i JOIN dim AS d ON d.k = i.k
```

Rules:

1. **Applied exactly once, non-recursively.** The policy body's own
   `FROM invoices` is the base relation and is not itself rewritten.
2. **Alias preserved**, so the rest of the query is untouched.
3. **Unparseable SQL is rejected, never passed through** — 400 naming the
   table. Queries touching no policied table are unaffected. This is the
   fail-closed half of the property TEMP VIEW would have given for free.
4. **Name collisions rejected**, checked on subquery/derived-table aliases as
   well as `exp.CTE` — `SELECT * FROM t AS x, (SELECT 1) invoices` puts the
   shadowing name on a `Subquery`. The error must be structured, because
   `WITH invoices AS (SELECT … FROM invoices …)` is the single most common
   analyst idiom and the default shape an LLM writes; see §16.
5. **Table sources are allowlisted, not denylisted.** After parsing, every
   table-producing node must resolve to a known registry entry. SQL-as-string
   functions (`query`, `query_table`, `bigquery_query`, `postgres_query`, …)
   are rejected outright — #1264 does this globally; a policied query must not
   depend on that list staying complete.
6. **Physical source paths are blocked for policied tables.** For BQ this
   extends the existing registered-path gate. **For Keboola there is no gate to
   extend and one must be built**: `_local_extract_catalogs`
   (`app/api/query.py:344`) deliberately excludes remote-extension catalogs, so
   `kbc."bucket"."table"` is reachable today — and `_build_materialized_hint`
   actively suggests it. Name matching alone will not do: the rewrite keys on
   the registry **name** while a `kbc.` path names `bucket`/`source_table`.

### 5.3 `id` vs `name`

Master views are created under the registry `name` (`src/orchestrator.py`), the
filesystem-fallback branch uses `table_id`, grants key on `id`, and
`_enforce_non_admin_sql_rbac` maps name→id explicitly to survive the
difference. A policy attaches to the **`id`**; the resolver matches on both,
resolving name→id first. Every helper takes the id.

## 6. Variables and binding

### 6.1 Agnes consumes identity, it does not own it — and what that rules out

A reviewer asked whether the variable set should grow: per-user attributes
(`$user.country`), per-group attributes, nested groups, and an endpoint that
publishes these back to the source systems. The answer shapes the whole feature,
so it is stated here rather than left to §21.

**Agnes is the place where access is *enforced*, not where it is *defined*.**
The authority for who-is-what is the identity provider (Google Workspace) and
the source systems; Agnes reads that truth and applies it. Everything below
follows from that one decision.

- **Per-user attributes are already expressible — as data, not as a new
  identity field.** `$user.country` and
  `WHERE country IN (SELECT country FROM user_access WHERE email = $user_email)`
  do the identical job; the mapping table (§15) *is* the dynamic attribute
  store, table-shaped. Making `country` a hand-edited field in Agnes would
  manufacture a second source of truth that drifts from the IdP — the exact
  failure the mapping-table pattern exists to avoid. A `$user.<attr>` sugar over
  one designated attribute table is a **v2 ergonomics layer** (it saves writing
  the join in N policies), explicitly not a v1 capability and not a new
  authority.

- **Per-group attributes are a mapping table with extra steps.** A group is
  already a named set. If a group encodes a country, `$user_groups` carries it;
  if a group implies a *set* of values, that is a `group_countries(group, …)`
  table — joinable and versioned. A key-value bag on the group adds no
  capability the mapping table lacks, so it is out.

- **Nested groups are a separate project, not a line in this feature.** Agnes
  RBAC is deliberately flat (`docs/RBAC.md`: two layers, no hierarchy).
  Nesting would touch `get_accessible_tables`, `StackResolver`, and every grant
  resolution — far beyond policies — and is emulable today by granting the same
  values to several groups or via a mapping table. If it is wanted it gets its
  own brainstorm; this design assumes the flat model and does nothing that would
  block a later hierarchy.

- **Push filtering to the source where the source can do it.** BigQuery
  row-level security / authorized views are *strictly better* than an Agnes
  policy: the data never leaves the source unfiltered and nothing has to trust
  the rewrite. Agnes policies exist for the tables the source *won't* filter.
  So the source-side path is the superior alternative, not a complement — and
  the mapping it needs comes from the shared identity truth (IdP), **not** from
  an Agnes endpoint the warehouse depends on, which would invert the
  dependency (an analytics harness becoming infrastructure for the warehouse).
  Agnes may *expose* what it already resolved for a caller —
  `GET /api/me/effective-access` half does this today (§10.2) — as a
  convenience and debugging surface, never as the master others read from.

The variable set for v1 is therefore exactly the three below. It grows only by
the v2 attribute sugar above, and only once repeated joins prove the need.

### 6.2 The three variables

Variables are **bound parameters**, never interpolated. Verified on DuckDB
1.5.2: named scalar and `LIST(VARCHAR)` parameters both bind.

| Variable | Type | Source |
|---|---|---|
| `$user_email` | `VARCHAR` | authenticated identity |
| `$user_id` | `VARCHAR` | `users.id` |
| `$user_groups` | `LIST(VARCHAR)` | effective group names, read live (§6.4) |

**A variable may only stand where a value stands.** `FROM $table`,
`EXCLUDE ($col)`, or any identifier position is rejected at save. That is the
whole safety argument: the admin fixes the query's *structure*, the caller
supplies only *values*.

### 6.3 Identity values must not reach pattern position

The claim "only values come from the caller, so shape cannot change" is *false*
for pattern operators. Verified:

```
WHERE owner LIKE $user_email
  $user_email = 'alice@x.com' → 1 row
  $user_email = '%@x.com'     → 3 rows   (everyone)
```

Emails are constrained if the internal connector's regex is reused. **Group
names are not validated anywhere** — a Workspace-synced or admin-created group
named `%` silently widens every pattern-matching policy. Therefore: the
save-time validator **rejects identity variables in `LIKE` / `ILIKE` /
`SIMILAR TO` / regex-function position**, and group names are validated against
a character class excluding pattern metacharacters before binding.

### 6.4 Group membership is read live — through the existing path

The first draft framed this as a deliberate inconsistency with the rest of
RBAC. It is not one: the authorization path **already** reads membership live
per request — `get_accessible_tables` → `StackResolver`, and
`user_group_members_repo().list_group_names_for_user(user_id)` exists on both
backends. What is a sign-in snapshot is the Workspace *sync*, not the read.

`$user_groups` resolves through that same call. The policy path then inherits
exactly the staleness characteristics of every other authorization check —
no more, no less. Resolving it any other way would make policies *diverge* from
`get_accessible_tables`, which is the worse outcome.

### 6.5 Group-membership idiom

Use `list_contains($user_groups, col)`. This is not style: it is the only idiom
that survives the DuckDB→BigQuery transpile cleanly (§7).

```
list_contains($g, unit)          → EXISTS(SELECT 1 FROM UNNEST(@g) AS _col WHERE _col = unit)
unit IN (SELECT unnest($g))      → a GENERATE_ARRAY/CROSS JOIN construct ~10× longer
```

The editor prefills and the docs teach `list_contains`; the validator warns
when a policy on a remote table uses the `unnest` form.

## 7. The BigQuery path

BQ-remote tables are in scope by decision, so this must be solved rather than
deferred. It is the hardest part of the design and the part most likely to fail
open.

### 7.1 The push-down wrapper cannot carry a bind

Remote queries are rewritten to
`SELECT * FROM bigquery_query('<billing>', $bqq_inner$<inner>$bqq_inner$)`
(`app/api/query.py:1934`). A `$user_groups` inside that dollar-quoted payload
is **not** bound — DuckDB raises `Parameter argument/count mismatch, identifiers
of the excess parameters: user_groups` (verified). The predicate would reach
BigQuery as the literal text `$user_groups`.

**Therefore: a query touching a policied BQ table does not use the
`bigquery_query()` push-down.** It runs through the jobs-API path
(`run_bq_query_to_arrow`, `connectors/bigquery/access.py:237`), which today
takes `(bq, sql, *, labels)` and **no parameters at all** — it gains a
`query_parameters` argument passed through to `QueryJobConfig`.

### 7.2 Dialect: transpile, do not ask the admin

The policy is authored once, in DuckDB dialect, and sqlglot transpiles it for
the BQ path. Verified end to end on sqlglot 30.6.0:

```
SELECT * EXCLUDE (national_id, email), md5(email) AS email FROM invoices
WHERE list_contains($user_groups, cost_center)

→ SELECT * EXCEPT (national_id, email), TO_HEX(MD5(email)) AS email FROM invoices
  WHERE EXISTS(SELECT 1 FROM UNNEST(@user_groups) AS _col WHERE _col = cost_center)
```

`EXCLUDE`→`EXCEPT`, `md5`→`TO_HEX(MD5(...))`, and — the part that makes this
work — **`$param` → `@param`**, which is exactly BigQuery's named-parameter
syntax. The marker survives the transpile, so §6's binding guarantee holds on
both engines with one authored policy.

The admin never writes BQ SQL. The save-time preview shows the transpiled form
(§13) so what BigQuery will run is visible, not implied.

### 7.3 Ordering against the existing rewriter

Two rewriters contend for the same SQL. The policy substitution runs **first**
and resolves the policy against the **physical** BQ path, so the substituted
subtree is already in its final form and the bare-name→backtick pass has
nothing left to do inside it. Substituting afterwards would leave sqlglot
nothing to find; substituting before *without* resolving to the physical path
would drag the policy's own `FROM invoices` through the identifier rewrite and
violate §5.2 rule 1.

### 7.4 The fail-open fallbacks must go

When the rewritten query is rejected by BigQuery, `_looks_like_bq_rewrite_parse_error`
matches and the handler runs `analytics.execute(request.sql)` — **the original,
un-rewritten, un-policied SQL** — returning a silent 200 with unfiltered rows
(`app/api/query.py:1106`; the snapshot path has the same shape at `:2350`).

For a query touching a policied table both fallbacks are removed: the error
propagates. §17's "every failure denies" has to be enforced in code, not
asserted in a table.

`execute_query` reads `request.sql` at five points after the rewrite. The
rewritten SQL becomes a single local and the original is made unreachable —
otherwise this regresses on the next edit to that function.

## 8. Enforcement: a ratchet, not a list

The first draft listed six surfaces and called the list exhaustive. It was not,
and the proposed test derived from the same hand-written list would have
inherited the blind spot by construction.

**The guard enumerates routes** that call `can_access_table` /
`get_accessible_tables` or open the analytics DB, and asserts that set equals
the set covered by the resolver. Same shape as
`tests/test_backend_split_guard.py`, which is a static ratchet for a
structurally identical bug class.

Known surfaces at time of writing, as the ratchet's starting state:

| Surface | Shape | Note |
|---|---|---|
| `POST /api/query` | SQL | incl. the remote path |
| `run_remote_select_to_arrow` | SQL | snapshot creation; returns full results to the laptop |
| `POST /api/mcp/query-table/{id}` | table_id | builds its own `SELECT`; its 400 handler lists allowed columns → must list *effective* ones |
| `POST /api/v2/sample` | table_id | `read_parquet` / direct BQ path |
| `POST /api/v2/scan` | table_id | ditto; `--select` names columns a policy may drop |
| `GET /api/v2/schema/{id}` | table_id | skips RBAC by design today |
| `POST /api/catalog/profile/{id}/refresh` | table_id | min/max/samples, unfiltered |
| stdio MCP (`cli/mcp/server.py`) | proxy | **separate hand-written tool set**, not `foundation_tools.py` |
| Cowork stdio shim (`app/api/cowork_bundle.py`) | proxy | a third dispatch |
| Broker replay (`app/api/broker.py`) | proxy | replays `{method, path}` in-process — reaches everything above |

`app/api/mcp/foundation_tools.py` is *not* a separate surface — its `schema` /
`describe` / `query` are HTTP proxies to the REST endpoints, so enforcing at
REST covers it. `POST /api/query/hybrid` is `require_admin`, so it is out of
scope by §12's admin bypass, not by omission.

The two proxy shims and the broker matter because they are how a chat sandbox
reaches the API: they inherit enforcement only if the endpoints they replay are
enforced, which the ratchet is what guarantees.

## 9. Response caches

`_sample_cache` is keyed `f"{table_id}|{n}"` with a 1 h TTL and `_schema_cache`
on `table_id` alone — both process-global. The moment those responses become
user-dependent, **one user's slice is served to another for an hour**. This is
precisely the failure §5.1 calls worse than no policy, one layer up from where
the first draft looked for it.

For a policied table the cache is keyed on the identity tuple that feeds the
policy (`user_id` + sorted group set) or bypassed. A guard test asserts that a
policied table's cache key carries a caller-identity component. The internal
connector already excludes itself from `_sample_cache` for this exact reason —
that precedent is the one to follow.

## 10. Disclosure: the analyst must know the result is a slice

Silent row filtering makes an analyst compute `SUM(amount)` over their own cost
centre and report it as the company total. An agent does the same with more
confidence. `command-ux.md` already forbids this class outright: *silent partial
scope is forbidden* — and row filtering is the sharpest partial scope there is.

Agnes has no channel for it today: `QueryResponse` is a fixed Pydantic model
(`app/api/query.py:507`) so FastAPI strips any extra key a handler returns, and
`/api/v2/scan` returns raw Arrow IPC with no JSON envelope. The chain is
therefore part of v1:

1. **API** — `row_scope: {policied_tables: [id], note: str} | null` added to
   `QueryResponse`, `/api/v2/sample`, and `/api/v2/scan/estimate`. For
   `/api/v2/scan`, an `X-Agnes-Row-Scope` response header, since it has no body
   to carry it.
2. **CLI** — a `[scope]` line on **stderr**, preserving `--format json` on
   stdout, exactly as the existing scope-fallback note does:
   `[scope] rows in 'invoices' are filtered by an access policy — this is your slice, not the whole table`.
3. **MCP** — the tool docstring documents the field *and* instructs: if
   `row_scope` is present, qualify the answer and do not present an aggregate
   as an organisation-wide figure. A model told only the format has no reason
   to be careful about the claim.
4. **`agnes pull`** writes a `.claude/rules/` entry naming the policied tables
   in the analyst's stack. This is the only link in the chain that reaches an
   agent's context **before** it writes the query rather than after, and it
   uses the delivery channel `km_*.md` / `ka_*.md` already established.

### 10.1 The catalog badge

A policied table keeps its unfiltered row count but the count is **relabelled**,
never left bare next to a filtered result: "4.2M rows in table; your access is
policy-filtered". Per-caller counts would mean running the policy on every
catalog page load — real money on a BQ-backed table.

Rendered via `detail.side_rows` + `detail.status()` on
`catalog_table_detail.html` **and** `catalog_table_detail_legacy.html` — the
dual-surface trap this repo hits repeatedly, guarded by `TestDetailPageParity`.
The "Preview data" modal matters most: it is the one place an analyst *watches*
rows appear.

### 10.2 Self-service diagnosis

`GET /api/admin/users/{id}/effective-access` and `GET /api/me/effective-access`
already exist — the latter explicitly so non-admins can self-audit without
elevation. Both gain, per accessible table:

```
policy: {applies: bool, rows_visible: int|null,
         reason: 'ok'|'empty_slice'|'mapping_empty'|'policy_error'|'identity_unresolvable'}
```

The operator ticket "X says Agnes shows her nothing" then closes in one page
load instead of a guess-the-table hunt. The self-service half is the one that
matters: the repo has already committed to a user being able to audit their own
access, and a mechanism that silently narrows what they see must not be
invisible on their own profile page.

### 10.3 Snapshot fingerprint

`SnapshotMeta` records nothing about the policy, so a local snapshot keeps
serving pre-tightening rows indefinitely — and `--auto-snapshot` creates these
without the analyst deciding to. The scan response carries a policy fingerprint
(hash of the policy SQL + the caller's bound group set); `SnapshotMeta` stores
it; `agnes pull` blocks the view and lists it in `snapshot_views_blocked` on
mismatch.

## 11. Effective schema

`agnes schema` and the catalog column list are unfiltered, so with
`SELECT * EXCLUDE (national_id)` an analyst sees a column that no longer
exists, selects it, and gets a bare DuckDB *"column does not exist"* from inside
a rewritten derived table. Worse, `agnes snapshot create --select national_id`
**passes** the where-validator (which resolves columns against the unfiltered
schema) and fails at execution — validation that green-lights what the engine
rejects is worse than none.

§14.6 already executes the policy with `LIMIT 0` at save time. That result's
column list and types are stored as the table's **effective schema**:
`/api/v2/schema` returns it for non-admin callers with per-column `hidden` /
`masked` markers, the where-validator validates against it, and the catalog
renders the marker. Nearly free, given the probe already runs.

Profiles are the sharper leak and the badge answer does not cover them:
`min` / `max` / `sample_values` / `top_values` are row content. For a policied
table, profiles are derived from the policy's output or suppressed.

## 12. Identity resolution

`$user_email` / `$user_id` are undefined for two principals, and the first
draft's one-line claim that agent surfaces "inherit enforcement" glossed both.

- **`AgentPrincipal`** binds the **owner's** identity (`owner_user_id` /
  `owner_email`). The consequence must be stated rather than left implicit: an
  agent's scope narrows *which tables* it reaches, but `$user_groups` would
  hand it the owner's **full** row slice. An admin who writes "finance group
  only" will not expect a `selected`-scope agent to inherit finance rows, so
  the agent builder surfaces the owner's effective slice when a policied table
  enters an agent's scope.
- **`SessionPrincipal`** (co-drive) carries `participant_user_ids` — no single
  user. Today's code substitutes a sentinel that matches no row, producing
  **zero rows, HTTP 200, no explanation**, currently pinned by a test as
  correct. Generalised to business tables that is exactly the failure §15's
  empty-mapping rule exists to prevent, and §17 would score it as a pass.
  A co-drive session reading a policied table gets a **named 403**:
  `policy_identity_unresolvable` — *"`invoices` has a per-user access policy; a
  co-drive session has no single identity. Open it in a solo session."*

**Admin bypass** is gated on `_credential_surface(user) == "all"`, so a PAT
minted with `surface='stack'` — the `agnes init` default — drops an admin into
the stack branch. The same admin would be unfiltered in the web UI and filtered
in `agnes query`. This design picks: **policies follow the credential surface**,
i.e. a `stack`-surface admin token *is* filtered, because that token exists to
represent the analyst-shaped view.

## 13. Admin surface

`/admin/tables` is a package-centric accordion whose per-table edits are
**modals** — three source-specific ones dispatched by `openEditModal()` — and
it has no inline-panel idiom and no "run SQL and show rows" affordance. The
policy editor is a modal, opened from a new **Access** column rather than from
`col-actions` (fixed at 152px, already holding four icons, with a documented
overflow bug for a fifth).

- **Editor**: plain `<textarea>` + prefill button + `form-hint`. There is no
  syntax highlighting anywhere in this codebase and inventing it here is scope
  creep. The `WhereFiltersBuilder` "structured editor + raw escape hatch"
  pattern is the model if guardrails are wanted.
- **Access column**, three states: `—` (none; on a distributed table, a muted
  *not available — distributed*), a `Policy` chip with persona count, and a
  `Policy · check` warn chip when the mapping is empty or stale. Badge
  language per the design system (`--ds-accent-{success,warn}-*`, never
  `--ds-primary`, never raw hex — contract-tested).
- On a distributed table the panel is **visible but disabled with the reason
  inline**. Hidden reads as "feature absent"; disabled reads as "not applicable
  here, and here is the fix".
- **Interlock rejections render inline and pre-emptively**, reusing the
  existing `onEditBqAccessModeChange()` warning that already fires when a mode
  radio changes. Save failures today are toast-only (4 s auto-hide, firing
  while the modal is still open) — acceptable for "sync schedule invalid", not
  for a security invariant explaining a refused flip.
- **History inline**: the last N `audit_log` rows with "restore this version",
  re-running validation and preview. `audit_repo().log()` already takes
  `params_before` for exactly this, so history is free; only rendering is work.
  Canonical component: `detail.version_timeline()`.

### 13.1 The preview is a matrix, not a run

A single-persona preview with a row count against the unfiltered total catches
only the extremes — 0 rows and all rows. The dangerous middle is invisible:
4,300 of 4.2M looks identical whether it is the right 4,300 or the wrong ones.
And the permissive bug that actually happens is a `CASE` on `$user_groups` with
a missing branch falling through to the open arm — which cannot be seen by
previewing one persona.

The preview enumerates the **distinct group-sets** among users who can access
the table (bounded by group-sets, not users) and shows persona → rows →
columns, plus two derived numbers that catch the two real bugs:

- **union coverage** — rows visible to ≥1 persona vs total. Union = 100% *and*
  every persona = 100% means the policy is a no-op.
- **pairwise overlap** — for a policy meant to partition, a non-zero overlap
  where the admin expected zero *is* the permissive bug, rendered.

On edit, the matrix is shown **before and after**, so what the admin approves is
"who gains access with this change".

The preview also renders the **transpiled BQ form** (§7.2) for remote tables,
and is **audited** — it shows one person another person's slice, and "who
looked at whose data, when" is the first question asked after an incident.

### 13.2 CLI

Not a new command group. `agnes admin` is flat verb-noun (`register-table`,
`update-table`, `metadata-show`, …) with no `agnes admin table` group to hang
`table policy set` from. The closest precedent is `update-table --query`, which
takes inline SQL **or `@path/to.sql`** and clears on empty:

```
agnes admin update-table <id> --policy @policy.sql --policy-note "..."
agnes admin table-policy show|preview <id> [--json] [--as <user>] [--as-groups a,b]
```

`@file` is mandatory — nobody pastes multi-line SQL into a shell. `--json` on
every read (39 admin commands already have it). `--as-groups` because §13
promises an ad-hoc group set in the web UI and the CLI must not drop it.
Rejections name the next step per `command-ux.md`, and a preview returning 0
rows distinguishes empty slice / empty mapping / unresolvable identity rather
than printing `0`.

## 14. Validation at save time

Parsed with `sqlglot` before the row is written — never at query time, where
the failure is a live outage. **Allowlist, not denylist**, which is the same
correction §5.2 rule 5 makes for analyst SQL and the one the internal connector
already made:

1. Single statement, and it is a `SELECT`.
2. Node types from a closed permitted set; function names from a closed
   permitted set. Enumerating forbidden nodes is the pattern this design
   rejects elsewhere and would become the security boundary the moment
   non-admin authoring (§1.2) arrives.
3. Table references restricted to the policied table's own base relation plus
   explicitly marked mapping tables (§15).
4. Variables in value position only; every variable name known; none in
   pattern-match position (§6.3).
5. For a remote table, the policy must transpile to BigQuery without error, and
   the transpiled form is shown to the admin.
6. The policy executes against the real table with a throwaway persona and
   `LIMIT 0`. This both rejects a policy referencing a dropped column and
   produces the effective schema (§11). The same probe also rejects a policy
   whose resolved output has a case-insensitive duplicate column name
   (`policy_duplicate_output_column`) — the shape this doc's own §1 example
   had before it was corrected: `SELECT * EXCLUDE (national_id), md5(email)
   AS email` re-derives `email` without excluding it from the star first, so
   DuckDB returns two columns both named `email`, and every read surface
   (`/api/query`'s positional row lists, pandas' `fetchdf()` behind
   `/api/v2/sample` and `/api/mcp/query-table`) either keeps the first
   (plaintext) occurrence under the plain name or renames the second one —
   putting the unmasked value exactly where a caller expects the masked one.
   Static analysis alone cannot catch this: it needs the actual `*`
   expansion, which only the live probe has.

It is worth stating plainly what this feature is: **a policy body is arbitrary
DuckDB SQL executed on the server's analytics connection on every analyst
request.** That is a meaningful escalation of what "admin" means on an Agnes
instance, and the reason §13's audit row is not optional.

## 15. Mapping tables

The expected shape beyond a trivial predicate is a join against a mapping table
maintained **upstream in the source system** — the person → org-unit mapping
usually already exists there, and copying it into the admin UI creates a second
copy to drift:

```sql
SELECT * FROM invoices
WHERE cost_center IN (SELECT cost_center FROM user_access WHERE email = $user_email)
```

`user_access` must carry `policy_mapping = TRUE`; otherwise rule 14.3 rejects
the join. Marking it does **not** grant analysts access to it.

`policy_mapping` is a second, invisible privilege bit on a table nobody is
looking at. It fails closed, so it is safe — but it needs three things or it
becomes a hidden privilege with a delayed blast radius: the rejection in 14.3
must **name the exact table and fix**; the admin list shows a
`policy-referenceable` chip; and un-marking or unregistering a mapping table
warns that N dependent policies will start failing.

### 15.1 The empty-mapping trap

If the sync of `user_access` fails or lands empty, every dependent policy
returns zero rows — indistinguishable, from the analyst's side, from "you
legitimately have no data". Both look like a healthy Agnes with an empty table.
All three mitigations are needed:

- A policy-referenceable table with **zero rows** makes dependent policies fail
  **closed with an explicit error** ("access mapping `user_access` is empty —
  contact your administrator"), not silently return nothing. An empty result
  from a *populated* mapping is a legitimate answer and returns as one.
- The error names the mapping's `last_sync`.
- The admin list flags policied tables whose mapping is empty or stale.

## 16. Error contracts

Errors here are load-bearing, because the most common ones will be hit by an
agent that retries. All are structured `detail` dicts rendered through
`cli/error_render.py`, which already formats `reason`-keyed dicts:

| Reason | When | Must say |
|---|---|---|
| `policy_name_collision` | caller CTE/alias shadows a policied table (§5.2 r4) | the table, and *rename your CTE* — an LLM will otherwise retry the same collision |
| `policy_identity_unresolvable` | co-drive session (§12) | open it in a solo session |
| `policy_mapping_empty` | §15.1 | the mapping table and its `last_sync` |
| `policy_error` | policy failed to execute | the table; **not** the raw engine message |

That last row is a change: `/api/query` currently echoes DuckDB's message
verbatim, which for a failing policy can include literal values from the policy
body.

## 17. Failure modes

| Situation | Behaviour |
|---|---|
| Policy fails at query time | error naming the table. **Never** fall back to the unfiltered table (§7.4). |
| Caller SQL unparseable, touches a policied table | 400, table named. |
| Caller SQL unparseable, no policied table | unchanged. |
| BQ rejects the transpiled query | error. The pre-existing retry-with-original-SQL path is removed for policied queries. |
| Mapping empty | explicit error (§15.1). |
| Principal has no single identity | `policy_identity_unresolvable` (§12). |
| Cache would serve another caller's slice | impossible by key construction (§9), asserted by a guard. |

**Every failure denies.** There is no path where an error degrades to
unfiltered data.

## 18. Migration & parity

The first draft's checklist was wrong in its specifics and missed the real
landmine. There is no generic "column-add checklist" in the repo; the migration
playbook's version reference is stale. The actual required set:

- **`src/db.py` — three edit sites**, not one: the `_SYSTEM_SCHEMA` DDL, the
  fresh-install chain, and the upgrade chain (`_v115_to_v116`), plus
  `SCHEMA_VERSION` (115 → 116 at `src/db.py:59`).
- **Alembic revision** for Postgres; both ladders must reach the same endpoint
  (`tests/test_db_schema_version.py`, blocking per `CONTRIBUTING.md`).
- **`src/models/ops.py`** — the SQLAlchemy model. Without it the schema-parity
  test fails *and* the DuckDB→PG migrator dies mid-copy with `UndefinedColumn`.
- **`docs/runbooks/wal-recovery.md`** — two literal schema-version strings,
  guarded by `tests/test_runbook_wal_recovery.py`.
- **`app/api/admin.py` strip-tuple.** `PUT /registry/{table_id}` does
  `repo.register(id=table_id, **merged)` where `merged` comes from a
  `SELECT *` — so **any** new `table_registry` column that `register()` does not
  accept makes every registry PUT raise `TypeError`. Either extend `register()`
  or add the columns to the existing strip-tuple (verified at
  `app/api/admin.py:3635-3644` / `:3773`).
- **`tests/snapshots/openapi.json`** — full-document equality;
  `make update-openapi-snapshot`.
- **`tests/db_pg/test_repo_method_parity.py`** — a static AST check: a setter on
  one backend only fails.
- **Repository pair** — `table_registry.py` + `table_registry_pg.py` in the same
  PR, contract test extended. Template to clone:
  `tests/db_pg/test_server_only_column_contract.py`.
- **CHANGELOG** bullet in the same PR.

Two non-requirements worth naming so nobody spends time on them: the DuckDB→PG
migrator's `_PK_COLUMNS` map does **not** need updating (it lists only tables
whose PK is not a single `id`; `table_registry`'s is), and no `stranded` entry
should be added (that mechanism is scoped to v97–v113 and pinned by a test).

## 19. Testing

- **Rewrite fixtures** — unqualified, 2- and 3-part qualified, aliased,
  CTE-nested, subquery, `LATERAL`, `POSITIONAL JOIN`, `UNION ALL BY NAME`,
  DuckDB FROM-first, comment-interleaved; collision rejection on both `CTE` and
  `Subquery` aliases; non-recursion.
- **Binding** — group names with quotes, backslashes and SQL keywords are inert;
  pattern metacharacters rejected before binding (§6.3).
- **Transpile** — the policy corpus round-trips DuckDB→BigQuery, pinned as a
  tripwire so a sqlglot upgrade that changes `EXCLUDE`/`md5`/`$param` handling
  fails loudly. sqlglot is pinned for the same reason rule 5.2.3 exists: its
  parse coverage is now a security dependency (it cannot parse
  `SELECT * FROM t SAMPLE 50%`, which DuckDB accepts, so such queries 400 once
  a policied table is in scope — expect pressure to loosen the rule).
- **Surface ratchet** (§8) — enumerated, not listed.
- **Cache keys** (§9) — a policied table's key carries caller identity.
- **Fail-closed** — a raising policy produces an error and the response contains
  no row from the unfiltered table; both removed BQ fallbacks have a regression
  test.
- **Interlock** — policy on a distributed table refused; making a policied table
  distributable refused; the physical-source twin (§3.2) refused; the manifest
  never carries one.
- **Contract** — `tests/db_pg/` parametrises the new registry methods on both
  backends.
- **E2E** — two users, one table, one policy: each sees their own slice; the
  admin sees everything; `agnes pull` downloads nothing for that table;
  `agnes query` prints the `[scope]` line; `agnes schema` hides the excluded
  column.

## 20. Documentation

The spec is not the record. `docs/RBAC.md` currently states outright that
access is *"Table-level, not row-level … Do not model general row-level
security on this"* — the canonical reference actively tells an admin this
feature does not exist, and that sentence must go with the same PR. A
`docs/table-access-policies.md` covers authoring, the variable vocabulary, the
mapping-table pattern, and the empty-mapping failure. `CLAUDE.md`'s access
control section gains the third layer.

## 21. Relationship to the open issues

Both are subsumed and close when this lands:

- **#698 (RLS)** — a policy of the form `SELECT * FROM t WHERE col IN (…)`. Its
  `rls_column` + `table_rls_grants` model is more surface for less
  expressiveness, and its `TEMP VIEW` mechanism does not reach the table_id
  surfaces. What it got right and this keeps: the admin bypass and the
  enforcement-point instinct.
- **#964 (column masking)** — a policy of the form `SELECT * EXCLUDE (…)`. The
  per-group *unmask grant* half stays open if still wanted.

One correction to carry into both: `md5(col)` is a **pseudonym, not a mask**.
An unsalted hash over a low-entropy domain is reversible by dictionary in
minutes, and the hashed column still joins, so it works as a stable
cross-table identifier. Where masking is the goal the documented example is a
keyed HMAC with an instance secret, or plain redaction.

## 22. Decisions taken

| Question | Decision |
|---|---|
| Scope | **`remote` and `server_only`** — BQ solved in v1 (§7), not deferred. |
| Already-distributed table | **`agnes pull` prunes**; admin dialog states the two limits (§3.3). |
| `$user_groups` freshness | **Live, through the existing `StackResolver` read** — not a new mechanism and not a deliberate inconsistency (§6.4). |
| Catalog row count | **Unfiltered count, relabelled, plus badge** (§10.1). |
| Preview audited | **Yes** (§13.1). |
| table_id-shaped surfaces | **Shared resolver** — they read through it instead of `read_parquet` (§5). |
| Disclosure | **Full chain** — API + CLI + MCP + `agnes pull` rules file (§10). |
| Pre-existing holes | **Standalone PRs** — #1264, #1265 — this design builds on them rather than inheriting them. |
| Agnes: authority or consumer? | **Consumer** — identity truth lives in the IdP/source; Agnes enforces, never becomes the attribute master (§6.1). |
| Per-user attributes (`$user.country`) | **v1 = mapping tables** (already §15); `$user.<attr>` sugar is a v2 layer, only if repeated joins prove it. |
| Per-group attributes / nested groups | **Out** — mapping-table-with-extra-steps and a whole separate RBAC project respectively; both emulable today (§6.1). |
| Attribute read-back endpoint for sources | **No** — inverts the dependency; where a source can filter (BQ RLS/authorized views) that path is *better* than a policy, and its mapping comes from the IdP, not from Agnes (§6.1). |
| Phased or one delivery? | **One delivery** — one branch, one PR, one verification gate, behind `access_policies.enabled` (off by default) so merge and activation are decoupled (§23). Build order is internal, not phased shipping. |

## 23. Implementation: one delivery, behind a flag

The feature ships as **one unit of work** — one branch, one PR, one verification
gate — not a phased rollout. The safety constraint that drove the earlier
phased plan is *automatically* satisfied by shipping together: **a policy must
not be attachable until enforcement is complete**, and when the admin surface
and the enforcement arrive in the same delivery, they cannot get out of order.

The single gate that makes one-shot delivery safe is a **feature flag**
(`access_policies.enabled`, off by default). It lets the whole change land dark
on `main` — deployed by the usual `:stable` auto-upgrade before any operator has
authored a policy — and be turned on per-instance once the operator is ready.
Without the flag, "one PR" would mean the feature goes live the moment it
merges; with it, merge and activation are decoupled and the diff can land
whenever CI is green. The flag gates **attachment** (the admin surface and the
API setter), not the enforcement code — enforcement is inert whenever
`access_policy_sql IS NULL`, which is every table until someone attaches one.

### 23.1 Build order (within the one delivery)

"One go" is a statement about delivery, not about writing unordered code. Some
parts genuinely depend on others, so the work has a shape even though it merges
as a whole:

```
  storage (§4, §18)  ──►  resolver (§5)  ──┬─►  SQL rewrite (§5.2) ──► BigQuery (§7)
  validation (§14)                         ├─►  table_id surfaces (§5) ─┐
  interlock (§3.1–3.2)                      ├─►  effective schema (§11)  ├─► surface ratchet (§8)
                                            ├─►  identity resolution (§12)┘
                                            ├─►  disclosure (§10)
                                            └─►  admin surface + CLI (§13)  +  cache keying (§9)  +  docs (§20)
```

- **Storage is the trunk.** The registry columns, both migration ladders, the
  parity set (§18), save-time validation (§14), and the interlock (§3.1–3.2)
  come first because nothing reads or writes a policy without them.
- **The resolver (§5) is the single junction.** Everything downstream consumes
  `policied_relation()`; it is the one file the whole feature turns on.
- **Everything past the resolver fans out** and is mutually independent — the
  SQL rewrite, the three table_id surfaces, BigQuery, disclosure, identity,
  the admin surface. They can be built in parallel and integrated together.

This maps directly onto `/agnes-build`: it decomposes along exactly these
coupling lines, builds the independent tasks in parallel worktrees, serializes
the single migration task last, and runs `/agnes-review` on the unified diff.
The parity siblings (`table_registry.py` / `_pg.py`) and the migration ladder
stay coupled per the sync-map, which is what keeps a parallel build honest.

### 23.2 The one gate that must not be shortened

There is exactly one place where "deliver it all" has teeth: **the surface
ratchet (§8) must be green and must enumerate every surface** before the flag is
documented as usable. Every read path the ratchet misses is a path where a
policy silently does not apply — and in a single delivery there is no "later
slice" to catch it. The ratchet, the fail-closed tests (§7.4, §17), and the E2E
(§19) are the acceptance bar for the whole PR, not for a phase of it.

### 23.3 What stays out of this delivery

The two pre-existing fixes (#1264, #1265) remain **separate PRs** and are not
folded in. They close live vulnerabilities that should merge on their own
schedule; coupling a security hotfix to a feature PR would delay the fix and
widen the hotfix's blast radius. This design depends on them being in first, not
on shipping with them.
