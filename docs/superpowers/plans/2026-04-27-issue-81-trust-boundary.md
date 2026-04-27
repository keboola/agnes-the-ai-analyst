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
    "keboola",   # Keboola Storage extension (community)
    "bigquery",  # BigQuery extension (community)
    "postgres",  # built-in
    "mysql",     # built-in
    "sqlite",    # built-in
}
```

Operators who need a different extension can override via env var:
`AGNES_REMOTE_ATTACH_EXTENSIONS=keboola,bigquery,custom_thing`. Document this in
`config/.env.template` and `docs/DEPLOYMENT.md`.

**Side note on `INSTALL FROM community`**: keep the `INSTALL` step but only for
extensions on the allowlist. Long term, we should pre-install allowed extensions at
image build time and skip the runtime `INSTALL` entirely (separate cleanup, not part
of this PR).

### A.2 Token-env-var allowlist

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

Allowlist policy:
- Names must match `^[A-Z][A-Z0-9_]{0,63}$` (already implied — env var convention).
- Must NOT match a denylist of well-known runtime secrets:
  `JWT_SECRET_KEY`, `SESSION_SECRET`, `DATABASE_URL`, `OPENAI_API_KEY`, …
- Must match one of:
  - the suffix pattern `_TOKEN`, `_API_KEY`, `_SECRET_TOKEN`, `_AUTH`, OR
  - an explicit allowlist `AGNES_REMOTE_ATTACH_TOKEN_ENVS` (comma-separated env-var
    names) for cases where the convention does not hold.

The point is to make accidental exfiltration impossible by structure. A connector that
writes `token_env = "SESSION_SECRET"` is not asking for credentials, it's asking the
orchestrator to send our session-signing key to its server.

### A.3 URL escape

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

Plus a scheme allowlist on `url`:
```python
parsed = urlparse(url)
if parsed.scheme not in _ALLOWED_URL_SCHEMES:  # {"https", "bigquery", "postgres", ...}
    logger.error("Remote attach %s: URL scheme %r not allowed", alias, parsed.scheme)
    continue
```

This prevents `file://` (local file disclosure), `http://` (token-in-clear), and any
scheme the underlying DuckDB extension might support that we did not intend to expose.

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
- **Security (CRITICAL)**: hardened the connector → orchestrator trust boundary
  (issue #81). Three fixes in `src/orchestrator.py::_attach_remote_extensions`:
  (1) DuckDB extensions referenced by `_remote_attach` are now matched against an
  allowlist (default: `keboola, bigquery, postgres, mysql, sqlite`; override via
  `AGNES_REMOTE_ATTACH_EXTENSIONS`); (2) `token_env` names must match a structural
  policy (`_TOKEN/_API_KEY/_AUTH` suffix, denylist of well-known runtime secrets like
  `JWT_SECRET_KEY` / `SESSION_SECRET` / `DATABASE_URL`, or operator allowlist via
  `AGNES_REMOTE_ATTACH_TOKEN_ENVS`); (3) the URL passed to `ATTACH` is now
  single-quote-escaped (mirrors `src/db.py:411`) and its scheme is checked against
  an allowlist (`https, bigquery, postgres, mysql, sqlite`).
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

### B.2 Scheduler integration

`services/scheduler/run.py` (or wherever the scheduler dispatches connectors) treats
exit code 2 as a partial-failure signal: log loudly, mark `sync_state.status =
"partial_failure"` for the source, do NOT advance `last_successful_sync_at`. The
existing behavior for exit 0/1 is unchanged.

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

## 6. Sequencing

| PR | Branch | Depends on | Ship before |
|---|---|---|---|
| A — C1 hardening | `zs/fix-81-orchestrator-attach-hardening` | — | next public release |
| B — partial-failure exit | `zs/fix-81-keboola-partial-failure` | — | next minor |
| C — view collisions | `zs/fix-81-view-collision-detection` | A merged (so the security path is clean before adding feature work to the same file) | next minor |

A is a blocker for the public OSS release (paired with the rest of #88's leak cleanup).
B and C can land any time after.

## 7. Open questions for review

- **Allowlist source of truth.** Are operator overrides via env vars enough, or do we
  want a YAML allowlist in `instance.yaml`? Env vars are simpler; YAML is auditable.
- **Token-env naming convention.** Is the `_TOKEN/_API_KEY/_AUTH` suffix rule too loose
  (a connector can still ask for `AWS_ACCESS_KEY_AUTH` if such env exists)? An explicit
  allowlist + denylist may be enough; the suffix rule was a convenience that
  back-stops the human-error case.
- **Scheme allowlist for non-HTTP extensions.** BigQuery uses a `bigquery://` URL?
  Let me confirm before finalising the scheme list.
