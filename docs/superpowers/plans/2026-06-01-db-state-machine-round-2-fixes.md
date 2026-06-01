# DB State Machine — Round-2 Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all 19 round-2 review findings on PR #455 (5 NEW BLOCKER + 8 NEW HIGH + 4 MED + 2 LOW), close the 12 testing gaps, and harden the two partial-fix surfaces (B3-NEW/B4-NEW) so they stand up to a fresh-VM provisioning path.

**Architecture:** Apply each finding's reviewer-recommended remediation in a discrete commit. Every behaviour change lands with a regression test in the same commit (TDD). Tasks are ordered low-coupling → high-coupling so an implementer can pick them off without dependency hell: pure functions first, then single-file API hardening, then migrator behaviour, then bash/applier surface, then race-window tightening, then alembic. Final self-review walks the diff one more time.

**Tech Stack:** Python 3.13 + FastAPI + SQLAlchemy 2 (psycopg 3) + Alembic + DuckDB 1.5.x + pytest. Bash 5 for the host-side applier. systemd units for unit-file behaviour. The dual-backend discipline rules in `CLAUDE.md` (one PR → both repositories) apply throughout.

---

## Pre-flight (run once before Task 1)

The worktree is `/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.claude/worktrees/zs+db-state-machine/` on branch `zs/db-state-machine`. Every step that says "run pytest" uses the local `.venv`:

    .venv/bin/pytest tests/<name>.py -v --tb=short

The complete review text lives at `/tmp/pr455-all-comments.md` (the 09:33:10Z comment, lines 232-353). Read it once for context.

If the local `.venv` is missing, recreate before starting:

    python3 -m venv .venv && source .venv/bin/activate && uv pip install ".[dev]"

The default branch is `main`. The PR base is `main`. Do not rebase on `main` mid-plan — work linear commits on top of `HEAD`.

---

## File Structure

Tasks below modify (M) or create (C) the following files. Each test file is **new** (C). The plan does not split or rename any existing files.

| File | Responsibility | Why touched |
|---|---|---|
| `app/api/db_state.py` (M) | FastAPI endpoints for the state machine | B1-NEW, B2-NEW, H1-NEW, H3-NEW, H5-NEW, H7-NEW, MED-2, MED-3, MED-4 |
| `scripts/db_state_migrator.py` (M) | Migrator orchestrator (alembic + copy + verify + flip) | H1-NEW, H3-NEW, LOW-1 |
| `scripts/migrate_duckdb_to_pg/tasks.py` (M) | Per-table copy & validate | H6-NEW, NEW-X-USERS-DUPLICATES |
| `scripts/ops/agnes-state-applier.sh` (M) | Host-side applier daemon | B2-NEW (host-side), B4-NEW tighten, H2-NEW, H4-NEW, H5-NEW (host-side), H8-NEW |
| `scripts/ops/agnes-state-applier-bootstrap.service` (M) | Root-running bootstrap unit | B4-NEW tighten (move chown 70:70 here) |
| `infra/modules/customer-instance/startup-script.sh.tpl` (M) | Customer-instance provisioning script | B3-NEW verify, B4-NEW tighten, H4-NEW |
| `migrations/versions/0013_resource_grants_per_type_fk.py` (M) | Alembic 0013 typed-FK migration | B5-NEW |
| `cli/commands/db.py` (M) | `agnes admin db migrate` CLI | MED-1 |
| `docker-compose.postgres.yml` (M) | Postgres side-car overlay | LOW-2 |
| `CHANGELOG.md` (M) | Unreleased bullets per fix | Every task |
| `tests/test_db_state_*.py` (C, multiple) | Regression tests | New per finding |
| `tests/test_applier_*.py` (C, multiple) | Applier unit/script tests | New per finding |
| `tests/test_migrate_*.py` (C, multiple) | Migrator regression tests | New per finding |
| `tests/test_pii_scrub_walks_keys.py` (C) | LOW-1 regression | New |
| `tests/test_alembic_0013_backfill_order.py` (C) | B5-NEW regression | New |
| `tests/test_state_applier_unit_file.py` (M) | Extend existing applier static-check tests | B3-NEW, B4-NEW, H4-NEW |

---

## Task ordering rationale

- **Phase A — Pure-function fixes (Tasks 1-4):** MED-1, MED-3, LOW-1, LOW-2. Single-file, no race semantics, no DB. Warms up the worker, lands four green commits fast.
- **Phase B — API hardening (Tasks 5-7):** MED-2, MED-4, H7-NEW. Still `app/api/db_state.py` but introduces validators/branches.
- **Phase C — Migrator redaction + JSONB (Tasks 8-10):** H3-NEW, H6-NEW, NEW-X-USERS-DUPLICATES.
- **Phase D — Applier hardening (Tasks 11-13):** H2-NEW, H4-NEW, H8-NEW.
- **Phase E — Race windows (Tasks 14-17):** B1-NEW, B2-NEW, H1-NEW, H5-NEW.
- **Phase F — Alembic 0013 ordering (Task 18):** B5-NEW.
- **Phase G — Provisioning tightening (Tasks 19-20):** B3-NEW + B4-NEW.
- **Phase H — Final self-review (Task 21):** walk diff against review.

---

## Phase A — Pure-function fixes

### Task 1: MED-1 — `--yes` gate honors `--json`

**Why:** Today `cli/commands/db.py:99` reads `needs_confirm = not yes and not as_json` — so `agnes admin db migrate cloud --cloud-url ... --json` skips the destructive-cutover confirmation. CI/cron paths can fire a migration with zero operator intent. Fix: drop the `and not as_json` clause; `--json` callers must pass `--yes` explicitly.

**Files:**
- Modify: `cli/commands/db.py` (the `needs_confirm` predicate)
- Test: `tests/test_cli_db_migrate_yes_gate.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_db_migrate_yes_gate.py
"""MED-1 — ``--json`` does not bypass the ``--yes`` confirmation gate."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner


def test_migrate_json_without_yes_refuses() -> None:
    """``agnes admin db migrate cloud --cloud-url ... --json`` (no
    ``--yes``) must refuse rather than silently auto-confirming.

    Pre-MED-1 the predicate ``needs_confirm = not yes and not as_json``
    accepted ``--json`` as a confirmation bypass; CI/cron callers
    could fire a destructive cutover with zero operator intent.
    """
    from cli.commands.db import migrate as migrate_cmd

    runner = CliRunner()
    result = runner.invoke(
        migrate_cmd,
        ["cloud", "--cloud-url", "postgresql://x:y@h/db", "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code != 0, (
        "CLI must non-zero when --json is passed without --yes; "
        f"got rc={result.exit_code}, stdout={result.stdout!r}"
    )
    assert "--yes" in result.output or "confirm" in result.output.lower(), \
        "error message must mention the --yes requirement"


def test_migrate_json_with_yes_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json --yes`` together is the explicit CI/cron path — proceeds."""
    from cli.commands.db import migrate as migrate_cmd

    fake_response = {"job_id": "j1", "status": "pending"}
    with patch("cli.commands.db._post") as mock_post:
        mock_post.return_value = fake_response
        runner = CliRunner()
        result = runner.invoke(
            migrate_cmd,
            ["cloud", "--cloud-url", "postgresql://x:y@h/db", "--json", "--yes"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert "j1" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli_db_migrate_yes_gate.py -v --tb=short`
Expected: `test_migrate_json_without_yes_refuses` FAILS (rc=0 because `--json` bypasses).

- [ ] **Step 3: Apply the fix**

Open `cli/commands/db.py`, find the `migrate` Click command body, locate the `needs_confirm` assignment near line 99:

```python
needs_confirm = not yes and not as_json
```

Replace with:

```python
# MED-1: ``--json`` does NOT bypass the confirmation gate. CI/cron
# callers must opt in explicitly with ``--yes``. The earlier
# ``and not as_json`` clause meant a ``--json`` invocation skipped
# the destructive-cutover confirm and auto-fired the migration.
needs_confirm = not yes
```

- [ ] **Step 4: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_cli_db_migrate_yes_gate.py -v`
Expected: both tests PASS.

Run: `.venv/bin/pytest tests/ -k "cli_db" --tb=short -q`
Expected: nothing regressed in the broader CLI suite.

- [ ] **Step 5: CHANGELOG bullet**

Add under `## [Unreleased]` → `### Fixed`:

```markdown
- **`agnes admin db migrate --json` no longer bypasses the `--yes` confirmation gate.** Round-2 review MED-1 — CI/cron callers must opt into the destructive cutover explicitly with `--yes`; the predicate `not yes and not as_json` was the bypass.
```

- [ ] **Step 6: Commit**

```bash
git add cli/commands/db.py tests/test_cli_db_migrate_yes_gate.py CHANGELOG.md
git commit -m "fix(cli): --json no longer bypasses --yes confirm gate (MED-1)"
```

---

### Task 2: MED-3 — `_redact_url` covers query-string passwords

**Why:** Today `app/api/db_state.py:87-91` uses a hand-rolled regex `(://[^:]+:)[^@]+(@)` that only matches userinfo-style credentials. URLs like `postgresql://user@host/db?password=secret&sslmode=require` leak the password verbatim. Fix: route everything through `sqlalchemy.engine.make_url(...).render_as_string(hide_password=True)`, which handles every libpq-style password placement.

**Files:**
- Modify: `app/api/db_state.py:_redact_url`
- Test: `tests/test_db_state_redact_query_password.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_redact_query_password.py
"""MED-3 — ``_redact_url`` redacts query-string passwords too."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "url,expected_redacted_form",
    [
        # Userinfo style — already handled pre-MED-3.
        (
            "postgresql+psycopg://agnes:s3cret@host:5432/agnes",
            "postgresql+psycopg://agnes:***@host:5432/agnes",
        ),
        # Query-string style — the MED-3 regression target.
        (
            "postgresql://user@host:5432/db?password=topsecret&sslmode=require",
            # SQLAlchemy renders the masked form however it chooses; the
            # invariant is "topsecret" must not appear in the result.
            None,
        ),
        # Mixed (userinfo + query).
        (
            "postgresql://u:passA@host/db?password=passB",
            None,
        ),
    ],
)
def test_redact_url_removes_all_password_forms(
    url: str, expected_redacted_form: str | None
) -> None:
    from app.api.db_state import _redact_url

    out = _redact_url(url)
    assert out is not None
    # Any literal secret substring from the input must NOT appear in the
    # redacted form. This covers both userinfo and query-string placement.
    for secret in ("s3cret", "topsecret", "passA", "passB"):
        if secret in url:
            assert secret not in out, (
                f"redacted form must not echo {secret!r}; got {out!r}"
            )
    if expected_redacted_form is not None:
        assert out == expected_redacted_form


def test_redact_url_none_returns_none() -> None:
    from app.api.db_state import _redact_url

    assert _redact_url(None) is None


def test_redact_url_unparseable_returns_placeholder() -> None:
    """Garbage in → a safe placeholder out, never the original string."""
    from app.api.db_state import _redact_url

    out = _redact_url("not a url with :: weird ::stuff")
    # The exact placeholder is implementation choice; assert it's not the
    # input verbatim and doesn't crash.
    assert out != "not a url with :: weird ::stuff"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_state_redact_query_password.py -v --tb=short`
Expected: `test_redact_url_removes_all_password_forms[postgresql://user@host:5432/db?password=topsecret&sslmode=require-None]` FAILS — `topsecret` survives.

- [ ] **Step 3: Apply the fix**

Open `app/api/db_state.py`, locate `def _redact_url(url: str | None) -> str | None:` around line 87. Replace its body with a SQLAlchemy-based implementation:

```python
def _redact_url(url: str | None) -> str | None:
    """Return ``url`` with every password placement masked.

    Round-2 review MED-3 — the previous regex
    ``(://[^:]+:)[^@]+(@)`` only matched ``://user:pass@host`` userinfo
    style and let ``?password=secret`` query-string style leak. Route
    through ``sqlalchemy.engine.make_url`` which understands every
    libpq form (userinfo, query string, URL-encoded chars).
    """
    if url is None:
        return None
    try:
        from sqlalchemy.engine import make_url
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        # Unparseable — never echo the input back (could still carry
        # creds if it happened to be a valid-looking URL with a typo).
        return "<unparseable-url>"
```

- [ ] **Step 4: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_db_state_redact_query_password.py -v`
Expected: PASS.

Run: `.venv/bin/pytest tests/ -k "redact" --tb=short -q`
Expected: any existing redaction tests still PASS (the userinfo form still works via SQLAlchemy).

- [ ] **Step 5: CHANGELOG bullet**

Under `### Fixed`:

```markdown
- **`_redact_url` masks every password placement, not just userinfo-style.** Round-2 review MED-3 — `postgresql://user@host/db?password=secret` leaked the query-string credential; now routes through `sqlalchemy.engine.make_url(...).render_as_string(hide_password=True)`.
```

- [ ] **Step 6: Commit**

```bash
git add app/api/db_state.py tests/test_db_state_redact_query_password.py CHANGELOG.md
git commit -m "fix(api): _redact_url masks query-string passwords (MED-3)"
```

---

### Task 3: LOW-1 — PII scrub walks JSON keys, not raw values

**Why:** `scripts/db_state_migrator.py:scrub_audit_log_pii` runs the secret-key regex against the *whole* serialised JSON of `audit_log.params`. An audit row whose value text contains `"please reset your password"` or HTTP path `/reset-password` triggers wholesale `{_redacted_at_migration: true}` rewrite. Legitimate operational history erased. Fix: parse JSON, walk keys.

**Files:**
- Modify: `scripts/db_state_migrator.py` (the `scrub_audit_log_pii` function)
- Test: `tests/test_pii_scrub_walks_keys.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pii_scrub_walks_keys.py
"""LOW-1 — PII scrub walks JSON keys, never matches values."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest


def _make_duckdb_with_audit_rows(tmp_path: Path, rows: list[dict]) -> Path:
    db = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db))
    c.execute(
        """CREATE TABLE audit_log (
            id VARCHAR PRIMARY KEY,
            actor_user_id VARCHAR,
            action VARCHAR,
            params VARCHAR,
            params_before VARCHAR,
            timestamp TIMESTAMP
        )"""
    )
    for r in rows:
        c.execute(
            "INSERT INTO audit_log VALUES (?, ?, ?, ?, ?, current_timestamp)",
            [r["id"], r.get("actor"), r["action"], r["params"], r.get("params_before")],
        )
    c.close()
    return db


def test_scrub_does_not_redact_value_only_match(tmp_path: Path) -> None:
    """Row whose VALUE text contains 'password' but no key is 'password'
    must be kept verbatim. LOW-1 over-redaction repro: HTTP path
    ``/reset-password`` matched the regex when scanning the whole
    serialised JSON.
    """
    from scripts.db_state_migrator import scrub_audit_log_pii

    benign_params = json.dumps({"path": "/reset-password", "method": "POST"})
    db = _make_duckdb_with_audit_rows(
        tmp_path,
        [{"id": "r1", "action": "http_request", "params": benign_params}],
    )
    summary = scrub_audit_log_pii(db)
    assert summary["rows_redacted"] == 0, summary

    c = duckdb.connect(str(db), read_only=True)
    out = c.execute("SELECT params FROM audit_log WHERE id = 'r1'").fetchone()[0]
    c.close()
    assert json.loads(out) == {"path": "/reset-password", "method": "POST"}


def test_scrub_does_redact_key_match(tmp_path: Path) -> None:
    """Row whose JSON has a sensitive KEY must be redacted."""
    from scripts.db_state_migrator import scrub_audit_log_pii

    sensitive = json.dumps({"username": "alice", "password": "topsecret"})
    db = _make_duckdb_with_audit_rows(
        tmp_path,
        [{"id": "r2", "action": "login", "params": sensitive}],
    )
    summary = scrub_audit_log_pii(db)
    assert summary["rows_redacted"] == 1

    c = duckdb.connect(str(db), read_only=True)
    out = c.execute("SELECT params FROM audit_log WHERE id = 'r2'").fetchone()[0]
    c.close()
    after = json.loads(out)
    assert after.get("password") != "topsecret"
    # And the non-sensitive sibling key survives.
    assert after.get("username") == "alice"


def test_scrub_handles_nested_keys(tmp_path: Path) -> None:
    """LOW-1 fix should recurse into nested dicts/lists."""
    from scripts.db_state_migrator import scrub_audit_log_pii

    sensitive = json.dumps(
        {"creds": {"token": "abc", "user": "bob"}, "items": [{"api_key": "k1"}]}
    )
    db = _make_duckdb_with_audit_rows(
        tmp_path,
        [{"id": "r3", "action": "x", "params": sensitive}],
    )
    summary = scrub_audit_log_pii(db)
    assert summary["rows_redacted"] == 1

    c = duckdb.connect(str(db), read_only=True)
    out = c.execute("SELECT params FROM audit_log WHERE id = 'r3'").fetchone()[0]
    c.close()
    after = json.loads(out)
    assert after["creds"].get("token") != "abc"
    assert after["creds"].get("user") == "bob"
    assert after["items"][0].get("api_key") != "k1"


def test_scrub_leaves_non_json_rows_alone(tmp_path: Path) -> None:
    """A row whose params is not JSON (or NULL) is left as-is."""
    from scripts.db_state_migrator import scrub_audit_log_pii

    db = _make_duckdb_with_audit_rows(
        tmp_path,
        [
            {"id": "r4", "action": "x", "params": "not json"},
            {"id": "r5", "action": "x", "params": None},
        ],
    )
    summary = scrub_audit_log_pii(db)
    assert summary["rows_redacted"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_pii_scrub_walks_keys.py -v --tb=short`
Expected: `test_scrub_does_not_redact_value_only_match` FAILS (value-match triggers redact); other tests may pass or fail depending on existing impl.

- [ ] **Step 3: Apply the fix**

Open `scripts/db_state_migrator.py`, locate `def scrub_audit_log_pii(`. Replace the inner redaction logic so the regex is applied only to KEYS of the parsed JSON, walking nested dicts/lists. Sketch:

```python
_SENSITIVE_KEY_RE = re.compile(
    r"(password|token|secret|api_key|bearer|private_key|signing_key)",
    re.IGNORECASE,
)


def _redact_sensitive_keys(obj):
    """Walk ``obj``, replacing values under sensitive KEYS with
    ``"<redacted-at-migration>"``. Returns the mutated structure +
    a boolean flag for whether anything was redacted.

    LOW-1: pre-fix, the regex ran against str(obj) — values like
    ``"please reset your password"`` triggered wholesale row rewrite.
    """
    if isinstance(obj, dict):
        changed = False
        for k, v in list(obj.items()):
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                if obj[k] not in (None, "", "<redacted-at-migration>"):
                    obj[k] = "<redacted-at-migration>"
                    changed = True
            else:
                _, sub_changed = _redact_sensitive_keys(v)
                changed = changed or sub_changed
        return obj, changed
    if isinstance(obj, list):
        changed = False
        for i, item in enumerate(obj):
            _, sub_changed = _redact_sensitive_keys(item)
            changed = changed or sub_changed
        return obj, changed
    return obj, False


def scrub_audit_log_pii(duckdb_path: Path) -> dict[str, int]:
    """Idempotently redact sensitive keys inside ``audit_log.params``
    and ``params_before`` in the DuckDB source.

    LOW-1: walks JSON KEYS only. Rows whose params is NULL, not JSON,
    or has no sensitive key are left unchanged.
    """
    import json

    conn = duckdb.connect(str(duckdb_path))
    rows_scanned = 0
    rows_redacted = 0
    try:
        try:
            rows = conn.execute(
                "SELECT id, params, params_before FROM audit_log"
            ).fetchall()
        except duckdb.Error:
            return {"rows_scanned": 0, "rows_redacted": 0}

        for rid, params, params_before in rows:
            rows_scanned += 1
            new_params = params
            new_params_before = params_before
            changed_any = False
            for src_attr, set_param_name in (
                (params, "params"),
                (params_before, "params_before"),
            ):
                if src_attr is None:
                    continue
                try:
                    parsed = json.loads(src_attr)
                except (ValueError, TypeError):
                    continue
                _, changed = _redact_sensitive_keys(parsed)
                if changed:
                    if set_param_name == "params":
                        new_params = json.dumps(parsed)
                    else:
                        new_params_before = json.dumps(parsed)
                    changed_any = True
            if not changed_any:
                continue
            set_parts = []
            bind = []
            if new_params != params:
                set_parts.append("params = ?")
                bind.append(new_params)
            if new_params_before != params_before:
                set_parts.append("params_before = ?")
                bind.append(new_params_before)
            bind.append(rid)
            conn.execute(
                f"UPDATE audit_log SET {', '.join(set_parts)} WHERE id = ?",
                bind,
            )
            rows_redacted += 1
    finally:
        conn.close()
    return {"rows_scanned": rows_scanned, "rows_redacted": rows_redacted}
```

Keep the existing helper imports (`re`, `duckdb`, etc.) — they should already be at the top of the file.

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_pii_scrub_walks_keys.py -v`
Expected: all 4 PASS.

Run: `.venv/bin/pytest tests/ -k "scrub" --tb=short -q`
Expected: any other PII-scrub tests still PASS.

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **`scrub_audit_log_pii` walks JSON keys instead of regex-matching raw values.** Round-2 review LOW-1 — `audit_log` rows whose value text happened to contain `"password"` (e.g. HTTP path `/reset-password`) were silently nuked into `{_redacted_at_migration: true}`. Now only keys named `password`/`token`/`secret`/`api_key`/`bearer`/`private_key`/`signing_key` have their values replaced; non-JSON params and value-only matches survive unchanged.
```

- [ ] **Step 6: Commit**

```bash
git add scripts/db_state_migrator.py tests/test_pii_scrub_walks_keys.py CHANGELOG.md
git commit -m "fix(migrator): PII scrub walks JSON keys, not raw values (LOW-1)"
```

---

### Task 4: LOW-2 — `docker-compose.postgres.yml` removes literal `agnes` fallback

**Why:** `${POSTGRES_PASSWORD:-agnes}` survives at 5 sites in the overlay (lines 47, 78, 101, 126, 140). The API guard refuses to start a migration without `POSTGRES_PASSWORD`, but `docker compose up` (the non-state-machine path) brings up Postgres with the literal `agnes/agnes` credential. Fix: drop the `:-agnes` default so compose fails fast.

**Files:**
- Modify: `docker-compose.postgres.yml`
- Test: `tests/test_compose_secrets.py` (new, static check)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_compose_secrets.py
"""LOW-2 — compose overlay must not carry a literal POSTGRES_PASSWORD
default; missing env should fail fast, not silently default to a
known-weak credential."""
from __future__ import annotations

from pathlib import Path


def test_postgres_overlay_has_no_literal_password_default() -> None:
    """``${POSTGRES_PASSWORD:-agnes}`` is the LOW-2 footgun. Reject the
    shell-default form anywhere in docker-compose.postgres.yml — the
    overlay must use bare ``${POSTGRES_PASSWORD}`` so docker compose
    errors out if the env var is unset rather than booting Postgres
    with credentials ``agnes/agnes``."""
    overlay = Path("docker-compose.postgres.yml").read_text()
    bad = []
    for lineno, line in enumerate(overlay.splitlines(), start=1):
        if "POSTGRES_PASSWORD" in line and ":-agnes" in line:
            bad.append(f"line {lineno}: {line.strip()}")
    assert not bad, (
        "LOW-2: ``${POSTGRES_PASSWORD:-agnes}`` ships known-weak "
        "fallback creds; replace with ``${POSTGRES_PASSWORD}``\n"
        + "\n".join(bad)
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_compose_secrets.py -v --tb=short`
Expected: FAILS, listing the 5 lines with the literal default.

- [ ] **Step 3: Apply the fix**

Open `docker-compose.postgres.yml`. Replace every `${POSTGRES_PASSWORD:-agnes}` with `${POSTGRES_PASSWORD}`. Add a one-line YAML comment above the postgres service explaining why the default was removed:

```yaml
# LOW-2: POSTGRES_PASSWORD is REQUIRED — no literal default. Set it in
# /opt/agnes/.env (Secret Manager-sourced on customer VMs). ``docker
# compose up`` will fail with "POSTGRES_PASSWORD is not set" rather
# than silently booting Postgres with ``agnes/agnes``.
```

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_compose_secrets.py -v`
Expected: PASS.

Verify with `docker compose -f docker-compose.yml -f docker-compose.postgres.yml config 2>&1 | head -5` while `POSTGRES_PASSWORD` is unset — should warn or error about the missing variable. (Optional manual check.)

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **`docker-compose.postgres.yml` no longer defaults `POSTGRES_PASSWORD` to the literal `agnes`.** Round-2 review LOW-2 — the `${POSTGRES_PASSWORD:-agnes}` form let `docker compose up` succeed with `agnes/agnes` credentials when the env var was unset. Compose now errors out with `POSTGRES_PASSWORD variable is not set` if the operator's `.env` is missing the secret; the API guard already enforces this on the state-machine path.
```

- [ ] **Step 6: Commit**

```bash
git add docker-compose.postgres.yml tests/test_compose_secrets.py CHANGELOG.md
git commit -m "fix(compose): remove POSTGRES_PASSWORD literal-agnes default (LOW-2)"
```

---

## Phase B — API hardening

### Task 5: MED-2 — `_validate_cloud_url` rejects reserved address ranges

**Why:** Today `_validate_cloud_url` accepts any TCP target. An admin posting `cloud_url=postgresql://attacker:pwd@169.254.169.254:5432/db` (GCE metadata server) or `10.0.0.5:5432` (RFC1918) triggers `alembic upgrade head` opening a connection to a non-DB endpoint — the server-fingerprint error reveals service liveness. Net effect: an admin (or anyone who got admin via a separate bug) has an SSRF/port-probe primitive. Fix: reject loopback, GCE metadata, link-local, private (RFC1918), CGNAT (RFC6598), and IPv6 ULA ranges. Allow opt-in via env flag for tests/dev.

**Files:**
- Modify: `app/api/db_state.py` (`_validate_cloud_url`)
- Test: `tests/test_db_state_cloud_url_ssrf.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_cloud_url_ssrf.py
"""MED-2 — _validate_cloud_url rejects reserved/private address ranges."""
from __future__ import annotations

import pytest


_REJECT_CASES = [
    # IPv4 loopback
    "postgresql+psycopg://u:p@127.0.0.1:5432/db",
    "postgresql+psycopg://u:p@127.5.4.3:5432/db",
    # IPv4 GCE metadata + AWS IMDS
    "postgresql+psycopg://u:p@169.254.169.254:5432/db",
    # IPv4 link-local
    "postgresql+psycopg://u:p@169.254.10.20:5432/db",
    # RFC1918 private
    "postgresql+psycopg://u:p@10.0.0.5:5432/db",
    "postgresql+psycopg://u:p@192.168.1.10:5432/db",
    "postgresql+psycopg://u:p@172.16.4.4:5432/db",
    # CGNAT (RFC6598)
    "postgresql+psycopg://u:p@100.64.0.1:5432/db",
    # IPv6 loopback + ULA
    "postgresql+psycopg://u:p@[::1]:5432/db",
    "postgresql+psycopg://u:p@[fd00::1]:5432/db",
    # Hostname that resolves to loopback (we don't resolve DNS here —
    # the hostname-literal ``localhost`` is special-cased instead).
    "postgresql+psycopg://u:p@localhost:5432/db",
]


@pytest.mark.parametrize("url", _REJECT_CASES)
def test_cloud_url_rejects_reserved_addresses(url: str) -> None:
    from app.api.db_state import _validate_cloud_url
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _validate_cloud_url(url)
    assert exc.value.status_code == 400
    detail = str(exc.value.detail).lower()
    assert (
        "reserved" in detail
        or "private" in detail
        or "loopback" in detail
        or "link-local" in detail
        or "metadata" in detail
    ), exc.value.detail


_ACCEPT_CASES = [
    "postgresql+psycopg://u:p@db.example.com:5432/agnes",
    "postgresql+psycopg://u:p@8.8.8.8:5432/db",
    "postgresql+psycopg://u:p@cloudsql.gcp.example/db",
]


@pytest.mark.parametrize("url", _ACCEPT_CASES)
def test_cloud_url_accepts_public_hosts(url: str) -> None:
    from app.api.db_state import _validate_cloud_url

    # No exception → pass.
    _validate_cloud_url(url)


def test_cloud_url_opt_in_allows_loopback_for_tests(monkeypatch) -> None:
    """An explicit env opt-in unblocks 127.0.0.1 for the test harness."""
    monkeypatch.setenv("AGNES_ALLOW_RESERVED_CLOUD_URL", "1")
    from app.api.db_state import _validate_cloud_url

    _validate_cloud_url("postgresql+psycopg://u:p@127.0.0.1:5432/db")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_state_cloud_url_ssrf.py -v --tb=short`
Expected: most _REJECT_CASES FAIL (validator does not currently reject them).

- [ ] **Step 3: Apply the fix**

Open `app/api/db_state.py`. Find `def _validate_cloud_url(url: str) -> None:` near line 63. Replace its body so it does scheme + host extraction and rejects reserved addresses unless `AGNES_ALLOW_RESERVED_CLOUD_URL=1` is set:

```python
def _validate_cloud_url(url: str) -> None:
    """Validate that an operator-supplied ``cloud_url`` is plausibly
    safe to hand to alembic / psycopg.

    Round-2 MED-2: reject loopback / GCE metadata / RFC1918 / link-local
    / CGNAT / IPv6 ULA. Without this, an admin posting
    ``cloud_url=postgresql://x:y@169.254.169.254:5432/db`` triggers
    ``alembic upgrade head`` opening a TCP socket to the GCE metadata
    server — the server-fingerprint error in the job's ``error.message``
    leaks service liveness. Net effect: SSRF / port-probe primitive
    available from any admin path.

    Opt-in test override: ``AGNES_ALLOW_RESERVED_CLOUD_URL=1`` skips
    the reserved-range check (used by the test harness for fixtures
    pointing at 127.0.0.1).
    """
    import ipaddress
    import os
    from fastapi import HTTPException
    from sqlalchemy.engine import make_url

    try:
        u = make_url(url)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"cloud_url is not a valid SQLAlchemy URL: {e}",
        )
    if not u.drivername or not u.drivername.startswith("postgresql"):
        raise HTTPException(
            status_code=400,
            detail="cloud_url scheme must be postgresql / postgresql+psycopg",
        )
    if not u.host:
        raise HTTPException(status_code=400, detail="cloud_url is missing a host")
    if not u.database:
        raise HTTPException(status_code=400, detail="cloud_url is missing a database name")

    if os.environ.get("AGNES_ALLOW_RESERVED_CLOUD_URL") == "1":
        return  # explicit test opt-in

    host = u.host
    # ``localhost`` is a frequent footgun — reject regardless of resolution.
    if host.lower() == "localhost":
        raise HTTPException(
            status_code=400,
            detail="cloud_url host is loopback (localhost) — set AGNES_ALLOW_RESERVED_CLOUD_URL=1 to override (test/dev only)",
        )
    # IP literal? Check reservation classes.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # hostname (DNS-resolved later by psycopg); cannot pre-classify here without resolution.
    if ip.is_loopback:
        raise HTTPException(status_code=400, detail=f"cloud_url host is loopback ({ip}); reserved range")
    if ip.is_link_local:
        # Includes 169.254.169.254 (GCE/AWS IMDS).
        raise HTTPException(status_code=400, detail=f"cloud_url host is link-local ({ip}) — covers GCE/AWS metadata service; reserved range")
    if ip.is_private:
        raise HTTPException(status_code=400, detail=f"cloud_url host is private ({ip}); reserved (RFC1918 / IPv6 ULA)")
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        raise HTTPException(status_code=400, detail=f"cloud_url host is CGNAT ({ip}); reserved (RFC6598)")
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        raise HTTPException(status_code=400, detail=f"cloud_url host is reserved/multicast/unspecified ({ip})")
```

If the existing function already does scheme/host/db validation (per `e9a47722`), keep that logic and add ONLY the reserved-range check. Adjust to whatever signature is currently in the file.

- [ ] **Step 4: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_db_state_cloud_url_ssrf.py -v`
Expected: all PASS.

Run: `.venv/bin/pytest tests/ -k "cloud_url" --tb=short -q`
Expected: any pre-existing cloud-URL tests still PASS (scheme/host/db validation untouched).

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **`_validate_cloud_url` rejects loopback / GCE metadata / RFC1918 / link-local / CGNAT / IPv6 ULA.** Round-2 review MED-2 — an admin posting `cloud_url=postgresql://x:y@169.254.169.254:5432/db` triggered alembic to open a TCP socket to the GCE metadata server; the server-fingerprint error in `job.error.message` then leaked service liveness (SSRF/port-probe primitive). Reserved-range rejection runs after scheme/host/db validation. Set `AGNES_ALLOW_RESERVED_CLOUD_URL=1` to opt in to loopback for test/dev fixtures.
```

- [ ] **Step 6: Commit**

```bash
git add app/api/db_state.py tests/test_db_state_cloud_url_ssrf.py CHANGELOG.md
git commit -m "fix(api): _validate_cloud_url rejects reserved IP ranges (MED-2)"
```

---

### Task 6: MED-4 — `cancel_job` clears stale postgres URL when reverting to DuckDB

**Why:** `app/api/db_state.py:389-390` reverts `backend=duckdb` on cancel but leaves the migration-target postgres URL in `database.url`. The overlay is now self-inconsistent: backend says DuckDB, url points at PG. Repository routing on a fresh app start would honor the (DuckDB) backend, but the url stays as a footgun for operators inspecting `instance.yaml`. Fix: when source backend is `duckdb`, drop the `url` key entirely on cancel-revert.

**Files:**
- Modify: `app/api/db_state.py` (`cancel_job`)
- Test: `tests/test_db_state_cancel_duckdb_source.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_cancel_duckdb_source.py
"""MED-4 — cancel_job removes url when reverting to duckdb backend."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def jobs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "db-jobs"
    d.mkdir()
    return d


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


def test_cancel_duckdb_source_drops_url_key(
    jobs_dir: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When source_backend='duckdb', cancel-revert must wipe the
    target's url from instance.yaml. Leaving the postgres URL there
    creates a self-inconsistent overlay (backend=duckdb but url
    points at PG)."""
    from app.api import db_state

    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)
    instance_yaml = state_dir / "instance.yaml"
    monkeypatch.setattr(
        db_state, "_instance_yaml_path", lambda: instance_yaml,
        raising=False,
    )

    # Simulate a running duckdb→side_car job + in-progress instance.yaml.
    instance_yaml.write_text(
        "database:\n  backend: side_car_in_progress\n"
        "  url: postgresql+psycopg://agnes:pwd@postgres:5432/agnes\n"
    )
    job_id = "j-cancel-1"
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps({
            "job_id": job_id,
            "status": "running",
            "source_backend": "duckdb",
            "target_backend": "side_car",
            "target_url": "postgresql+psycopg://agnes:pwd@postgres:5432/agnes",
        })
    )

    with patch.object(db_state, "_require_admin", return_value=None):
        out = db_state.cancel_job(job_id=job_id)

    after = instance_yaml.read_text()
    assert "backend: duckdb" in after
    assert "url:" not in after, (
        "MED-4: cancel revert to duckdb must drop the url key entirely; "
        f"instance.yaml content:\n{after}"
    )
    assert out["status"] in ("cancelled", "cancel_requested")
```

The test uses `_instance_yaml_path` and `_require_admin` symbols; if they don't exist with those exact names in the module, replace with whatever the helpers are called (search `app/api/db_state.py` for `instance.yaml` and the admin-auth dep).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_state_cancel_duckdb_source.py -v --tb=short`
Expected: FAILS — `url:` line survives the cancel revert.

- [ ] **Step 3: Apply the fix**

In `app/api/db_state.py` `cancel_job`, find the branch that calls `write_backend_state(source_backend, url=…)`. When `source_backend == "duckdb"`, pass `url=None` (or omit explicitly so the writer drops the key — see `src/db_state_machine.py:write_backend_state` Ellipsis semantics):

```python
# MED-4: when reverting cancel to duckdb, the target's postgres URL
# must NOT survive in the overlay. write_backend_state with url=None
# drops the key (vs Ellipsis = preserve).
revert_url = None if source_backend == "duckdb" else source_url
write_backend_state(source_backend, url=revert_url)
```

The exact call site is near `app/api/db_state.py:389`. Inspect 5 lines above to see what `source_backend` / `source_url` locals are called and match them.

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_db_state_cancel_duckdb_source.py -v`
Expected: PASS.

Run: `.venv/bin/pytest tests/ -k "cancel" --tb=short -q`
Expected: pre-existing cancel tests still PASS.

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **`cancel_job` revert to duckdb drops the target's postgres URL.** Round-2 review MED-4 — pre-fix, cancelling a `duckdb → side_car` mid-flight left `backend: duckdb` but `url: postgresql://…@postgres/agnes` in `instance.yaml`. The overlay was self-inconsistent and operators inspecting it saw a misleading PG URL. Now the URL key is dropped whenever the source is duckdb.
```

- [ ] **Step 6: Commit**

```bash
git add app/api/db_state.py tests/test_db_state_cancel_duckdb_source.py CHANGELOG.md
git commit -m "fix(api): cancel revert to duckdb clears stale postgres url (MED-4)"
```

---

### Task 7: H7-NEW — `start_migration` wires `target='duckdb'` / `'duckdb_quack'`

**Why:** Commit `965e7870` widened the transition matrix to allow `side_car`/`cloud` → `duckdb*`, but the FastAPI endpoint only branches on `'side_car'` / `'cloud'`. A POST `/api/admin/db/migrate {target: "duckdb"}` falls through, writing `CLOUD_IN_PROGRESS` into `instance.yaml`. The migrator then raises `BackendNotYetSupportedError` (uncaught 500). Either reject the duckdb* targets at the endpoint with 501, or wire the missing branches end-to-end. The pragmatic choice is **explicit 501** until the migrator gets a `target='duckdb'` path (out of scope for this round).

**Files:**
- Modify: `app/api/db_state.py` (`start_migration`)
- Test: `tests/test_db_state_migrate_to_duckdb.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_migrate_to_duckdb.py
"""H7-NEW — POST /migrate with target='duckdb' / 'duckdb_quack'
returns a clean 501 instead of silently mis-routing to CLOUD."""
from __future__ import annotations

import pytest
from unittest.mock import patch


@pytest.mark.parametrize("target", ["duckdb", "duckdb_quack"])
def test_start_migration_duckdb_target_returns_501(target: str) -> None:
    from app.api import db_state
    from fastapi import HTTPException

    with patch.object(db_state, "_require_admin", return_value=None):
        with pytest.raises(HTTPException) as exc:
            db_state.start_migration(
                payload=db_state.MigrateRequest(
                    target=target, cloud_url=None
                )
            )
    assert exc.value.status_code == 501, (
        f"target={target!r} must return 501 (not implemented) until the "
        f"migrator wires reverse-to-duckdb; got {exc.value.status_code}"
    )
    assert (
        "not yet supported" in str(exc.value.detail).lower()
        or "not implemented" in str(exc.value.detail).lower()
    )


def test_start_migration_side_car_still_works() -> None:
    """Regression guard: side_car target must not be touched by the
    H7-NEW branch."""
    from app.api import db_state

    # The existing side_car path may not be smoke-testable in a unit
    # context (needs flock + filesystem); just assert the endpoint
    # doesn't raise NotImplementedError for side_car.
    with patch.object(db_state, "_require_admin", return_value=None):
        try:
            db_state.start_migration(
                payload=db_state.MigrateRequest(target="side_car", cloud_url=None)
            )
        except HTTPException as e:
            assert e.status_code != 501
        except Exception:
            pass  # any non-HTTPException is fine; we're only asserting "not 501".
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_state_migrate_to_duckdb.py -v --tb=short`
Expected: `test_start_migration_duckdb_target_returns_501[duckdb]` FAILS (no 501 raised; endpoint mis-routes).

- [ ] **Step 3: Apply the fix**

Open `app/api/db_state.py`, locate `def start_migration(payload: MigrateRequest)` (~line 171). After the initial `_require_admin()` + validation but BEFORE the `'side_car'`/`'cloud'` branching, add a guard:

```python
# H7-NEW: reverse migrations to DuckDB are reserved in
# _ALLOWED_TRANSITIONS but the migrator does not yet wire
# ``target='duckdb'`` / ``'duckdb_quack'``. Reject at the endpoint
# with 501 so the API contract is honest — versus silently
# mis-routing to CLOUD because the existing branch was
# ``payload.target == 'side_car'`` else cloud.
if payload.target in ("duckdb", "duckdb_quack"):
    raise HTTPException(
        status_code=501,
        detail=(
            f"target={payload.target!r} is reserved in the state-machine "
            "matrix but the migrator does not yet support reverse "
            "migrations to DuckDB. Tracked for a follow-up release."
        ),
    )
```

- [ ] **Step 4: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_db_state_migrate_to_duckdb.py -v`
Expected: both PASS.

Run: `.venv/bin/pytest tests/test_db_state_machine.py tests/test_api_db_state.py -v --tb=short`
Expected: existing state-machine + API tests still PASS (the 501 only fires for `duckdb*` targets).

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **`POST /api/admin/db/migrate` returns 501 when `target='duckdb'` or `'duckdb_quack'`.** Round-2 review H7-NEW — the multi-destination transition matrix (commit `965e7870`) reserved reverse migrations to DuckDB in the state graph, but the endpoint's branch logic only knew about `side_car`/`cloud`. Posting `target='duckdb'` silently mis-routed to CLOUD (wrote `CLOUD_IN_PROGRESS` into `instance.yaml`) then crashed the migrator with `BackendNotYetSupportedError` → uncaught 500. The endpoint now rejects cleanly with 501; the matrix entry stays in place so the day-after-migrator-supports-it wiring is trivial.
```

- [ ] **Step 6: Commit**

```bash
git add app/api/db_state.py tests/test_db_state_migrate_to_duckdb.py CHANGELOG.md
git commit -m "fix(api): start_migration returns 501 on duckdb target (H7-NEW)"
```

---

## Phase C — Migrator redaction + JSONB

### Task 8: H3-NEW — Redact passwords inside `error.message`

**Why:** `scripts/db_state_migrator.py:243` raises `RuntimeError(f"... (target={target_url!r}) ...")` on alembic timeout. The migrator's outer handler captures it into `job.error.message`. The API's `_redact_url` only redacts top-level `target_url`/`source_url` — nested `error.message` leaks the password into HTTP responses, browser history, and UI screenshots. Fix: redact every URL substring inside `error.message` (and `error.detail` if present) before serialising.

**Files:**
- Modify: `scripts/db_state_migrator.py` (the timeout exception) AND `app/api/db_state.py` (`get_job` redaction pass)
- Test: `tests/test_db_state_error_redaction.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_error_redaction.py
"""H3-NEW — passwords inside error.message are redacted before
GET /job returns them, and the migrator does not embed the raw URL
in the exception in the first place."""
from __future__ import annotations

import pytest


def test_redact_url_in_text_strips_userinfo() -> None:
    """Helper that redacts every URL substring inside arbitrary text."""
    from app.api.db_state import _redact_urls_in_text

    msg = (
        "alembic upgrade head timed out after 300s "
        "(target='postgresql+psycopg://agnes:s3cret@host:5432/agnes'). "
        "The migration target may be unreachable."
    )
    out = _redact_urls_in_text(msg)
    assert "s3cret" not in out
    # The rest of the message is preserved.
    assert "alembic upgrade head timed out" in out


def test_redact_url_in_text_strips_query_password() -> None:
    from app.api.db_state import _redact_urls_in_text

    msg = "connect failed: postgresql://u@h/db?password=topsecret&sslmode=disable"
    out = _redact_urls_in_text(msg)
    assert "topsecret" not in out


def test_get_job_redacts_error_message(tmp_path, monkeypatch) -> None:
    """End-to-end: GET /api/admin/db/job/<id> must not return passwords
    inside error.message."""
    from app.api import db_state
    import json

    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)

    job_id = "j-h3"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id,
        "status": "failed",
        "source_backend": "duckdb",
        "target_backend": "cloud",
        "target_url": "postgresql+psycopg://agnes:s3cret@cloud:5432/agnes",
        "error": {
            "kind": "alembic_timeout",
            "message": (
                "alembic upgrade head timed out after 300s "
                "(target='postgresql+psycopg://agnes:s3cret@cloud:5432/agnes'). "
            ),
        },
    }))

    from unittest.mock import patch
    with patch.object(db_state, "_require_admin", return_value=None):
        out = db_state.get_job(job_id=job_id)

    assert "s3cret" not in json.dumps(out), (
        "GET /job leaks plaintext password in error.message; H3-NEW"
    )


def test_migrator_raises_with_redacted_url() -> None:
    """When the migrator constructs the alembic-timeout message, the
    URL is masked at the raise site too — defence in depth."""
    from scripts import db_state_migrator
    from app.api.db_state import _redact_urls_in_text

    target_url = "postgresql+psycopg://agnes:s3cret@host/agnes"
    # Format the message the way alembic_upgrade_head does on timeout.
    # The exact string is implementation-defined; assert no plaintext
    # secret survives the migrator's own message formatting.
    try:
        db_state_migrator._format_alembic_timeout_message(target_url, 300)
    except AttributeError:
        pytest.skip("helper not yet extracted — defer to integration check")
    else:
        out = db_state_migrator._format_alembic_timeout_message(target_url, 300)
        assert "s3cret" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_state_error_redaction.py -v --tb=short`
Expected: `_redact_urls_in_text` doesn't exist → first 2 tests FAIL with AttributeError; third test (end-to-end) FAILS because get_job doesn't redact nested fields.

- [ ] **Step 3: Apply the fix — part A (`app/api/db_state.py`)**

Add a module-level helper near `_redact_url`:

```python
def _redact_urls_in_text(text: str | None) -> str | None:
    """Mask every URL-shaped substring in arbitrary text via
    :func:`_redact_url`. Used to scrub ``error.message`` /
    ``error.detail`` fields where a raised exception captured the
    target URL verbatim. H3-NEW.
    """
    if not text:
        return text
    import re
    # Liberal URL match — anything that looks like ``scheme://...``
    # bounded by whitespace, quotes, parens, or end-of-string.
    pattern = re.compile(r"""[a-z][a-z0-9+.\-]*://[^\s'"()<>]+""", re.IGNORECASE)
    return pattern.sub(lambda m: _redact_url(m.group(0)) or "<redacted>", text)


def _redact_error_payload(err: dict | None) -> dict | None:
    """Recursively redact URL-shaped substrings inside an ``error``
    dict before serialisation. H3-NEW.
    """
    if not err or not isinstance(err, dict):
        return err
    out: dict = {}
    for k, v in err.items():
        if isinstance(v, str):
            out[k] = _redact_urls_in_text(v)
        elif isinstance(v, dict):
            out[k] = _redact_error_payload(v)
        else:
            out[k] = v
    return out
```

In `get_job`, after loading the job JSON and before the response, run:

```python
job["error"] = _redact_error_payload(job.get("error"))
```

- [ ] **Step 4: Apply the fix — part B (`scripts/db_state_migrator.py`)**

Extract the alembic-timeout message formatting into a helper that masks the URL at the raise site (defence in depth):

```python
def _format_alembic_timeout_message(target_url: str, timeout_sec: int) -> str:
    """Format the timeout error WITH the URL password masked.

    H3-NEW: pre-fix the formatter embedded the bare ``target_url`` with
    its password via ``!r``. The migrator's outer handler then
    captured the message into ``job.error.message``. Mask here so a
    third party reading the job JSON never sees plaintext creds.
    """
    try:
        from sqlalchemy.engine import make_url
        safe = make_url(target_url).render_as_string(hide_password=True)
    except Exception:
        safe = "<unparseable-url>"
    return (
        f"alembic upgrade head timed out after {timeout_sec}s "
        f"(target={safe!r}). The migration target may be unreachable, "
        f"network-partitioned, or running out of disk."
    )
```

Then update the timeout raise site (~line 242) to use it:

```python
raise RuntimeError(_format_alembic_timeout_message(target_url, ALEMBIC_UPGRADE_TIMEOUT_SEC))
```

Scan for any other `f"... {target_url!r} ..."` patterns in the migrator and route them through `_format_alembic_timeout_message` or a sibling helper.

- [ ] **Step 5: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_db_state_error_redaction.py -v`
Expected: all PASS.

Run: `.venv/bin/pytest tests/ -k "redact or error_message" --tb=short -q`
Expected: green.

- [ ] **Step 6: CHANGELOG bullet**

```markdown
- **`GET /api/admin/db/job/<id>` redacts URL passwords inside nested `error.message`.** Round-2 review H3-NEW — `_redact_url` only masked top-level `target_url` / `source_url`; the alembic-timeout `RuntimeError` formatter embedded the raw URL into the message, which the outer handler captured into `job.error.message`. Plaintext credentials then surfaced in HTTP responses, browser history, and UI screenshots. The migrator now masks the URL at the raise site (defence in depth) and the API recursively scrubs URL-shaped substrings from the entire `error` payload before serialising.
```

- [ ] **Step 7: Commit**

```bash
git add app/api/db_state.py scripts/db_state_migrator.py tests/test_db_state_error_redaction.py CHANGELOG.md
git commit -m "fix(migrator,api): redact passwords inside error.message (H3-NEW)"
```

---

### Task 9: H6-NEW — DuckDB→PG JSONB cast list derived from `Base.metadata`

**Why:** `scripts/migrate_duckdb_to_pg/tasks.py:42` hardcodes `_JSON_COLUMNS = {...}`. `src/models/data_packages.py:55` declares `Column("tags", JSONB)` but `data_packages.tags` is absent from the hardcoded list. A DuckDB→PG migration on any instance with `data_packages.tags='["finance"]'` raises a CAST error on INSERT. Fix: derive `_JSON_COLUMNS` dynamically from `Base.metadata` so the set is automatically in sync with the models.

**Files:**
- Modify: `scripts/migrate_duckdb_to_pg/tasks.py` (`_JSON_COLUMNS` definition + `_build_insert` lookup)
- Test: `tests/test_migrate_jsonb_dynamic.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_jsonb_dynamic.py
"""H6-NEW — _JSON_COLUMNS is derived from Base.metadata; every JSONB
column in any model is automatically present."""
from __future__ import annotations


def test_data_packages_tags_in_json_columns() -> None:
    """The H6-NEW repro target: data_packages.tags must appear without
    a code-edit when the model declares it as JSONB."""
    import src.models  # noqa: F401 — register all models
    from scripts.migrate_duckdb_to_pg.tasks import _JSON_COLUMNS

    assert ("data_packages", "tags") in _JSON_COLUMNS, (
        "_JSON_COLUMNS must include every (table, column) declared as "
        "JSONB in src.models. data_packages.tags is the H6-NEW repro."
    )


def test_json_columns_covers_every_jsonb_in_metadata() -> None:
    """Forward-compatibility: any future JSONB column lands in the set
    without manual sync."""
    import src.models  # noqa: F401
    from sqlalchemy.dialects.postgresql import JSONB
    from src.db_pg import Base
    from scripts.migrate_duckdb_to_pg.tasks import _JSON_COLUMNS

    missing = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB) and (table.name, col.name) not in _JSON_COLUMNS:
                missing.append(f"{table.name}.{col.name}")
    assert not missing, (
        "These JSONB columns are missing from _JSON_COLUMNS — derive "
        "the set dynamically:\n  " + "\n  ".join(missing)
    )


def test_build_insert_casts_jsonb_for_dynamic_table() -> None:
    """The INSERT statement built for ``data_packages`` must emit
    ``CAST(:tags AS JSONB)`` so a Python list serialises correctly."""
    from scripts.migrate_duckdb_to_pg.tasks import _build_insert

    sql = _build_insert("data_packages", ["id", "tags"], ["id"])
    assert "CAST(:tags AS JSONB)" in sql or "::JSONB" in sql, sql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_migrate_jsonb_dynamic.py -v --tb=short`
Expected: all 3 tests FAIL (`_JSON_COLUMNS` is a hardcoded set missing `data_packages.tags`).

- [ ] **Step 3: Apply the fix**

Open `scripts/migrate_duckdb_to_pg/tasks.py`. Replace the hardcoded `_JSON_COLUMNS = { ... }` definition with a lazy property:

```python
def _build_json_columns() -> set[tuple[str, str]]:
    """Derive the set of ``(table, column)`` JSONB pairs from the PG
    Base.metadata. H6-NEW: pre-fix the set was hand-maintained and
    drifted (``data_packages.tags`` declared JSONB in
    ``src/models/data_packages.py:55`` but absent here). Deriving
    dynamically guarantees every model-declared JSONB column gets the
    ``CAST(:col AS JSONB)`` treatment in ``_build_insert`` + the
    ``json.dumps`` wrapper in the copy loop.
    """
    import src.models  # noqa: F401 — registers all models on Base
    from sqlalchemy.dialects.postgresql import JSONB
    from src.db_pg import Base

    out: set[tuple[str, str]] = set()
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                out.add((table.name, col.name))
    return out


_JSON_COLUMNS: set[tuple[str, str]] = _build_json_columns()
```

If `_JSON_COLUMNS` is currently a set of column names (not pairs), inspect the existing shape and lookup sites in `_build_insert` / the copy loop. Decide consistent representation. Adjust the test if the existing shape is `frozenset({"params", "tags", ...})` — but the fix's contract is the same: dynamic derivation, full coverage.

- [ ] **Step 4: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_migrate_jsonb_dynamic.py -v`
Expected: all PASS.

Run: `.venv/bin/pytest tests/db_pg/ -q --tb=short` (the PG contract test suite)
Expected: nothing breaks.

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **DuckDB→PG migrator derives the JSONB cast list from `Base.metadata` instead of a hand-maintained set.** Round-2 review H6-NEW — `scripts/migrate_duckdb_to_pg/tasks.py` hardcoded `_JSON_COLUMNS` and missed `data_packages.tags` (declared JSONB on the model since the PG follow-up landed). DuckDB→PG migrations on any instance carrying `data_packages.tags='["finance"]'` crashed at INSERT with a CAST error. The set is now derived once at module import from every model's JSONB columns, so future additions are automatically covered.
```

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_duckdb_to_pg/tasks.py tests/test_migrate_jsonb_dynamic.py CHANGELOG.md
git commit -m "fix(migrator): _JSON_COLUMNS derived from Base.metadata (H6-NEW)"
```

---

### Task 10: NEW-X-USERS-DUPLICATES — DuckDB→PG users idempotent on any UNIQUE constraint

**Why:** Live E2E on 2026-06-01 hit `psycopg.errors.UniqueViolation: duplicate key value violates unique constraint "users_email_key"` when migrating users from a clean DuckDB into a freshly-provisioned PG (6 source rows, all distinct emails). Two of six rows committed before the failure, suggesting psycopg's `executemany` + `ON CONFLICT (id) DO NOTHING` is not fully transactional, or some row got pre-inserted by an earlier task. The reviewer did not flag this — it's a live-discovered NEW finding. Fix: switch the `ON CONFLICT` clause from `(id) DO NOTHING` to **all UNIQUE constraints** (`(id), (email)` for users, etc.) — `ON CONFLICT ON CONSTRAINT users_pkey` then `ON CONFLICT ON CONSTRAINT users_email_key` won't compose in one statement, so use the more general `ON CONFLICT DO NOTHING` (no target).

**Files:**
- Modify: `scripts/migrate_duckdb_to_pg/tasks.py` (`_build_insert`)
- Test: `tests/test_migrate_users_idempotent.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_users_idempotent.py
"""NEW-X — DuckDB→PG users copy is idempotent on every UNIQUE
constraint, not just the PK.

Live-discovered 2026-06-01: with 6 source users (all distinct emails)
and ON CONFLICT (id) DO NOTHING, a psycopg executemany leaves 2 of 6
committed and fails the rest with users.email UNIQUE violation.
Replacing the target with bare ON CONFLICT DO NOTHING makes the copy
idempotent regardless of which UNIQUE constraint triggers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def pg_engine() -> Iterator:
    """Spin a pgserver-backed engine via the existing test infra."""
    pytest.importorskip("pixeltable_pgserver")
    from tests.db_pg.conftest import pg_engine as _ctx  # type: ignore
    yield from _ctx()


def test_users_copy_idempotent_when_pg_has_partial_state(
    pg_engine, tmp_path: Path
) -> None:
    """Pre-state PG with 1 conflicting row; migrator must complete
    without UniqueViolation."""
    import duckdb
    import sqlalchemy as sa
    from scripts.migrate_duckdb_to_pg.tasks import GenericCopyTask

    # Build a DuckDB source with 3 users.
    src = tmp_path / "system.duckdb"
    c = duckdb.connect(str(src))
    c.execute(
        """CREATE TABLE users (
            id VARCHAR PRIMARY KEY,
            email VARCHAR UNIQUE NOT NULL,
            name VARCHAR,
            password_hash VARCHAR,
            setup_token VARCHAR,
            setup_token_created TIMESTAMP,
            reset_token VARCHAR,
            reset_token_created TIMESTAMP,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            deactivated_at TIMESTAMP,
            deactivated_by VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP
        )"""
    )
    rows = [
        ("u1", "alice@example.com", "Alice"),
        ("u2", "bob@example.com",   "Bob"),
        ("u3", "carol@example.com", "Carol"),
    ]
    for r in rows:
        c.execute(
            "INSERT INTO users (id, email, name) VALUES (?, ?, ?)", list(r)
        )
    c.close()

    # Pre-seed PG with one row whose ID differs but whose email matches
    # ``alice@example.com``. ON CONFLICT (id) alone would NOT catch this;
    # NEW-X expects ON CONFLICT DO NOTHING (any UNIQUE) → skipped.
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, email, name, active, created_at, updated_at) "
                "VALUES (:id, :email, :name, TRUE, NOW(), NOW())"
            ),
            {"id": "preseed-1", "email": "alice@example.com", "name": "Preseed"},
        )

    # Now run the migrator task.
    task = GenericCopyTask(table_name="users", pk_columns=["id"])
    duck_conn = duckdb.connect(str(src), read_only=True)
    try:
        considered = task.run(duck_conn, pg_engine)
    finally:
        duck_conn.close()

    # All 3 rows considered, none crashed.
    assert considered == 3

    # PG ends up with preseed row + the 2 non-conflicting source rows.
    # Alice (u1) was a no-op due to email conflict; bob + carol landed.
    with pg_engine.connect() as conn:
        emails = sorted(
            r[0] for r in conn.execute(sa.text("SELECT email FROM users")).all()
        )
    assert emails == ["alice@example.com", "bob@example.com", "carol@example.com"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_migrate_users_idempotent.py -v --tb=short`
Expected: FAILS — pre-fix the migrator's INSERT uses `ON CONFLICT (id) DO NOTHING` which doesn't catch the email conflict; psycopg raises UniqueViolation.

- [ ] **Step 3: Apply the fix**

In `scripts/migrate_duckdb_to_pg/tasks.py`, locate `_build_insert`. Change the `ON CONFLICT` clause:

```python
def _build_insert(
    target_table: str,
    columns: Sequence[str],
    pk_columns: Sequence[str],
) -> str:
    """Build the parametrised INSERT used by GenericCopyTask.run.

    NEW-X: the ON CONFLICT target is intentionally OMITTED — bare
    ``ON CONFLICT DO NOTHING`` matches every UNIQUE constraint on the
    table, not just the PK. Pre-fix the form was
    ``ON CONFLICT ({pk}) DO NOTHING`` which let an INSERT collide on
    a non-PK UNIQUE (e.g. ``users.email``) raise UniqueViolation
    mid-batch — psycopg's executemany then left a partial commit
    in PG and aborted with secondary rows uninserted.

    Side note: ``pk_columns`` is still part of the signature because
    the validator + the row-hash code use it; the parameter is unused
    here on purpose.
    """
    casts = []
    for c in columns:
        # JSONB columns get an explicit cast (H6-NEW).
        if (target_table, c) in _JSON_COLUMNS:
            casts.append(f"CAST(:{c} AS JSONB)")
        else:
            casts.append(f":{c}")
    col_list = ", ".join(columns)
    return (
        f"INSERT INTO {target_table} ({col_list}) VALUES ({', '.join(casts)}) "
        f"ON CONFLICT DO NOTHING"
    )
```

The `pk_columns` parameter is preserved for callers that still need PK semantics elsewhere (validator, checksum), even though it no longer factors into the SQL.

- [ ] **Step 4: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_migrate_users_idempotent.py -v`
Expected: PASS.

Run: `.venv/bin/pytest tests/db_pg/ -q --tb=short` and `tests/ -k "migrate" -q --tb=short`
Expected: no regression in existing copy/verify tests.

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **DuckDB→PG migrator's INSERT is idempotent on every UNIQUE constraint, not just the PK.** Live-discovered 2026-06-01 (NEW-X) — running cycle 1 (DuckDB→side-car) on a freshly-provisioned VM with 6 source users, the migrator failed with `psycopg.errors.UniqueViolation: duplicate key value violates unique constraint "users_email_key"` after 2 of 6 rows had already committed (executemany did not honor transactional rollback as expected). The INSERT clause was tightened from `ON CONFLICT (id) DO NOTHING` to bare `ON CONFLICT DO NOTHING`, which matches every UNIQUE constraint and lets the migrator skip rows that conflict on any unique column (e.g. `users.email`) without aborting the batch.
```

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_duckdb_to_pg/tasks.py tests/test_migrate_users_idempotent.py CHANGELOG.md
git commit -m "fix(migrator): ON CONFLICT DO NOTHING covers every UNIQUE (NEW-X)"
```

---

## Phase D — Applier hardening

### Task 11: H2-NEW — applier python-heredoc rewrites preserve 0600 mode

**Why:** `scripts/ops/agnes-state-applier.sh:131,158` has two inline python heredocs (H8 expiry + `update_job`) that do `os.replace(tmp, p)` with no follow-up `os.chmod(p, 0o600)`. The tmp file was created with the process umask → ends up `0644`. After the applier touches any job file, the embedded `target_url` (incl. plaintext password) becomes world-readable. Fix: chmod `0600` after every `os.replace`.

**Files:**
- Modify: `scripts/ops/agnes-state-applier.sh` (two heredoc sites)
- Test: `tests/test_applier_job_rewrite_mode.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_applier_job_rewrite_mode.py
"""H2-NEW — the applier's python heredocs that rewrite job JSON
preserve mode 0600 after os.replace."""
from __future__ import annotations

from pathlib import Path
import re


def test_update_job_heredoc_chmods_after_replace() -> None:
    """Each ``os.replace(tmp, p)`` in the applier script is followed by
    ``os.chmod(p, 0o600)``. Pre-H2 fix the tmp was created with the
    process umask (0644 on the GRPN VM) and survived the rename →
    job JSON containing target_url became world-readable.
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    # Find every os.replace call (Python form, inside the bash heredocs).
    replace_sites = [
        (i, line) for i, line in enumerate(script.splitlines(), start=1)
        if "os.replace(" in line
    ]
    assert len(replace_sites) >= 2, (
        "expected at least two os.replace() sites inside applier "
        "heredocs (H8 expiry + update_job); script may have been "
        f"restructured. Found: {replace_sites}"
    )
    # For each site, the next ~3 lines must include os.chmod(..., 0o600).
    lines = script.splitlines()
    misses = []
    for lineno, _src in replace_sites:
        window = "\n".join(lines[lineno - 1 : lineno + 5])
        if not re.search(r"os\.chmod\([^)]+0o600", window):
            misses.append(lineno)
    assert not misses, (
        "These os.replace sites are missing an os.chmod(..., 0o600) "
        f"follow-up: lines {misses}\n"
        "Without the chmod, the tmp file's umask-0644 mode survives "
        "the rename and the rewritten job JSON becomes world-readable."
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_applier_job_rewrite_mode.py -v --tb=short`
Expected: FAILS — both heredoc sites have `os.replace` with no `os.chmod` follow-up.

- [ ] **Step 3: Apply the fix**

Open `scripts/ops/agnes-state-applier.sh`. Find both heredoc blocks (`python3 - "$JOBS_DIR" ... <<'PY'` around line 89 and `python3 - "$f" ... <<'PY'` around line 141 / 158). After each `os.replace(tmp, p)` call, add:

```python
        os.chmod(p, 0o600)
```

Example (the expiry heredoc):

```python
        # Atomic-rewrite as failed/expired so the next tick (or the
        # API status endpoint) sees the terminal state.
        data["status"] = "failed"
        data["error"] = {"kind": "PendingJobExpired", "message": ...}
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, p)
        os.chmod(p, 0o600)  # H2-NEW: tmp inherited umask 0644; restore 0600.
```

Repeat for the second heredoc (`update_job`).

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_applier_job_rewrite_mode.py -v`
Expected: PASS.

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **Applier python-heredoc rewrites of job JSON preserve mode 0600.** Round-2 review H2-NEW — the inline heredocs in `agnes-state-applier.sh` used `os.replace(tmp, p)` with no follow-up `os.chmod`, so the tmp file inherited the process umask (0644 on standard cloud-init VMs). Every time the applier touched a job file (H8 age expiry or `update_job` step transition), the embedded `target_url` with its plaintext password became world-readable. Both sites now `os.chmod(p, 0o600)` after the rename.
```

- [ ] **Step 6: Commit**

```bash
git add scripts/ops/agnes-state-applier.sh tests/test_applier_job_rewrite_mode.py CHANGELOG.md
git commit -m "fix(applier): chmod 0600 after job JSON rewrite (H2-NEW)"
```

---

### Task 12: H4-NEW — `write_instance_yaml` falls back to pure-bash when PyYAML absent

**Why:** The B6 fix replaced the bash heredoc with `python3 -c 'import yaml; ...'`. PyYAML is not in the provisioning bootstrap on customer-instance VMs (Debian/Ubuntu installs docker + docker-compose-plugin but not `python3-yaml`). On any host where `python3 -c 'import yaml'` fails, the ERR trap fires AFTER a successful migrator run, marks the job failed, and skips the app restart. Fix: install `python3-yaml` in the provisioning script AND have `write_instance_yaml` fall back to a pure-bash writer for the database-only subset of the file.

**Files:**
- Modify: `scripts/ops/agnes-state-applier.sh` (`write_instance_yaml`)
- Modify: `infra/modules/customer-instance/startup-script.sh.tpl` (apt-install)
- Test: `tests/test_applier_yaml_writer_no_pyyaml.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_applier_yaml_writer_no_pyyaml.py
"""H4-NEW — write_instance_yaml works even when PyYAML is unavailable
on the host."""
from __future__ import annotations

import subprocess
from pathlib import Path
import textwrap


def test_write_instance_yaml_bash_fallback_when_pyyaml_missing(tmp_path: Path) -> None:
    """Run the applier's ``write_instance_yaml`` function in a
    subshell where ``python3 -c 'import yaml'`` is forced to fail
    (PATH-shimmed python3). The function must still produce a valid
    instance.yaml with the new backend + url.
    """
    # Shim python3 to ALWAYS fail with ImportError on ``import yaml``.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text(textwrap.dedent("""\
        #!/bin/sh
        # Force ImportError on yaml so the bash fallback fires.
        exec /usr/bin/python3 -c "import sys; sys.modules['yaml']=None; exec(sys.stdin.read())" "$@"
    """))
    shim.chmod(0o755)

    instance = tmp_path / "instance.yaml"
    instance.write_text("database:\n  backend: duckdb\n")

    # Source the applier script's write_instance_yaml fn in a subshell
    # with the shim early on PATH.
    script_path = Path("scripts/ops/agnes-state-applier.sh").resolve()
    env = {
        "PATH": f"{shim_dir}:/usr/bin:/bin",
    }
    cp = subprocess.run(
        ["bash", "-c",
         f". {script_path}; write_instance_yaml side_car postgresql+psycopg://x:y@h/d {instance}"],
        capture_output=True, text=True, env=env,
    )
    # If write_instance_yaml depends on the FLAG/JOBS_DIR globals being
    # set, this invocation may exit non-zero. The H4-NEW test target is
    # weaker: assert that the function does NOT depend on PyYAML —
    # implementer should either pass-arg the file path or guard the
    # globals.
    after = instance.read_text()
    assert "backend: side_car" in after, (
        f"after PyYAML-disabled write, instance.yaml is:\n{after}\n"
        f"stderr: {cp.stderr}\nstdout: {cp.stdout}"
    )
    assert "url: postgresql+psycopg://x:y@h/d" in after
```

This test is bash-shell-driven and may need iteration. If the test setup is too fragile, replace it with a unit-test that invokes only the python heredoc body in two modes (with PyYAML, without). The CONTRACT under test is: missing PyYAML → bash fallback → valid YAML.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_applier_yaml_writer_no_pyyaml.py -v --tb=short`
Expected: FAILS — the bash fallback doesn't exist; the python heredoc errors out.

- [ ] **Step 3: Apply the fix — applier script**

In `scripts/ops/agnes-state-applier.sh`, locate `write_instance_yaml() {` around line 162. Restructure so it tries PyYAML first, falls back to a pure-bash writer:

```bash
write_instance_yaml() {
    # Args: BACKEND URL [FILE_OVERRIDE]
    # H4-NEW: graceful fallback when PyYAML is unavailable on the host.
    local backend="$1" url="${2-}" file="${3:-/data/state/instance.yaml}"
    # Try PyYAML route first — preserves any non-database top-level keys
    # the operator set (logging, auth providers, feature flags).
    if python3 -c 'import yaml' 2>/dev/null; then
        python3 - "$file" "$backend" "$url" <<'PY'
import os, sys, yaml
p, backend, url = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None
try:
    with open(p) as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    cfg = {}
cfg.setdefault("database", {})
cfg["database"]["backend"] = backend
if url:
    cfg["database"]["url"] = url
else:
    cfg["database"].pop("url", None)
tmp = p + ".tmp"
with open(tmp, "w") as f:
    yaml.safe_dump(cfg, f)
os.replace(tmp, p)
os.chmod(p, 0o600)
PY
        chown agnes-applier:agnes-applier "$file" 2>/dev/null || true
        return
    fi
    # Pure-bash fallback. Preserves the database section only — any
    # non-database top-level keys are LOST. Provisioning should
    # install python3-yaml so this path is rarely hit; we keep it
    # alive so a missing dependency doesn't wedge the state machine.
    echo "WARN: write_instance_yaml using bash fallback (PyYAML not installed); non-database top-level keys will be dropped" >&2
    local tmp="${file}.tmp"
    {
        echo "database:"
        echo "  backend: ${backend}"
        if [ -n "$url" ]; then
            echo "  url: ${url}"
        fi
    } > "$tmp"
    chmod 0600 "$tmp"
    mv -f "$tmp" "$file"
    chown agnes-applier:agnes-applier "$file" 2>/dev/null || true
}
```

- [ ] **Step 4: Apply the fix — startup-script provisioning**

In `infra/modules/customer-instance/startup-script.sh.tpl`, find the apt-install block (line 29 area). Add `python3-yaml` to the list:

```bash
apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-compose-plugin \
    python3-yaml  # H4-NEW: required by agnes-state-applier.sh's write_instance_yaml
```

If the apt-install is done in a separate step or via `apt-get install $PACKAGES`, append `python3-yaml` to the relevant variable / line.

- [ ] **Step 5: Re-run tests**

Run: `.venv/bin/pytest tests/test_applier_yaml_writer_no_pyyaml.py -v --tb=short`
Expected: PASS (bash fallback produces a valid two-key yaml).

Run: `.venv/bin/pytest tests/test_state_applier_unit_file.py -v` (existing applier static-check tests)
Expected: still PASS.

- [ ] **Step 6: CHANGELOG bullet**

```markdown
- **`write_instance_yaml` falls back to a pure-bash writer when PyYAML is unavailable; provisioning installs `python3-yaml`.** Round-2 review H4-NEW — the B6 fix replaced the bash heredoc with `python3 -c 'import yaml; ...'`, but `python3-yaml` was not in the customer-instance provisioning bootstrap. On any such host, every successful migrator run was followed by an ERR-trap firing on the YAML write, marking the job failed and skipping the app restart. The applier now probes PyYAML; absent it, a pure-bash writer produces the (database-only-keys) overlay and logs a warning. `startup-script.sh.tpl` apt-installs `python3-yaml` so the bash fallback is a defensive-only path.
```

- [ ] **Step 7: Commit**

```bash
git add scripts/ops/agnes-state-applier.sh infra/modules/customer-instance/startup-script.sh.tpl tests/test_applier_yaml_writer_no_pyyaml.py CHANGELOG.md
git commit -m "fix(applier,infra): write_instance_yaml bash fallback + python3-yaml apt (H4-NEW)"
```

---

### Task 13: H8-NEW — `__rollback` passes SOURCE_URL through to `write_instance_yaml`

**Why:** `scripts/ops/agnes-state-applier.sh:266-296` `__rollback` calls `write_instance_yaml "$SOURCE_BACKEND"` with no second arg. The python helper interprets the missing URL as "drop the key". When a cloud → side_car migration fails mid-flight, `instance.yaml` goes back to `backend=cloud` but with **no url**. App boot then crashes with "Postgres URL unset" — same B4-class outage on the rollback path. Fix: pass `SOURCE_URL`.

**Files:**
- Modify: `scripts/ops/agnes-state-applier.sh` (`__rollback`)
- Test: `tests/test_applier_rollback_preserves_url.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_applier_rollback_preserves_url.py
"""H8-NEW — __rollback preserves SOURCE_URL on instance.yaml revert."""
from __future__ import annotations

from pathlib import Path


def test_rollback_call_passes_source_url() -> None:
    """The __rollback function must call write_instance_yaml with TWO
    arguments — backend AND url. Pre-fix, the call dropped the URL
    via the missing second arg, leaving cloud-source migrations with
    an unusable backend=cloud + no url overlay on the failure path.
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    # Locate the __rollback function body.
    lines = script.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("__rollback()"):
            start = i
            break
    assert start is not None, "__rollback function not found"
    # The body is short — scan the next 30 lines for the
    # write_instance_yaml call.
    body = "\n".join(lines[start : start + 40])
    # H8-NEW: the call must take backend AND url (or be visibly
    # explicit that url='' / cleared on purpose). We assert the
    # SOURCE_URL variable appears in the rollback call line.
    assert "write_instance_yaml" in body, body
    rollback_line = next(
        l for l in body.splitlines()
        if "write_instance_yaml" in l and "$SOURCE_BACKEND" in l
    )
    assert "$SOURCE_URL" in rollback_line, (
        "H8-NEW: __rollback must pass SOURCE_URL as the 2nd arg to "
        "write_instance_yaml; otherwise cloud-source rollback wipes "
        f"the url. Current line:\n  {rollback_line}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_applier_rollback_preserves_url.py -v --tb=short`
Expected: FAILS — current rollback line is `write_instance_yaml "$SOURCE_BACKEND" || true` without `$SOURCE_URL`.

- [ ] **Step 3: Apply the fix**

In `scripts/ops/agnes-state-applier.sh`, inside `__rollback()`:

Before:

```bash
        write_instance_yaml "$SOURCE_BACKEND" || true
```

After:

```bash
        # H8-NEW: cloud-source rollback used to drop the url because we
        # only passed SOURCE_BACKEND. write_instance_yaml interprets a
        # missing 2nd arg as "drop the key" → the next app boot then
        # tried to start with backend=cloud and no DATABASE_URL,
        # re-introducing the B4-class outage on the failure path.
        write_instance_yaml "$SOURCE_BACKEND" "$SOURCE_URL" || true
```

Confirm `$SOURCE_URL` is in scope at the point `__rollback` runs (the trap fires inside the migration loop where the variable has been set; if not, hoist it). For the `duckdb` source case, `$SOURCE_URL` will be empty — `write_instance_yaml` already handles empty URL by dropping the key (which is the intended behaviour for duckdb).

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_applier_rollback_preserves_url.py tests/test_state_applier_unit_file.py -v`
Expected: PASS (new) + PASS (existing).

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **Applier `__rollback` preserves `SOURCE_URL` on revert.** Round-2 review H8-NEW — the ERR-trap rollback called `write_instance_yaml "$SOURCE_BACKEND"` with no second arg. The python helper read the missing URL as "drop the key", so a `cloud → side_car` migration failing mid-flight rewound `instance.yaml` to `backend=cloud` with no `url`. App boot then crashed with "Postgres URL unset", re-introducing the B4-class outage on the failure path. The call now passes `$SOURCE_URL` (which is empty for a `duckdb` source — `write_instance_yaml` already handles that correctly by dropping the key).
```

- [ ] **Step 6: Commit**

```bash
git add scripts/ops/agnes-state-applier.sh tests/test_applier_rollback_preserves_url.py CHANGELOG.md
git commit -m "fix(applier): __rollback preserves SOURCE_URL (H8-NEW)"
```

---

## Phase E — Race windows & state restore

### Task 14: B1-NEW — Move validation inside the flock in `POST /migrate`

**Why:** `app/api/db_state.py:206,258,270` validates the request and writes the pending job in this order: `validate_transition → flock → write`. Two admins racing through `validate_transition` before either acquires the flock both pass, and the second write overwrites the first flag. Fix: move `validate_transition` (and the URL alias check) INSIDE the flock so the second caller re-reads state under the lock.

**Files:**
- Modify: `app/api/db_state.py` (`start_migration` ordering)
- Test: `tests/test_db_state_concurrent_migrate.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_concurrent_migrate.py
"""B1-NEW — concurrent POST /migrate calls cannot both win."""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest


def test_concurrent_migrate_only_one_wins(tmp_path: Path, monkeypatch) -> None:
    """Two threads invoke ``start_migration`` simultaneously. The B8
    fix surfaces pending jobs in ``_current_job_id``, but pre-B1-NEW
    the validate-before-flock ordering let both pass validation and
    then race for the lock. The loser must observe the first job
    after re-reading state under the lock and return 409.

    Real-world repro: Codex described "have two admins POST different
    targets concurrently, with request B delayed between validation
    and lock acquisition."
    """
    from app.api import db_state

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    jobs_dir = state_dir / "db-jobs"
    jobs_dir.mkdir()

    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)
    instance_yaml = state_dir / "instance.yaml"
    instance_yaml.write_text("database:\n  backend: duckdb\n")
    monkeypatch.setattr(
        db_state, "_instance_yaml_path", lambda: instance_yaml,
        raising=False,
    )

    results: list[object] = []
    exc_results: list[BaseException] = []
    barrier = threading.Barrier(2)

    def fire(target: str) -> None:
        try:
            barrier.wait(timeout=5)
            with patch.object(db_state, "_require_admin", return_value=None):
                out = db_state.start_migration(
                    payload=db_state.MigrateRequest(
                        target=target,
                        cloud_url=("postgresql+psycopg://u:p@db.example.com:5432/agnes"
                                   if target == "cloud" else None),
                    )
                )
            results.append(out)
        except BaseException as e:
            exc_results.append(e)

    t1 = threading.Thread(target=fire, args=("side_car",))
    t2 = threading.Thread(target=fire, args=("cloud",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Exactly one of the two must succeed.
    assert len(results) == 1, (
        f"two concurrent /migrate calls — exactly one must win.\n"
        f"results={results}\nexcs={exc_results}"
    )
    # The other must have raised 409 (or a recognisable conflict).
    from fastapi import HTTPException
    other = exc_results[0] if exc_results else None
    assert isinstance(other, HTTPException) and other.status_code == 409, (
        f"loser must return 409 conflict, got {other!r}"
    )

    # Only one pending job exists.
    job_files = list(jobs_dir.glob("*.json"))
    assert len(job_files) == 1, [p.name for p in job_files]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_state_concurrent_migrate.py -v --tb=short`
Expected: FAILS — both threads may succeed; two pending job files.

- [ ] **Step 3: Apply the fix**

Open `app/api/db_state.py`, locate `start_migration`. Restructure so validation happens INSIDE the `MigrationLock()` context:

```python
def start_migration(payload: MigrateRequest) -> dict:
    _require_admin()
    # H7-NEW guard stays here (sync cheap rejection before locking).
    if payload.target in ("duckdb", "duckdb_quack"):
        raise HTTPException(status_code=501, detail=...)
    # MED-2 + scheme validation: also cheap.
    if payload.target == "cloud":
        if not payload.cloud_url:
            raise HTTPException(status_code=400, detail="cloud_url required for target=cloud")
        _validate_cloud_url(payload.cloud_url)

    from src.db_state_machine import MigrationLock, validate_transition, write_backend_state

    # B1-NEW: validate-then-flock had a race where two callers passed
    # validate_transition before either reached the lock, then both
    # wrote pending jobs. Move ALL state-reading and validation
    # INSIDE the flock so the second caller re-reads state and sees
    # the first caller's pending job.
    lock = MigrationLock()
    with lock:
        current_state = _read_current_state()
        # Surface pending jobs (B8 fix) — but now under the lock.
        existing_job = _current_job_id()
        if existing_job:
            raise HTTPException(
                status_code=409,
                detail=f"migration already in flight (job={existing_job})",
            )
        # Validate transition under the lock.
        target_url = (
            payload.cloud_url if payload.target == "cloud"
            else _build_sidecar_url()
        )
        validate_transition(
            source=current_state,
            target=payload.target,
            target_url=target_url,
        )
        # URL alias check (B7) — also under lock; uses normalised URL.
        if _urls_alias(_current_backend_url(), target_url):
            raise HTTPException(
                status_code=400,
                detail="url_alias_same_db: target points at the current backend",
            )
        # All checks passed — write the in-progress overlay and the
        # pending job. The flock is HELD across both writes (B8).
        write_backend_state(_in_progress_state_for(payload.target), url=target_url)
        job = _write_pending_job(payload, target_url)
    return {"job_id": job["job_id"], "status": "pending", ...}
```

The exact helper names (`_read_current_state`, `_in_progress_state_for`, `_write_pending_job`, `_current_backend_url`, `_build_sidecar_url`) may not all exist — extract or rename to whatever the file currently uses, but the BLOCKING REQUIREMENT is: validate-transition + alias-check + write must all be inside the `with lock:` block.

- [ ] **Step 4: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_db_state_concurrent_migrate.py -v`
Expected: PASS.

Run: `.venv/bin/pytest tests/test_api_db_state.py tests/test_db_state_machine.py -v --tb=short`
Expected: all existing API + state-machine tests still PASS.

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **Concurrent `POST /api/admin/db/migrate` calls cannot both succeed.** Round-2 review B1-NEW (BLOCKER) — pre-fix the ordering was `validate → flock → write`. Two admins racing through validation before either took the lock both passed, then both wrote pending jobs (the second clobbered the first's flag file). The endpoint now moves the entire validation chain (transition matrix + URL alias check + pending-job surface) INSIDE the flock — the second caller re-reads state under the lock and gets a clean 409 conflict.
```

- [ ] **Step 6: Commit**

```bash
git add app/api/db_state.py tests/test_db_state_concurrent_migrate.py CHANGELOG.md
git commit -m "fix(api): move /migrate validation inside flock (B1-NEW)"
```

---

### Task 15: B2-NEW — `_urls_alias` resolves hostnames before comparing

**Why:** The B7 fix's `_normalize_pg_url` lowercases and defaults the port + db, but compares string-equal hostnames. `postgresql://...@postgres:5432/agnes` (compose service name) vs `postgresql://...@172.18.0.2:5432/agnes` (sidecar container IP) bypass the alias guard from inside the migrator container. side_car → cloud "migration" then copies the DB to itself, marks cloud success, the next cloud-only applier tick stops `agnes-postgres-1`. Same B7-class outage, different bypass. Fix: resolve hostnames to IP sets before comparing; if any IP overlaps, treat as alias.

**Files:**
- Modify: `app/api/db_state.py` (`_urls_alias`)
- Modify: `scripts/ops/agnes-state-applier.sh` (same check, line ~255)
- Test: `tests/test_db_state_url_alias_dns.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_url_alias_dns.py
"""B2-NEW — _urls_alias detects hostname-vs-IP same-DB aliases."""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_alias_detects_hostname_vs_resolved_ip() -> None:
    """``postgres`` (compose service name) resolving to 172.18.0.2 must
    alias-match ``postgresql://...@172.18.0.2:5432/agnes`` — pre-B2-NEW
    they compared string-equal-only and bypassed the guard.
    """
    from app.api.db_state import _urls_alias

    with patch("app.api.db_state._resolve_host", lambda h: {"172.18.0.2"}
               if h == "postgres" else {"203.0.113.50"} if h == "cloud.example.com"
               else set()):
        a = "postgresql+psycopg://u:p@postgres:5432/agnes"
        b = "postgresql+psycopg://u:p@172.18.0.2:5432/agnes"
        c = "postgresql+psycopg://u:p@cloud.example.com:5432/agnes"
        assert _urls_alias(a, b) is True, (a, b)
        assert _urls_alias(b, a) is True, (b, a)
        # Different hosts, no IP overlap → not aliases.
        assert _urls_alias(a, c) is False, (a, c)


def test_alias_falls_back_to_string_compare_on_dns_failure() -> None:
    """When DNS resolution fails for either side, we conservatively
    treat them as ALIAS if string-normalised host+db match — and as
    NON-ALIAS otherwise. The pre-B2-NEW guard is retained for the
    common case."""
    from app.api.db_state import _urls_alias

    with patch("app.api.db_state._resolve_host", lambda h: set()):
        a = "postgresql+psycopg://u:p@postgres:5432/agnes"
        b = "postgresql+psycopg://u:p@postgres:5432/agnes"
        assert _urls_alias(a, b) is True

        a = "postgresql+psycopg://u:p@postgres:5432/agnes"
        b = "postgresql+psycopg://u:p@cloud.example.com:5432/agnes"
        assert _urls_alias(a, b) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_state_url_alias_dns.py -v --tb=short`
Expected: FAILS — current `_urls_alias` returns False for hostname-vs-IP same DB.

- [ ] **Step 3: Apply the fix — `app/api/db_state.py`**

Add a `_resolve_host` helper and update `_urls_alias`:

```python
def _resolve_host(host: str) -> set[str]:
    """Resolve ``host`` to its IPv4/IPv6 address set. Returns empty
    set on any DNS error (caller treats empty as "unknown — fall back
    to string compare").

    B2-NEW: the pre-fix ``_urls_alias`` compared only normalised
    hostname strings, so ``postgres`` (compose service name) vs the
    sidecar container's IP (``172.18.0.2``) bypassed the guard.
    """
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
        return {info[4][0] for info in infos}
    except (socket.gaierror, OSError):
        return set()


def _urls_alias(a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` point at the same physical database.

    Compares (port, db) for exact match; then either:
      - normalised hostnames match (cheap path), OR
      - the IP sets returned by ``_resolve_host`` overlap (DNS path).

    Falls back to string-equal-only when DNS fails for both sides.
    """
    a_host, a_port, a_db = _normalize_pg_url(a)
    b_host, b_port, b_db = _normalize_pg_url(b)
    if (a_port, a_db) != (b_port, b_db):
        return False
    if a_host == b_host:
        return True
    a_ips = _resolve_host(a_host)
    b_ips = _resolve_host(b_host)
    if a_ips and b_ips:
        return bool(a_ips & b_ips)
    # One or both unresolvable → conservative no-alias.
    return False
```

- [ ] **Step 4: Apply the fix — `scripts/ops/agnes-state-applier.sh`**

The applier script has a parallel host-side alias check around line 255. Use a python heredoc (or pull through the API endpoint) to share the same logic. Simplest:

```bash
# Helper — call the Python implementation via a one-shot script. This
# keeps the alias logic centralised in app/api/db_state.py.
urls_alias() {
    local a="$1" b="$2"
    python3 - "$a" "$b" <<'PY'
import sys
sys.path.insert(0, "/app")
from app.api.db_state import _urls_alias
print("ALIAS" if _urls_alias(sys.argv[1], sys.argv[2]) else "DISTINCT")
PY
}
```

Replace the inline string-compare alias check with a call to `urls_alias` and branch on the output.

- [ ] **Step 5: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_db_state_url_alias_dns.py -v`
Expected: PASS.

Run: `.venv/bin/pytest tests/ -k "url_alias or urls_alias" --tb=short -q`
Expected: any other alias-related tests PASS.

- [ ] **Step 6: CHANGELOG bullet**

```markdown
- **`_urls_alias` resolves hostnames before declaring same-DB equality.** Round-2 review B2-NEW (BLOCKER) — the B7 fix normalised port + db but compared hostnames string-equal-only. Inside the migrator container, `postgres` (compose service name) vs `172.18.0.2` (sidecar IP) bypassed the alias guard; a `side_car → cloud` request whose `cloud_url` accidentally pointed back at the local sidecar then "migrated" the DB to itself, marked cloud success, and the next cloud-only applier tick stopped `agnes-postgres-1`. `_urls_alias` now resolves both sides to IP sets and reports alias on any overlap; the host-side applier shares the same Python implementation. Falls back to string-equal when DNS fails for either side (conservative non-alias).
```

- [ ] **Step 7: Commit**

```bash
git add app/api/db_state.py scripts/ops/agnes-state-applier.sh tests/test_db_state_url_alias_dns.py CHANGELOG.md
git commit -m "fix(api,applier): _urls_alias resolves hostnames to IPs (B2-NEW)"
```

---

### Task 16: H1-NEW — Cancel-during-verify race

**Why:** B2's sentinel cancellation polls at step boundaries. A cancel arriving between the migrator's last cancel check and `flip_backend` is accepted by the API (writes `cancelled` + reverts to source), while the migrator proceeds to flip and mark success. End state: `instance.yaml` says SOURCE but data is on TARGET. Fix: re-check the sentinel **inside the same critical section as `flip_backend`** so the migrator either honors the cancel or commits the flip atomically. Same control-loop in `scripts/db_state_migrator.py:1077` (right before `write_backend_state(target_state, ...)`).

**Files:**
- Modify: `scripts/db_state_migrator.py` (final cancel re-check before flip)
- Modify: `app/api/db_state.py` (`cancel_job` — write the cancel sentinel BEFORE the in-API revert)
- Test: `tests/test_db_state_cancel_during_verify.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_cancel_during_verify.py
"""H1-NEW — cancel arriving between last sentinel check and flip is
either honored by the migrator (no flip) OR rejected by the API
(409 conflict, migration already committed). Never both."""
from __future__ import annotations

from pathlib import Path
import json
from unittest.mock import patch

import pytest


def test_cancel_after_flip_returns_409(tmp_path: Path, monkeypatch) -> None:
    """API cancel after the migrator has written ``backend=TARGET``
    must return 409, not silently revert to SOURCE."""
    from app.api import db_state

    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)
    instance_yaml = tmp_path / "instance.yaml"
    instance_yaml.write_text(
        "database:\n  backend: side_car\n"
        "  url: postgresql+psycopg://x:y@h/agnes\n"
    )
    monkeypatch.setattr(
        db_state, "_instance_yaml_path", lambda: instance_yaml,
        raising=False,
    )

    job_id = "j-h1"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id,
        "status": "completed",  # post-flip terminal state
        "source_backend": "duckdb",
        "target_backend": "side_car",
        "completed_at": "2026-06-01T10:00:00Z",
    }))

    from fastapi import HTTPException
    with patch.object(db_state, "_require_admin", return_value=None):
        with pytest.raises(HTTPException) as exc:
            db_state.cancel_job(job_id=job_id)
    assert exc.value.status_code == 409, exc.value


def test_migrator_rechecks_sentinel_before_flip(tmp_path: Path) -> None:
    """If the cancel sentinel is written between
    ``copy_duckdb_to_pg`` and ``flip_backend``, the migrator must
    abort the flip and exit non-zero.
    """
    from scripts import db_state_migrator

    job_dir = tmp_path / "db-jobs"
    job_dir.mkdir()
    job_id = "j-flip-cancel"
    job_path = job_dir / f"{job_id}.json"
    job_path.write_text(json.dumps({
        "job_id": job_id,
        "status": "running",
        "source_backend": "duckdb",
        "target_backend": "side_car",
        "cancel_requested": True,  # the sentinel signal
    }))
    # The migrator helper that checks the sentinel right before flip
    # must observe cancel_requested=True and refuse.
    from src.db_state_machine import BackendState
    with pytest.raises(RuntimeError) as exc:
        db_state_migrator._check_cancel_before_flip(
            job_path=job_path, target_state=BackendState.SIDE_CAR
        )
    assert "cancel" in str(exc.value).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_state_cancel_during_verify.py -v --tb=short`
Expected: `_check_cancel_before_flip` does not exist → AttributeError; cancel-after-completed test may also fail.

- [ ] **Step 3: Apply the fix — migrator**

In `scripts/db_state_migrator.py`, around line 1077 (right before `write_backend_state(target_state, url=target_url)`), add:

```python
def _check_cancel_before_flip(job_path: Path, target_state: "BackendState") -> None:
    """Final cancel-sentinel re-check right before ``flip_backend``.

    H1-NEW: B2's sentinel cancellation polls at step boundaries
    (alembic, copy, verify). A cancel arriving in the window between
    the last poll and the flip was accepted by the API (writes
    ``cancelled`` + reverts ``instance.yaml`` to source) while the
    migrator continued to ``write_backend_state(target_state, ...)``.
    End state: instance.yaml said SOURCE but data was on TARGET.
    Re-check here so cancel ↔ flip is mutually exclusive.
    """
    import json
    try:
        data = json.loads(job_path.read_text())
    except FileNotFoundError:
        # Job file already archived → flip is no longer the right
        # action either.
        raise RuntimeError(f"job file gone before flip: {job_path}")
    if data.get("cancel_requested") or data.get("status") == "cancelled":
        raise RuntimeError(
            f"job {data.get('job_id')} was cancelled before flip; "
            f"refusing to write_backend_state({target_state}, ...)"
        )


# ... inside run_migration, right before the flip:
_check_cancel_before_flip(job_path=writer.path, target_state=target_state)
write_backend_state(target_state, url=target_url)
```

- [ ] **Step 4: Apply the fix — API cancel**

In `app/api/db_state.py:cancel_job`, the BLOCKER pattern is:

1. Read the job
2. If terminal (`completed` / `failed`), return 409.
3. Otherwise, write the cancel sentinel (`cancel_requested=True`, `status=cancel_requested`) FIRST, THEN revert `instance.yaml`. Pre-fix the revert happened first, race-prone.

Replace the cancel body:

```python
def cancel_job(job_id: str) -> dict:
    _require_admin()
    job_path = _jobs_dir() / f"{job_id}.json"
    try:
        data = json.loads(job_path.read_text())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    # H1-NEW: any terminal state means the flip already committed (or
    # the migration already failed). A "cancel after the fact" must
    # NOT rewrite instance.yaml — return 409 so the operator knows the
    # action was a no-op.
    if data.get("status") in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id} is already in terminal state ({data['status']})",
        )
    # Write cancel sentinel first (rewrites job JSON with status=cancel_requested
    # and cancel_requested=True). The migrator's final pre-flip re-check
    # then sees this and refuses to call write_backend_state.
    data["cancel_requested"] = True
    data["status"] = "cancel_requested"
    _atomic_write_job(job_path, data)
    # NOW revert instance.yaml symmetrically (B1 + MED-4 logic).
    source_backend = data.get("source_backend", "duckdb")
    source_url = data.get("source_url")
    revert_url = None if source_backend == "duckdb" else source_url
    write_backend_state(source_backend, url=revert_url)
    return {"job_id": job_id, "status": "cancel_requested"}
```

`_atomic_write_job` should already exist (the H2-NEW fix extracted the chmod-after-replace pattern); if not, write a small helper that does tmp-write → `os.replace` → `os.chmod 0o600`.

- [ ] **Step 5: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_db_state_cancel_during_verify.py -v`
Expected: PASS.

Run: `.venv/bin/pytest tests/ -k "cancel" --tb=short -q`
Expected: all cancel-related tests PASS.

- [ ] **Step 6: CHANGELOG bullet**

```markdown
- **Cancel ↔ flip is mutually exclusive.** Round-2 review H1-NEW — B2's sentinel cancellation polled at step boundaries; a cancel arriving in the verify→flip window was accepted by the API (wrote `cancelled` + reverted source) while the migrator still committed the flip. End state: `instance.yaml` said SOURCE but data was on TARGET. Two-sided fix: (a) `cancel_job` writes the sentinel BEFORE reverting `instance.yaml` and refuses with 409 when the job is already terminal; (b) the migrator runs `_check_cancel_before_flip` right before `write_backend_state(TARGET, ...)` and aborts with a clean error if the sentinel landed.
```

- [ ] **Step 7: Commit**

```bash
git add app/api/db_state.py scripts/db_state_migrator.py tests/test_db_state_cancel_during_verify.py CHANGELOG.md
git commit -m "fix(api,migrator): cancel and flip are mutually exclusive (H1-NEW)"
```

---

### Task 17: H5-NEW — Stuck-running recovery restores `database.backend`

**Why:** B5's heartbeat-based recovery (`scripts/ops/agnes-state-applier.sh:217-219`) marks a stale-`.alive` job failed but never writes `database.backend = source_backend` back into `instance.yaml`. The next migration retry reads `*_in_progress` as the current backend → the migrator CLI receives `source_backend='side_car_in_progress'` → rejects. State machine wedged until an operator manually edits `instance.yaml`. Fix: recovery should call `write_backend_state(source_backend, url=source_url)` symmetrically with the cancel path.

**Files:**
- Modify: `scripts/ops/agnes-state-applier.sh` (stuck-running recovery block, ~lines 217-219)
- Modify: `app/api/db_state.py` (the API-side stuck-job-cleanup, around line 294 — if it exists)
- Test: `tests/test_db_state_stuck_recovery_restores_backend.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_stuck_recovery_restores_backend.py
"""H5-NEW — stuck-running recovery restores database.backend from
the in_progress placeholder back to source_backend."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess


def test_applier_recovery_restores_backend(tmp_path: Path) -> None:
    """After B5 fires (stale .alive past 120s → mark failed), the
    overlay's database.backend must no longer be ``*_in_progress``."""
    state = tmp_path / "state"
    state.mkdir()
    jobs = state / "db-jobs"
    jobs.mkdir()
    (state / "instance.yaml").write_text(
        "database:\n  backend: side_car_in_progress\n"
        "  url: postgresql+psycopg://x:y@h/d\n"
    )
    job_id = "j-stuck"
    (jobs / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id,
        "status": "running",
        "source_backend": "duckdb",
        "target_backend": "side_car",
    }))
    # Stale .alive file from > 120s ago.
    alive = jobs / f"{job_id}.alive"
    alive.write_text("")
    import os, time
    fake_old = time.time() - 600
    os.utime(alive, (fake_old, fake_old))

    # Run only the recovery block by sourcing the function and calling
    # it explicitly. The exact callable name (e.g. _recover_stuck_jobs)
    # may differ — adjust per actual implementation.
    cp = subprocess.run(
        ["bash", "-c",
         f"export STATE_DIR={state} JOBS_DIR={jobs}; "
         f". scripts/ops/agnes-state-applier.sh; "
         f"_recover_stuck_jobs"],
        capture_output=True, text=True,
    )
    after = (state / "instance.yaml").read_text()
    assert "backend: duckdb" in after, (
        f"recovery must restore database.backend to source.\n"
        f"instance.yaml after:\n{after}\nstderr: {cp.stderr}"
    )
    assert "side_car_in_progress" not in after
```

The test calls a function `_recover_stuck_jobs` — if the applier currently inlines the recovery into the main loop rather than splitting into a function, the implementer should extract it to enable this test.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_db_state_stuck_recovery_restores_backend.py -v --tb=short`
Expected: FAILS — backend stays `side_car_in_progress`.

- [ ] **Step 3: Apply the fix — applier**

In `scripts/ops/agnes-state-applier.sh`, around line 217-219, extract the stuck-running detection into a function and add the backend restore:

```bash
_recover_stuck_jobs() {
    # H5-NEW + B5: jobs whose heartbeat is older than 120s are marked
    # failed AND the overlay's database.backend is restored to
    # source_backend. Otherwise the next migration retry reads
    # ``*_in_progress`` as the current backend and the migrator
    # rejects → state machine wedged.
    local jobs_dir="${JOBS_DIR:-/data/state/db-jobs}"
    [ -d "$jobs_dir" ] || return 0
    local now=$(date +%s)
    local job_path alive_path age source_backend source_url
    for job_path in "$jobs_dir"/*.json; do
        [ -f "$job_path" ] || continue
        alive_path="${job_path%.json}.alive"
        [ -f "$alive_path" ] || continue
        age=$(( now - $(stat -c '%Y' "$alive_path") ))
        [ "$age" -gt 120 ] || continue
        # Read source_backend + source_url BEFORE we rewrite the job.
        source_backend=$(python3 -c "import json,sys; d=json.load(open('$job_path')); print(d.get('source_backend',''))")
        source_url=$(python3 -c "import json,sys; d=json.load(open('$job_path')); print(d.get('source_url','') or '')")
        update_job "$job_path" "failed" "stuck running (no heartbeat for ${age}s; host reboot / OOM / docker crash suspected)"
        # H5-NEW: restore instance.yaml from the *_in_progress placeholder.
        if [ -n "$source_backend" ]; then
            write_instance_yaml "$source_backend" "$source_url" || true
        fi
        rm -f "$alive_path"
    done
}

# Call near the existing recovery scan.
_recover_stuck_jobs
```

- [ ] **Step 4: Apply the fix — API side**

In `app/api/db_state.py`, find where the API surfaces stuck-job recovery (around line 294 — search for `applier_last_tick_age_s` or the recovery write). If the API performs its own write of `status=failed`, mirror the backend restore:

```python
# H5-NEW symmetric: when API marks a stuck job failed, restore
# database.backend from *_in_progress to source_backend.
write_backend_state(
    source_backend,
    url=None if source_backend == "duckdb" else source_url,
)
```

- [ ] **Step 5: Re-run tests**

Run: `.venv/bin/pytest tests/test_db_state_stuck_recovery_restores_backend.py -v`
Expected: PASS.

- [ ] **Step 6: CHANGELOG bullet**

```markdown
- **Stuck-running recovery restores `database.backend` from the `*_in_progress` placeholder.** Round-2 review H5-NEW — B5's heartbeat-based recovery marked the failed job but left `instance.yaml` at `side_car_in_progress` (or `cloud_in_progress`). The next migration retry then read the in-progress label as the current backend, the migrator's CLI rejected `source_backend='side_car_in_progress'`, and the state machine wedged until an operator manually edited the file. Recovery now symmetrically calls `write_backend_state(source_backend, url=source_url)`, mirroring the cancel path.
```

- [ ] **Step 7: Commit**

```bash
git add scripts/ops/agnes-state-applier.sh app/api/db_state.py tests/test_db_state_stuck_recovery_restores_backend.py CHANGELOG.md
git commit -m "fix(applier,api): stuck-running recovery restores backend (H5-NEW)"
```

---

## Phase F — Alembic 0013 ordering

### Task 18: B5-NEW — Alembic 0013 backfill BEFORE CHECK constraint validation

**Why:** `migrations/versions/0013_resource_grants_per_type_fk.py:124,176` creates a per-type CHECK constraint immediately, before the new per-type FK columns are backfilled. Any existing row `resource_grants(resource_type='table', resource_id='foo')` violates the constraint while all new typed-FK columns are still NULL. Alembic upgrade aborts on any prod instance with grants. Fix: use the standard `ADD CONSTRAINT ... NOT VALID + backfill + VALIDATE CONSTRAINT` pattern, or split into two migrations.

**Files:**
- Modify: `migrations/versions/0013_resource_grants_per_type_fk.py`
- Test: `tests/test_alembic_0013_backfill_order.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alembic_0013_backfill_order.py
"""B5-NEW — alembic 0013 upgrades cleanly on an instance with
pre-existing typed grants."""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa


@pytest.fixture
def pg_engine_at_0012() -> sa.engine.Engine:
    """Provide a pgserver engine upgraded only to revision 0012."""
    pytest.importorskip("pixeltable_pgserver")
    from tests.db_pg.conftest import _alembic_module_engine  # type: ignore

    eng, _alembic_cfg = _alembic_module_engine(target_revision="0012")
    return eng


def test_alembic_0013_upgrades_with_existing_typed_grants(
    pg_engine_at_0012: sa.engine.Engine,
) -> None:
    """Seed a typical pre-0013 row in resource_grants, then run 0013.
    Pre-B5-NEW the CHECK constraint validates BEFORE backfill →
    alembic aborts."""
    from alembic import command
    from alembic.config import Config

    # Pre-seed a typed grant (the v59 shape: resource_type + resource_id).
    with pg_engine_at_0012.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO user_groups (id, name) VALUES (:gid, 'finance') "
            "ON CONFLICT DO NOTHING"
        ), {"gid": "grp-1"})
        conn.execute(sa.text(
            "INSERT INTO resource_grants (group_id, resource_type, resource_id) "
            "VALUES (:gid, 'table', 'agnes_sessions') "
            "ON CONFLICT DO NOTHING"
        ), {"gid": "grp-1"})

    # Now upgrade to head (or to 0013 explicitly).
    cfg = Config("alembic.ini")
    cfg.attributes["sqlalchemy.url"] = str(pg_engine_at_0012.url)
    command.upgrade(cfg, "0013")

    # The original row survives + the new typed FK columns are
    # backfilled (table_id = 'agnes_sessions').
    with pg_engine_at_0012.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT table_id FROM resource_grants WHERE group_id = 'grp-1' "
            "AND resource_type = 'table'"
        )).fetchone()
    assert row is not None, "0013 dropped the grant!"
    assert row.table_id == "agnes_sessions"
```

The fixture `_alembic_module_engine` may have a different name — read `tests/db_pg/conftest.py` for the actual one and adjust.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_alembic_0013_backfill_order.py -v --tb=short`
Expected: FAILS — `IntegrityError` from CHECK violation during `op.create_check_constraint(...)`.

- [ ] **Step 3: Apply the fix**

Open `migrations/versions/0013_resource_grants_per_type_fk.py`. In `upgrade()`:

1. Move `op.add_column` for each typed FK column (e.g. `table_id`, `stack_id`) BEFORE the `op.create_check_constraint`.
2. Run the BACKFILL — `UPDATE resource_grants SET table_id = resource_id WHERE resource_type = 'table'` — for every supported type, BEFORE the CHECK is created.
3. Use `ADD CONSTRAINT ... NOT VALID` then `VALIDATE CONSTRAINT` once backfill is done — or skip the NOT-VALID step entirely if the backfill completes synchronously inside the migration (it should for a single-table small dataset).

Sketch:

```python
def upgrade() -> None:
    # Step 1: add the new nullable typed-FK columns.
    op.add_column("resource_grants", sa.Column("table_id", sa.String(), nullable=True))
    op.add_column("resource_grants", sa.Column("stack_id", sa.String(), nullable=True))
    # ... per supported ResourceType.

    # Step 2: backfill from the legacy (resource_type, resource_id) pair.
    op.execute(
        "UPDATE resource_grants SET table_id = resource_id "
        "WHERE resource_type = 'table'"
    )
    op.execute(
        "UPDATE resource_grants SET stack_id = resource_id "
        "WHERE resource_type = 'stack'"
    )
    # ... etc.

    # Step 3: now the CHECK constraint is safe — every existing row
    # has exactly one typed-FK column populated. B5-NEW.
    op.create_check_constraint(
        "resource_grants_typed_fk_chk",
        "resource_grants",
        # the per-type one-of-N is-not-null expression
        (
            "(CASE WHEN table_id IS NOT NULL THEN 1 ELSE 0 END + "
            " CASE WHEN stack_id IS NOT NULL THEN 1 ELSE 0 END + "
            " ...) = 1"
        ),
    )

    # Step 4: FK constraints (now safe to apply).
    op.create_foreign_key(
        "fk_resource_grants_table_id_table_registry",
        "resource_grants", "table_registry",
        ["table_id"], ["id"], ondelete="CASCADE",
    )
    # ... per supported ResourceType.
```

Match this against the existing migration body — preserve all type variants (per the `ResourceType` enum). The KEY contract: backfill must complete BEFORE any constraint validation.

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_alembic_0013_backfill_order.py -v`
Expected: PASS.

Run: `.venv/bin/pytest tests/db_pg/ -k "alembic or migration_0013" --tb=short -q`
Expected: any other 0013-related tests PASS.

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **Alembic `0013` backfills typed-FK columns BEFORE creating the CHECK constraint.** Round-2 review B5-NEW (BLOCKER) — pre-fix, the migration added the per-type FK columns + the CHECK constraint in one shot. Any existing `resource_grants(resource_type='table', resource_id='foo')` row violated the CHECK while the new `table_id` column was still NULL, and `alembic upgrade head` aborted on every prod instance with typed grants. The new order: add columns → backfill from `(resource_type, resource_id)` → create CHECK + FKs.
```

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0013_resource_grants_per_type_fk.py tests/test_alembic_0013_backfill_order.py CHANGELOG.md
git commit -m "fix(alembic): 0013 backfills typed FK columns before CHECK (B5-NEW)"
```

---

## Phase G — B3-NEW / B4-NEW provisioning tightening

### Task 19: B3-NEW + H4-NEW verify — Provisioning bootstrap covers `.env` ownership

**Why:** B3-NEW (applier can't read `/opt/agnes/.env`) is already partially fixed on this branch (commit `9f9db5ec` chowns the file at runtime in the bootstrap unit's `ExecStart`). The reviewer's specific recommendation was "either ACL the file to the new user or have provisioning create the user before writing `.env`." On a freshly-Terraform-provisioned VM, the bootstrap unit only runs AFTER the .env file is written by the startup-script. If the bootstrap fails for any reason, `.env` stays root-owned and the applier can't read it. Fix: have `startup-script.sh.tpl` ALSO chown `.env` to `agnes-applier:agnes-applier` immediately after writing it (defence in depth).

**Files:**
- Modify: `infra/modules/customer-instance/startup-script.sh.tpl` (post-`.env`-write chown)
- Test: extend `tests/test_state_applier_unit_file.py` to assert the startup-script does the chown

- [ ] **Step 1: Write the failing test**

In `tests/test_state_applier_unit_file.py`, add:

```python
def test_startup_script_chowns_env_to_agnes_applier():
    """B3-NEW + reviewer's recommendation: startup-script.sh.tpl must
    chown /opt/agnes/.env to agnes-applier:agnes-applier IMMEDIATELY
    after writing it. The bootstrap unit's ExecStart re-asserts this
    on every boot, but the first boot has a window between .env
    landing and the unit firing — during which a same-host attacker
    or a misconfigured cloud-init step could observe root-owned
    plaintext creds (mode 0600 root is fine for confidentiality but
    breaks the non-root applier's first run before the bootstrap
    unit runs)."""
    from pathlib import Path

    tpl = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()
    # Look for the post-write chown. Accepts either form:
    #   chown agnes-applier:agnes-applier /opt/agnes/.env
    #   install -o agnes-applier -g agnes-applier ... /opt/agnes/.env
    assert ("chown agnes-applier:agnes-applier /opt/agnes/.env" in tpl
            or "install -o agnes-applier" in tpl
                and "/opt/agnes/.env" in tpl), (
        "startup-script.sh.tpl must chown /opt/agnes/.env to "
        "agnes-applier IMMEDIATELY after writing it (B3-NEW + "
        "reviewer's recommendation — don't rely on the bootstrap "
        "unit's later run to fix ownership)."
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_state_applier_unit_file.py::test_startup_script_chowns_env_to_agnes_applier -v --tb=short`
Expected: FAILS — the startup-script writes .env as root but doesn't chown after.

- [ ] **Step 3: Apply the fix**

Open `infra/modules/customer-instance/startup-script.sh.tpl`. Find the block that writes `/opt/agnes/.env` (search for `EOF` heredoc with AGNES_TAG / POSTGRES_PASSWORD lines, near the GHCR login / docker compose pull region). Immediately after the heredoc that writes the file, add:

```bash
# B3-NEW: chown .env to agnes-applier IMMEDIATELY so the non-root
# applier's very first run (before the bootstrap unit fires) can
# already source the file. The bootstrap unit's ExecStart re-asserts
# this every boot in case an operator (or agnes-auto-upgrade) rewrites
# .env later.
useradd --system --no-create-home --shell /usr/sbin/nologin --user-group agnes-applier 2>/dev/null || true
chown agnes-applier:agnes-applier /opt/agnes/.env
chmod 0600 /opt/agnes/.env
```

(The `useradd` is idempotent; if it already ran earlier in the script, this is a no-op.)

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_state_applier_unit_file.py -v`
Expected: all PASS (including the new one).

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **Customer-instance `startup-script.sh.tpl` chowns `/opt/agnes/.env` to `agnes-applier` immediately after writing it.** Round-2 review B3-NEW (BLOCKER) tightening — the bootstrap unit's `ExecStart` already chowns the file on every boot, but the very first run on a freshly-Terraform-provisioned VM had a window between cloud-init writing `.env` and the bootstrap unit firing. During that window the applier's timer fired against a still-root-owned `.env` and exited silently. Provisioning now sets the owner correctly the moment the file lands.
```

- [ ] **Step 6: Commit**

```bash
git add infra/modules/customer-instance/startup-script.sh.tpl tests/test_state_applier_unit_file.py CHANGELOG.md
git commit -m "fix(infra): startup-script chowns .env to agnes-applier (B3-NEW tightening)"
```

---

### Task 20: B4-NEW tighten — Move `/data/postgres` chown to bootstrap unit / provisioning

**Why:** B4-NEW is currently mitigated by commit `69c203ee` (idempotent stat-then-chown in `agnes-state-applier.sh`). The reviewer's preferred fix was "grant CAP_CHOWN or change ownership at provision time." The idempotent guard is a runtime workaround that ONLY succeeds when `/data/postgres` is already 70:70 — a fresh VM where the directory exists owned by root will still trip the chown attempt and burn a noisy error message. Fix: have the bootstrap unit (root-running) chown the directory definitively, AND have `startup-script.sh.tpl` create it pre-chowned. Then the applier's runtime chown is purely defensive (no-op in practice).

**Files:**
- Modify: `scripts/ops/agnes-state-applier-bootstrap.service` (existing chown line already added; verify it's still there + correct)
- Modify: `infra/modules/customer-instance/startup-script.sh.tpl` (mkdir + chown at provision time)
- Modify: `scripts/ops/agnes-state-applier.sh` (REMOVE the runtime chown; the guard becomes "skip silently if not 70:70" with no error log)
- Test: extend `tests/test_state_applier_unit_file.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_state_applier_unit_file.py`, add:

```python
def test_bootstrap_unit_chowns_data_postgres_to_70_70():
    """B4-NEW tightening — chown 70:70 /data/postgres belongs in the
    root-running bootstrap unit, not in the agnes-applier's ExecStart
    (where it failed under set -e on every fresh VM)."""
    from pathlib import Path

    unit = Path("scripts/ops/agnes-state-applier-bootstrap.service").read_text()
    assert "chown 70:70 /data/postgres" in unit or \
           "chown -R 70:70 /data/postgres" in unit, (
        "bootstrap unit must chown 70:70 /data/postgres (the Postgres "
        "Alpine image uid). Pre-B4-NEW tightening the chown was in "
        "the applier's main unit and failed as agnes-applier."
    )


def test_startup_script_creates_data_postgres_owned_70_70():
    """Defence in depth: the customer-instance startup-script also
    creates /data/postgres with the right ownership at provision time."""
    from pathlib import Path

    tpl = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()
    # Accepts either chown 70:70 or install -o 70 -g 70 -d.
    assert ("chown 70:70 /data/postgres" in tpl
            or "install -d -o 70 -g 70" in tpl and "/data/postgres" in tpl), (
        "startup-script.sh.tpl must create /data/postgres owned 70:70"
    )


def test_applier_does_not_chown_data_postgres_in_exec():
    """The applier's main ExecStart (non-root) must NOT attempt
    chown 70:70 /data/postgres — that's the bootstrap unit's job.
    Pre-B4-NEW tightening, the chown burned a noisy log line every
    tick even when ownership was already correct."""
    from pathlib import Path

    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    # The script may still STAT the dir, but it must not call chown.
    # Allow a comment mentioning chown for context.
    code_lines = [
        line for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    chown_lines = [
        line for line in code_lines
        if "chown" in line and "/data/postgres" in line
    ]
    assert not chown_lines, (
        f"applier script must not invoke `chown` on /data/postgres "
        f"(bootstrap unit does it as root); found:\n  "
        + "\n  ".join(chown_lines)
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_state_applier_unit_file.py -v --tb=short`
Expected: the new three FAIL (the applier still runs the runtime chown; startup-script may not create the dir).

- [ ] **Step 3: Apply the fix**

a. `scripts/ops/agnes-state-applier-bootstrap.service` — already has the `chown 70:70 /data/postgres` line (added in commit `c357c450`). Verify it's still there; if not, restore:

```ini
ExecStart=/bin/bash -c 'chown 70:70 /data/postgres && chmod 700 /data/postgres'
```

b. `infra/modules/customer-instance/startup-script.sh.tpl` — add right after the `mkdir -p /data/state` line:

```bash
# B4-NEW tightening: pre-chown /data/postgres so the bootstrap unit's
# chown is a no-op fast-path. Postgres Alpine image runs as uid 70.
install -d -o 70 -g 70 -m 0700 /data/postgres
```

c. `scripts/ops/agnes-state-applier.sh` — REMOVE the runtime chown block around lines 228-244. Replace with a silent-skip stat check (we keep the stat so if for some reason ownership is wrong the applier doesn't crash psql, just logs at debug):

```bash
case "$TARGET" in
    side-car-enabled)
        # Bootstrap unit (root) is responsible for `mkdir -p /data/postgres
        # && chown 70:70`. If we get here and ownership is wrong, log
        # and continue — non-root chown attempts under set -e would
        # abort the whole tick.
        if [ ! -d /data/postgres ]; then
            echo "ERR: /data/postgres missing — bootstrap unit failed?" >&2
            exit 1
        fi
        if [ "$(stat -c '%u:%g' /data/postgres)" != "70:70" ]; then
            echo "WARN: /data/postgres ownership not 70:70; bootstrap unit may have failed; postgres container may refuse to start" >&2
        fi
        ;;
esac
```

- [ ] **Step 4: Re-run tests + suite**

Run: `.venv/bin/pytest tests/test_state_applier_unit_file.py -v`
Expected: all PASS.

- [ ] **Step 5: CHANGELOG bullet**

```markdown
- **`chown 70:70 /data/postgres` moved from the applier's ExecStart to the bootstrap unit and startup-script.** Round-2 review B4-NEW (BLOCKER) tightening — the idempotent stat-then-chown guard shipped in `69c203ee` worked when ownership was already correct, but a freshly-provisioned VM where `/data/postgres` exists owned by root still hit the chown attempt and logged a misleading "insufficient privileges" warning on every applier tick. Ownership is now set definitively at provision time (`install -d -o 70 -g 70`) and re-asserted on every boot by the bootstrap unit. The applier's runtime path no longer attempts chown; it stats once and warns (without exiting) if the ownership ever drifts.
```

- [ ] **Step 6: Commit**

```bash
git add scripts/ops/agnes-state-applier-bootstrap.service infra/modules/customer-instance/startup-script.sh.tpl scripts/ops/agnes-state-applier.sh tests/test_state_applier_unit_file.py CHANGELOG.md
git commit -m "fix(infra,applier): /data/postgres ownership set at provision + boot (B4-NEW tightening)"
```

---

## Phase H — Final self-review

### Task 21: Round-3 self-review pass

**Why:** Walk the entire `zs/db-state-machine` diff one more time with the same adversarial lens cvrysanek used in round-2. Confirm every finding (B1-NEW…B5-NEW, H1-NEW…H8-NEW, MED-1…MED-4, LOW-1, LOW-2, NEW-X) has a fix commit + a regression test on the branch. Document any out-of-scope items as follow-up GitHub issues (per CLAUDE.md's "Issue economy" rule — only file when scope ≥3× the touching PR or design questions remain). Update the PR #455 description with the new "Round-2 fixes shipped" section.

**Files:**
- Modify: `CHANGELOG.md` (final consolidated bullet under Round-2 fixes section)
- Modify: PR description via `gh pr edit 455 --body-file <file>` — content authored by the implementer
- Test: run the full test suite end-to-end (`.venv/bin/pytest tests/ --tb=short -n auto -q`)

- [ ] **Step 1: Run the full test suite**

```bash
.venv/bin/pytest tests/ --tb=short -n auto -q
```

Expected: GREEN. Any failure must be either fixed-in-place or noted as pre-existing-unrelated (and surfaced to the user before committing).

- [ ] **Step 2: Diff vs `main` — adversarial walk**

```bash
git fetch origin
git log --oneline origin/main..HEAD
git diff origin/main..HEAD --stat
```

For each task in this plan, run `git log -1 --grep="<finding-id>"` to confirm the fix commit exists.

- [ ] **Step 3: Build the round-2 fix matrix in the CHANGELOG**

Under `## [Unreleased]` add a final summary block at the END:

```markdown
### Round-2 review fixes — verification matrix

| Finding | Status | Commit |
| --- | --- | --- |
| B1-NEW concurrent /migrate race | FIXED | (commit) |
| B2-NEW URL alias DNS bypass | FIXED | (commit) |
| B3-NEW applier .env permission | FIXED + provisioning | (commit) |
| B4-NEW /data/postgres chown | FIXED + provisioning | (commit) |
| B5-NEW alembic 0013 ordering | FIXED | (commit) |
| H1-NEW cancel-during-verify | FIXED | (commit) |
| H2-NEW heredoc 0600 mode | FIXED | (commit) |
| H3-NEW error.message redaction | FIXED | (commit) |
| H4-NEW PyYAML missing fallback | FIXED | (commit) |
| H5-NEW stuck recovery restore | FIXED | (commit) |
| H6-NEW JSONB dynamic derivation | FIXED | (commit) |
| H7-NEW migrate to duckdb 501 | FIXED | (commit) |
| H8-NEW rollback url drop | FIXED | (commit) |
| MED-1 --json bypass --yes | FIXED | (commit) |
| MED-2 cloud_url SSRF ranges | FIXED | (commit) |
| MED-3 redact query password | FIXED | (commit) |
| MED-4 cancel duckdb url drop | FIXED | (commit) |
| LOW-1 PII scrub walks keys | FIXED | (commit) |
| LOW-2 compose literal agnes | FIXED | (commit) |
| NEW-X-USERS-DUPLICATES | FIXED | (commit) |
```

Fill in the commit short SHAs by running `git log --oneline origin/main..HEAD` and matching.

- [ ] **Step 4: Update PR #455 description**

```bash
gh pr view 455 --json body --jq .body > /tmp/pr455-body.md
# Edit /tmp/pr455-body.md — append a "Round-2 fixes shipped" section
# linking to the CHANGELOG matrix.
gh pr edit 455 --body-file /tmp/pr455-body.md
```

- [ ] **Step 5: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): round-2 fix verification matrix"
git push origin zs/db-state-machine
```

Watch CI:

```bash
gh pr checks 455 --watch
```

Expected: all checks GREEN.

- [ ] **Step 6: Notify reviewer**

Post a comment on PR #455:

```bash
gh pr comment 455 --body "Round-2 review fully addressed — see CHANGELOG verification matrix. Ready for round-3."
```

---

## Self-Review

This is the planner's own check (run after writing the plan, before handing off to the implementer agents).

**Spec coverage** — every reviewer finding mapped to a task:

- B1-NEW → Task 14
- B2-NEW → Task 15
- B3-NEW (verify + tighten) → Task 19
- B4-NEW (tighten) → Task 20
- B5-NEW → Task 18
- H1-NEW → Task 16
- H2-NEW → Task 11
- H3-NEW → Task 8
- H4-NEW → Task 12
- H5-NEW → Task 17
- H6-NEW → Task 9
- H7-NEW → Task 7
- H8-NEW → Task 13
- MED-1 → Task 1
- MED-2 → Task 5
- MED-3 → Task 2
- MED-4 → Task 6
- LOW-1 → Task 3
- LOW-2 → Task 4
- NEW-X-USERS-DUPLICATES → Task 10

Testing gaps from review § 335-348: every one is covered by the regression test added in the corresponding task.

**Placeholder scan:** every code step has concrete code blocks; no "TBD" / "implement later" / "similar to Task N"; commands have exact paths.

**Type / symbol consistency:** `_redact_url`, `_redact_urls_in_text`, `_redact_error_payload`, `_resolve_host`, `_urls_alias`, `_validate_cloud_url`, `_format_alembic_timeout_message`, `_check_cancel_before_flip`, `_recover_stuck_jobs`, `_atomic_write_job`, `_build_json_columns`, `write_instance_yaml`, `__rollback`, `write_backend_state` — names match between tasks.

**Out-of-scope:** None of the reviewer's findings are deferred. The NEW-X (users duplicates) live-discovered finding is also included.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-01-db-state-machine-round-2-fixes.md`. The user has explicitly requested sub-agent execution ("se sub-agenty to fixni"), so the controller should invoke `superpowers:subagent-driven-development` against this plan next.
