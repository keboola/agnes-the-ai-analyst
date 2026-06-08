# Slack bot tokens in the vault — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the three server-wide Slack bot secrets (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`) settable/rotatable from the admin UI via the encrypted secret vault, resolved as `env > vault > none`.

**Architecture:** A new `system_secrets` vault scope (DuckDB + Postgres, dual-backend via the repo factory) stores the Fernet-encrypted tokens keyed by name. A single `slack_secret()` accessor resolves `env > vault > none` and replaces the 12 direct `os.environ.get("SLACK_…")` reads. New admin endpoints (`/api/admin/slack-secrets`) write to the vault (never the config overlay) with write-only semantics, surfaced as a section on `/admin/server-config`.

**Tech Stack:** Python, FastAPI, DuckDB, SQLAlchemy/Alembic (Postgres), Fernet (`cryptography`), pytest.

**Spec:** `docs/superpowers/specs/2026-06-04-slack-bot-tokens-vault-design.md`

**Run tests with:** `.venv/bin/pytest <path> --tb=short -q` (full suite: `.venv/bin/pytest tests/ --tb=short -n auto -q`).

---

## File Structure

- `src/db.py` — DuckDB migration `_v71_to_v72` + `SCHEMA_VERSION` 71→72 + ladder wiring (2 sites).
- `migrations/versions/0019_system_secrets_v72.py` — Alembic migration (new).
- `src/models/vault.py` — `SystemSecret` ORM model (new).
- `src/models/__init__.py` — register `SystemSecret`.
- `scripts/migrate_duckdb_to_pg/__init__.py` — add `"system_secrets": ["name"]` to `_PK_COLUMNS`.
- `app/secrets_vault.py` — `SystemSecretsRepository` (DuckDB).
- `src/repositories/secrets_vault_pg.py` — `SystemSecretsPgRepository`.
- `src/repositories/__init__.py` — `system_secrets_repo()` factory + export.
- `tests/db_pg/test_system_secrets_contract.py` — cross-engine contract test (new).
- `services/slack_bot/secrets.py` — `slack_secret()` resolver + allow-list (new).
- `tests/test_slack_secret_resolver.py` — resolver unit tests (new).
- 4 read-site files — swap `os.environ.get("SLACK_…")` → `slack_secret("SLACK_…")`.
- `app/api/admin_slack_secrets.py` — admin endpoints (new).
- `app/main.py` — register the new router.
- `tests/test_admin_slack_secrets.py` — API tests (new).
- `app/web/templates/admin_server_config.html` — Slack secrets UI section.
- `CHANGELOG.md`, `config/.env.template`, `docs/slack-manifest-http.md`, `docs/slack-manifest-socket.md` — docs.
- `pyproject.toml` + `CHANGELOG.md` — release-cut to 0.66.0.

---

## Task 1: DuckDB `system_secrets` migration (v71 → v72)

**Files:**
- Modify: `src/db.py` (line 50 `SCHEMA_VERSION`; add `_v71_to_v72` near line 4903; wire into both ladders ~5227 and ~5422)
- Test: `tests/test_db_schema_version.py` (existing gate — no new test file; we run it)

- [ ] **Step 1: Add the migration function** after `_v70_to_v71` (around line 4903 in `src/db.py`):

```python
def _v71_to_v72(conn: duckdb.DuckDBPyConnection) -> None:
    """v72: ``system_secrets`` table — server-wide vault for system-level
    secrets keyed by name (Slack bot tokens).

    Distinct from ``mcp_secrets`` (keyed by ``source_id``, MCP data sources):
    this scope holds server-wide secrets that are not tied to any MCP source,
    starting with the three Slack bot tokens. Fernet-encrypted at rest, read
    via ``env > vault`` by ``services/slack_bot/secrets.slack_secret``.

    Idempotent CREATE TABLE IF NOT EXISTS — safe on fresh and upgrade paths.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_secrets (
            name             VARCHAR PRIMARY KEY,
            secret_value_enc BLOB NOT NULL,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 72")
```

- [ ] **Step 2: Bump the version constant** at `src/db.py:50`:

```python
SCHEMA_VERSION = 72
```

- [ ] **Step 3: Wire into the fresh-install ladder** — after the `_v70_to_v71(conn)` call (around line 5227), add:

```python
            # v71→v72: system_secrets — server-wide vault for Slack bot tokens.
            _v71_to_v72(conn)
```

- [ ] **Step 4: Wire into the sequential-upgrade ladder** — after the `if current < 71: _v70_to_v71(conn)` block (around line 5422), add:

```python
            if current < 72:
                _v71_to_v72(conn)
```

- [ ] **Step 5: Run the schema-version gate**

Run: `.venv/bin/pytest tests/test_db_schema_version.py --tb=short -q`
Expected: PASS (DuckDB reaches v72; the test will also check Alembic parity — that comes in Task 2, so if this test asserts cross-backend parity it may fail until Task 2. If it fails only on the Alembic-head comparison, proceed to Task 2 and re-run at the end of Task 2.)

- [ ] **Step 6: Commit**

```bash
git add src/db.py
git commit -m "feat(db): system_secrets table (DuckDB v72)"
```

---

## Task 2: Postgres parity — Alembic migration + ORM model + migrator PK

**Files:**
- Create: `migrations/versions/0019_system_secrets_v72.py`
- Create: `src/models/vault.py`
- Modify: `src/models/__init__.py` (import + `__all__`)
- Modify: `scripts/migrate_duckdb_to_pg/__init__.py` (`_PK_COLUMNS`)
- Test: `tests/db_pg/test_schema_parity.py`, `tests/db_pg/test_data_migration.py`

- [ ] **Step 1: Create the Alembic migration** `migrations/versions/0019_system_secrets_v72.py`:

```python
"""system_secrets table (DuckDB v72 parity).

Server-wide vault for system-level secrets keyed by name (Slack bot tokens).
Mirrors DuckDB ``_v71_to_v72``. Additive-only.

Revision ID: 0019_system_secrets_v72
Revises: 0018_slack_user_id_v71
Create Date: 2026-06-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_system_secrets_v72"
down_revision: Union[str, None] = "0018_slack_user_id_v71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_secrets",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("secret_value_enc", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("system_secrets")
```

- [ ] **Step 2: Create the ORM model** `src/models/vault.py`:

```python
"""SQLAlchemy model for the system-secrets vault.

Mirrors:
  - system_secrets (src/db.py v72)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class SystemSecret(Base):
    """Server-wide vault for system-level secrets keyed by name (v72).

    Holds the Fernet ciphertext of server-wide secrets not tied to an MCP
    source — currently the three Slack bot tokens.
    """
    __tablename__ = "system_secrets"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    secret_value_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
```

- [ ] **Step 3: Register the model** in `src/models/__init__.py` — add the import (after the `from src.models.telemetry import (...)` block, before or after the mcp import) and the `__all__` entry:

```python
from src.models.vault import SystemSecret
```

And add `"SystemSecret",` to the `__all__` list (keep it roughly alphabetical, e.g. after `"SyncState",`).

- [ ] **Step 4: Add the non-`id` PK mapping** in `scripts/migrate_duckdb_to_pg/__init__.py` — inside `_PK_COLUMNS` (near the MCP entries, ~line 103), add:

```python
    "system_secrets": ["name"],
```

- [ ] **Step 5: Run the schema parity + migrator gates**

Run: `.venv/bin/pytest tests/db_pg/test_schema_parity.py tests/db_pg/test_data_migration.py tests/test_db_schema_version.py --tb=short -q`
Expected: PASS — `test_alembic_head_materializes_every_model` sees `system_secrets`, and `test_non_id_pk_tables_are_in_pk_columns_map` accepts the `name` PK.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0019_system_secrets_v72.py src/models/vault.py src/models/__init__.py scripts/migrate_duckdb_to_pg/__init__.py
git commit -m "feat(db): system_secrets Postgres parity (alembic 0019 + model + migrator PK)"
```

---

## Task 3: `SystemSecretsRepository` (DuckDB)

**Files:**
- Modify: `app/secrets_vault.py` (add class after `SharedSecretsRepository`, ~line 198)

- [ ] **Step 1: Add the repository** in `app/secrets_vault.py` (after `SharedSecretsRepository`):

```python
# ---------------------------------------------------------------------------
# Repository — server-wide system secrets (system_secrets table)
# ---------------------------------------------------------------------------


class SystemSecretsRepository:
    """Server-wide system secrets keyed by ``name`` (Slack bot tokens)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def upsert(self, name: str, value: str) -> None:
        """Store the encrypted secret for ``name``. Replaces any prior row."""
        token = encrypt_secret(value)
        self.conn.execute(
            """INSERT INTO system_secrets (name, secret_value_enc, updated_at)
               VALUES (?, ?, current_timestamp)
               ON CONFLICT (name) DO UPDATE SET
                 secret_value_enc = excluded.secret_value_enc,
                 updated_at       = excluded.updated_at""",
            [name, token],
        )

    def get(self, name: str) -> Optional[str]:
        """Decrypted secret for ``name`` or ``None`` when absent or
        undecryptable. Catches both ``InvalidToken`` (vault key rotated) and
        ``RuntimeError`` (``AGNES_VAULT_KEY`` set-but-malformed) so a bad key
        fails closed (feature disabled) instead of 500-ing every Slack request."""
        row = self.conn.execute(
            "SELECT secret_value_enc FROM system_secrets WHERE name = ?",
            [name],
        ).fetchone()
        if row is None:
            return None
        token = row[0]
        if not isinstance(token, (bytes, bytearray)):
            return None
        try:
            return decrypt_secret(bytes(token))
        except (InvalidToken, RuntimeError):
            logger.warning(
                "system_secrets row for %s failed to decrypt — vault key "
                "rotated or malformed? Treating as unset.",
                name,
            )
            return None

    def delete(self, name: str) -> None:
        self.conn.execute("DELETE FROM system_secrets WHERE name = ?", [name])

    def has(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM system_secrets WHERE name = ? LIMIT 1",
            [name],
        ).fetchone()
        return row is not None
```

(`InvalidToken`, `encrypt_secret`, `decrypt_secret`, `logger`, `Optional`, `duckdb` are already imported at the top of this module.)

- [ ] **Step 2: Commit** (no standalone test yet — the contract test in Task 5 exercises this):

```bash
git add app/secrets_vault.py
git commit -m "feat(vault): SystemSecretsRepository (DuckDB)"
```

---

## Task 4: `SystemSecretsPgRepository` + factory

**Files:**
- Modify: `src/repositories/secrets_vault_pg.py` (add class after `SharedSecretsPgRepository`)
- Modify: `src/repositories/__init__.py` (add `system_secrets_repo()` ~after line 496; add export to the `__all__`-style list ~line 88)

- [ ] **Step 1: Add the PG repository** in `src/repositories/secrets_vault_pg.py` (after `SharedSecretsPgRepository`, before `PerUserSecretsPgRepository`):

```python
class SystemSecretsPgRepository:
    """Server-wide system secrets keyed by ``name`` (PG).

    Signature-compatible with ``app.secrets_vault.SystemSecretsRepository``.
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    def upsert(self, name: str, value: str) -> None:
        token = encrypt_secret(value)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO system_secrets
                           (name, secret_value_enc, updated_at)
                       VALUES (:name, :token, CURRENT_TIMESTAMP)
                       ON CONFLICT (name) DO UPDATE SET
                           secret_value_enc = EXCLUDED.secret_value_enc,
                           updated_at       = EXCLUDED.updated_at"""
                ),
                {"name": name, "token": token},
            )

    def get(self, name: str) -> Optional[str]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT secret_value_enc FROM system_secrets WHERE name = :name"
                ),
                {"name": name},
            ).fetchone()
        if row is None:
            return None
        token = row[0]
        if not isinstance(token, (bytes, bytearray, memoryview)):
            return None
        try:
            return decrypt_secret(bytes(token))
        except (InvalidToken, RuntimeError):
            logger.warning(
                "system_secrets row for %s failed to decrypt — vault key "
                "rotated or malformed? Treating as unset.",
                name,
            )
            return None

    def delete(self, name: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM system_secrets WHERE name = :name"),
                {"name": name},
            )

    def has(self, name: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT 1 FROM system_secrets WHERE name = :name LIMIT 1"),
                {"name": name},
            ).fetchone()
        return row is not None
```

(`sa`, `InvalidToken`, `Engine`, `encrypt_secret`, `decrypt_secret`, `logger`, `Optional` are already imported in this module.)

- [ ] **Step 2: Add the factory** in `src/repositories/__init__.py` after `shared_secrets_repo()` (~line 497):

```python
def system_secrets_repo() -> Any:
    if use_pg():
        from src.repositories.secrets_vault_pg import SystemSecretsPgRepository
        return SystemSecretsPgRepository(_pg_engine())
    from app.secrets_vault import SystemSecretsRepository
    return SystemSecretsRepository(get_system_db())
```

- [ ] **Step 3: Export the factory** — add `"system_secrets_repo",` to the exported-names list near line 88 (the one containing `"shared_secrets_repo"`).

- [ ] **Step 4: Commit**

```bash
git add src/repositories/secrets_vault_pg.py src/repositories/__init__.py
git commit -m "feat(vault): SystemSecretsPgRepository + system_secrets_repo factory"
```

---

## Task 5: Cross-engine contract test

**Files:**
- Create: `tests/db_pg/test_system_secrets_contract.py`

- [ ] **Step 1: Write the contract test** (mirrors `tests/db_pg/test_parity_mcp_shared_vault.py`; `state_backend` is the parametrized fixture from `tests/db_pg/conftest.py`):

```python
"""Cross-engine contract for the system_secrets vault (Slack bot tokens).

This repo lives in app/secrets_vault.py (DuckDB) + src/repositories/
secrets_vault_pg.py (PG), so the automatic method-parity sweep
(tests/db_pg/test_repo_method_parity.py, which only scans
src/repositories/*.py) does NOT cover it. This test is the sole mechanical
guard against DuckDB/PG drift — keep it in lockstep with the repo methods.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()
    return state_backend


def test_system_secret_round_trip_both_backends(_env):
    from src.repositories import system_secrets_repo

    repo = system_secrets_repo()
    repo.upsert("SLACK_BOT_TOKEN", "xoxb-original")
    assert repo.has("SLACK_BOT_TOKEN") is True
    assert repo.get("SLACK_BOT_TOKEN") == "xoxb-original"

    # rotate
    repo.upsert("SLACK_BOT_TOKEN", "xoxb-rotated")
    assert repo.get("SLACK_BOT_TOKEN") == "xoxb-rotated"

    repo.delete("SLACK_BOT_TOKEN")
    assert repo.has("SLACK_BOT_TOKEN") is False
    assert repo.get("SLACK_BOT_TOKEN") is None


def test_system_secret_absent_returns_none_both_backends(_env):
    from src.repositories import system_secrets_repo

    assert system_secrets_repo().get("SLACK_APP_TOKEN") is None
    assert system_secrets_repo().has("SLACK_APP_TOKEN") is False
```

- [ ] **Step 2: Run the contract test**

Run: `.venv/bin/pytest tests/db_pg/test_system_secrets_contract.py --tb=short -q`
Expected: PASS for the DuckDB parametrization. (The PG parametrization runs only when the PG test backend is configured in CI; if it's skipped locally, that's expected.)

- [ ] **Step 3: Commit**

```bash
git add tests/db_pg/test_system_secrets_contract.py
git commit -m "test(vault): cross-engine contract for system_secrets"
```

---

## Task 6: `slack_secret()` resolver + allow-list

**Files:**
- Create: `services/slack_bot/secrets.py`
- Test: `tests/test_slack_secret_resolver.py`

- [ ] **Step 1: Write the failing test** `tests/test_slack_secret_resolver.py`:

```python
"""Unit tests for the env > vault > none Slack secret resolver."""
from __future__ import annotations

import pytest

from services.slack_bot.secrets import SLACK_SECRET_NAMES, slack_secret


def test_env_wins_over_vault(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
    # Even if the vault would return something, env must win and the vault
    # must not be consulted. Make the vault raise to prove it isn't called.
    import services.slack_bot.secrets as mod

    def _boom():
        raise AssertionError("vault must not be consulted when env is set")

    monkeypatch.setattr(
        "src.repositories.system_secrets_repo", _boom, raising=False
    )
    assert slack_secret("SLACK_BOT_TOKEN") == "xoxb-from-env"


def test_vault_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    class _Repo:
        def get(self, name):
            return "xapp-from-vault" if name == "SLACK_APP_TOKEN" else None

    monkeypatch.setattr(
        "src.repositories.system_secrets_repo", lambda: _Repo()
    )
    assert slack_secret("SLACK_APP_TOKEN") == "xapp-from-vault"


def test_none_when_neither(monkeypatch):
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

    class _Repo:
        def get(self, name):
            return None

    monkeypatch.setattr(
        "src.repositories.system_secrets_repo", lambda: _Repo()
    )
    assert slack_secret("SLACK_SIGNING_SECRET") is None


def test_non_allow_listed_name_raises(monkeypatch):
    with pytest.raises(ValueError):
        slack_secret("DATABASE_URL")


def test_vault_failure_is_swallowed(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("src.repositories.system_secrets_repo", _boom)
    # A vault hiccup must not propagate — resolver returns None (fail-closed).
    assert slack_secret("SLACK_BOT_TOKEN") is None


def test_allow_list_contents():
    assert SLACK_SECRET_NAMES == (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_slack_secret_resolver.py --tb=short -q`
Expected: FAIL with `ModuleNotFoundError: services.slack_bot.secrets`.

- [ ] **Step 3: Write the resolver** `services/slack_bot/secrets.py`:

```python
"""Resolve Slack bot secrets: env > vault > None.

Environment variables are authoritative (Terraform / secret-manager
deployments stay in control); the ``system_secrets`` vault is the
UI-managed fallback. Only the three known Slack secret names are
resolvable via the vault — the allow-list prevents using the vault
namespace to read arbitrary environment variables.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SLACK_SECRET_NAMES = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
)


def slack_secret(name: str) -> Optional[str]:
    """Return the value for ``name`` resolving env > vault > None.

    Raises ``ValueError`` for any name outside the Slack allow-list. A vault
    lookup failure (DB unavailable, etc.) is swallowed and treated as unset
    so signature verification fails closed (401) rather than 500-ing.
    """
    if name not in SLACK_SECRET_NAMES:
        raise ValueError(f"{name!r} is not a Slack secret name")
    env = os.environ.get(name)
    if env:
        return env
    try:
        from src.repositories import system_secrets_repo

        return system_secrets_repo().get(name)
    except Exception:
        logger.warning("vault lookup for %s failed; treating as unset", name)
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_slack_secret_resolver.py --tb=short -q`
Expected: PASS (all 6).

- [ ] **Step 5: Commit**

```bash
git add services/slack_bot/secrets.py tests/test_slack_secret_resolver.py
git commit -m "feat(slack): env > vault > none secret resolver"
```

---

## Task 7: Swap the 12 read sites onto `slack_secret()`

**Files:**
- Modify: `app/main.py:324-325`
- Modify: `app/api/slack.py:37,60,82` (+ remove now-unused `import os`)
- Modify: `services/slack_bot/sender.py` (6 sites)
- Modify: `services/slack_bot/identity.py:20`

- [ ] **Step 1: `app/main.py`** — replace lines 324-325:

```python
    from services.slack_bot.secrets import slack_secret
    app_token = slack_secret("SLACK_APP_TOKEN") or ""
    bot_token = slack_secret("SLACK_BOT_TOKEN") or ""
```

(Place the import at the top of the function or module per the file's existing style; the `or ""` preserves the prior empty-string default that `socket_mode_preflight` expects.)

- [ ] **Step 2: `app/api/slack.py`** — add the import near the top:

```python
from services.slack_bot.secrets import slack_secret
```

Replace each of the three `secret = os.environ.get("SLACK_SIGNING_SECRET", "")` (lines 37, 60, 82) with:

```python
    secret = slack_secret("SLACK_SIGNING_SECRET") or ""
```

Then remove the now-unused `import os` (line 6) — verify with `grep -n "os\." app/api/slack.py` that no other `os.` usage remains before deleting.

- [ ] **Step 3: `services/slack_bot/sender.py`** — add the import:

```python
from services.slack_bot.secrets import slack_secret
```

Replace each of the 6 `token = os.environ.get("SLACK_BOT_TOKEN")` with:

```python
    token = slack_secret("SLACK_BOT_TOKEN")
```

Remove the now-unused `import os` if no other `os.` usage remains (`grep -n "os\." services/slack_bot/sender.py`).

- [ ] **Step 4: `services/slack_bot/identity.py`** — add the import and replace line 20:

```python
from services.slack_bot.secrets import slack_secret
```
```python
    token = slack_secret("SLACK_BOT_TOKEN")
```

Remove the now-unused `import os` if nothing else uses it.

- [ ] **Step 5: Run the Slack regression suite**

Run: `.venv/bin/pytest tests/ -k slack --tb=short -q`
Expected: PASS — existing tests monkeypatch `os.environ["SLACK_…"]`, and `slack_secret` checks env first, so they are unaffected.

- [ ] **Step 6: Commit**

```bash
git add app/main.py app/api/slack.py services/slack_bot/sender.py services/slack_bot/identity.py
git commit -m "refactor(slack): resolve bot tokens via slack_secret (env > vault)"
```

---

## Task 8: Admin API — `/api/admin/slack-secrets`

**Files:**
- Create: `app/api/admin_slack_secrets.py`
- Modify: `app/main.py` (import ~line 242 area; `include_router` ~line 1165 after `admin_mcp_router`)
- Test: `tests/test_admin_slack_secrets.py`

- [ ] **Step 1: Write the failing test** `tests/test_admin_slack_secrets.py` (uses the `seeded_app` fixture: `client`, `admin_token`, `analyst_token`):

```python
"""Tests for /api/admin/slack-secrets — admin-gated, write-only, vault-backed."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def test_set_requires_admin(seeded_app):
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/slack-secrets/SLACK_BOT_TOKEN",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "xoxb-x"},
    )
    assert r.status_code == 403


def test_set_rejects_unknown_name(seeded_app):
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/slack-secrets/DATABASE_URL",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "x"},
    )
    assert r.status_code == 400


def test_set_rejects_empty(seeded_app):
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/slack-secrets/SLACK_BOT_TOKEN",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": ""},
    )
    assert r.status_code == 400


def test_set_then_status_reports_vault(seeded_app, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    client = seeded_app["client"]
    admin = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    r = client.put(
        "/api/admin/slack-secrets/SLACK_BOT_TOKEN", headers=admin,
        json={"value": "xoxb-secret"},
    )
    assert r.status_code == 204

    r = client.get("/api/admin/slack-secrets", headers=admin)
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["SLACK_BOT_TOKEN"]["source"] == "vault"
    assert by_name["SLACK_BOT_TOKEN"]["has_value"] is True
    # value is never echoed
    assert "value" not in by_name["SLACK_BOT_TOKEN"]


def test_env_shadows_vault_in_status(seeded_app, monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-env")
    client = seeded_app["client"]
    admin = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.get("/api/admin/slack-secrets", headers=admin)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["SLACK_APP_TOKEN"]["source"] == "env"


def test_delete_clears(seeded_app, monkeypatch):
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    client = seeded_app["client"]
    admin = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    client.put(
        "/api/admin/slack-secrets/SLACK_SIGNING_SECRET", headers=admin,
        json={"value": "shh"},
    )
    r = client.delete("/api/admin/slack-secrets/SLACK_SIGNING_SECRET", headers=admin)
    assert r.status_code == 204
    r = client.get("/api/admin/slack-secrets", headers=admin)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["SLACK_SIGNING_SECRET"]["source"] == "unset"
    assert by_name["SLACK_SIGNING_SECRET"]["has_value"] is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_slack_secrets.py --tb=short -q`
Expected: FAIL (404s — router not registered yet).

- [ ] **Step 3: Write the router** `app/api/admin_slack_secrets.py`:

```python
"""Admin REST API for server-wide Slack bot secrets (vault-backed).

  - GET    /api/admin/slack-secrets          — presence/source status (no values)
  - PUT    /api/admin/slack-secrets/{name}    — set / rotate (write-only)
  - DELETE /api/admin/slack-secrets/{name}    — clear

All gated by ``require_admin``. The secret value lives only in the request
body → Fernet-encrypted at rest in ``system_secrets``. It is never returned
by any endpoint and never placed in an audit record (audit params are empty,
mirroring the MCP secret endpoints).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.secrets_vault import VaultKeyNotConfiguredError
from services.slack_bot.secrets import SLACK_SECRET_NAMES
from src.repositories import system_secrets_repo
from src.repositories.audit import AuditRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin-slack-secrets"])


class SlackSecretBody(BaseModel):
    value: str


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        AuditRepository(conn).log(
            user_id=actor_id, action=action, resource=resource, params=params or {}
        )
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


@router.get("/slack-secrets")
async def list_slack_secrets(user: dict = Depends(require_admin)):
    """Presence/source status for the three Slack tokens. Never leaks values."""
    repo = system_secrets_repo()
    out = []
    for name in SLACK_SECRET_NAMES:
        if os.environ.get(name):
            source, has_value = "env", True
        elif repo.has(name):
            source, has_value = "vault", True
        else:
            source, has_value = "unset", False
        out.append({"name": name, "source": source, "has_value": has_value})
    return {"secrets": out}


@router.put("/slack-secrets/{name}", status_code=204)
async def set_slack_secret(
    name: str,
    body: SlackSecretBody,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Store (or rotate) the vault secret for ``name``. Write-only."""
    if name not in SLACK_SECRET_NAMES:
        raise HTTPException(status_code=400, detail="unknown_slack_secret")
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    try:
        system_secrets_repo().upsert(name, body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(conn, user["id"], "slack.secret.set", f"slack_secret:{name}", {})


@router.delete("/slack-secrets/{name}", status_code=204)
async def delete_slack_secret(
    name: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Drop the vault row for ``name``. Resolution falls back to env / disabled."""
    if name not in SLACK_SECRET_NAMES:
        raise HTTPException(status_code=400, detail="unknown_slack_secret")
    system_secrets_repo().delete(name)
    _audit(conn, user["id"], "slack.secret.clear", f"slack_secret:{name}", {})
```

- [ ] **Step 4: Register the router** in `app/main.py`. Add the import next to the other admin router imports (~line 242):

```python
from app.api.admin_slack_secrets import router as admin_slack_secrets_router
```

And the include next to `app.include_router(admin_mcp_router)` (~line 1165):

```python
    app.include_router(admin_slack_secrets_router)
```

- [ ] **Step 5: Run the API tests to verify they pass**

Run: `.venv/bin/pytest tests/test_admin_slack_secrets.py --tb=short -q`
Expected: PASS (all 6).

- [ ] **Step 6: Commit**

```bash
git add app/api/admin_slack_secrets.py app/main.py tests/test_admin_slack_secrets.py
git commit -m "feat(admin): /api/admin/slack-secrets vault endpoints"
```

---

## Task 9: Admin UI — Slack secrets section

**Files:**
- Modify: `app/web/templates/admin_server_config.html` (add a section + JS, mirroring the existing `#chat-section` secret-presence pattern at ~line 299)

- [ ] **Step 1: Add the section markup** after the `#chat-section` `</section>` (~line 333). Mirror the chat section's structure (write-only password inputs + presence status, no value rendered):

```html
  <!-- Slack bot secrets — vault-backed (system_secrets), set/rotate/clear from UI.
       Written to the encrypted vault via /api/admin/slack-secrets, NOT to the
       instance.yaml overlay. Status is presence-only; values are never returned. -->
  <section class="cfg-section" id="slack-secrets-section">
    <div class="section-head">
      <div>
        <h3>Slack bot secrets</h3>
        <p class="sub">
          Stored encrypted in the server vault. Environment variables (e.g. set
          via Terraform) always take precedence — when a token shows
          <code>env</code> it is pinned in the environment and cannot be changed
          here. Requires <code>AGNES_VAULT_KEY</code> on the server.
        </p>
      </div>
    </div>
    <div class="section-body" id="slack-secrets-body">
      <div id="slack-secrets-status" class="cfg-loading">Loading…</div>
      <div class="cfg-field">
        <label for="slack-bot-token">SLACK_BOT_TOKEN</label>
        <input type="password" id="slack-bot-token" autocomplete="off"
               placeholder="xoxb-… (leave blank to keep existing)">
      </div>
      <div class="cfg-field">
        <label for="slack-app-token">SLACK_APP_TOKEN</label>
        <input type="password" id="slack-app-token" autocomplete="off"
               placeholder="xapp-… (leave blank to keep existing)">
      </div>
      <div class="cfg-field">
        <label for="slack-signing-secret">SLACK_SIGNING_SECRET</label>
        <input type="password" id="slack-signing-secret" autocomplete="off"
               placeholder="(leave blank to keep existing)">
      </div>
    </div>
    <div class="section-actions">
      <button type="button" class="btn btn-primary" id="slack-secrets-save-btn">Save secrets</button>
    </div>
  </section>
```

- [ ] **Step 2: Add the JS** mirroring the chat-secrets save/refresh logic already in this file. Find the chat-secrets `fetch('/api/admin/chat/secrets', ...)` block and add an analogous one. The save handler PUTs each non-empty input to `/api/admin/slack-secrets/{NAME}`, then refreshes status from `GET /api/admin/slack-secrets`:

```javascript
  const SLACK_FIELDS = [
    ["SLACK_BOT_TOKEN", "slack-bot-token"],
    ["SLACK_APP_TOKEN", "slack-app-token"],
    ["SLACK_SIGNING_SECRET", "slack-signing-secret"],
  ];

  async function refreshSlackSecrets() {
    const el = document.getElementById("slack-secrets-status");
    try {
      const r = await fetch("/api/admin/slack-secrets");
      const data = await r.json();
      el.classList.remove("cfg-loading");
      el.innerHTML = data.secrets.map(s =>
        `<div><code>${s.name}</code>: <strong>${s.source}</strong></div>`
      ).join("");
    } catch (e) {
      el.textContent = "Could not load Slack secret status.";
    }
  }

  document.getElementById("slack-secrets-save-btn").addEventListener("click", async () => {
    let any = false, vaultKeyMissing = false;
    for (const [name, inputId] of SLACK_FIELDS) {
      const v = document.getElementById(inputId).value.trim();
      if (!v) continue;
      any = true;
      const r = await fetch(`/api/admin/slack-secrets/${name}`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({value: v}),
      });
      if (r.status === 409) vaultKeyMissing = true;
      document.getElementById(inputId).value = "";
    }
    const banner = document.getElementById("cfg-banner");
    if (!any) { banner.textContent = "No secret entered."; }
    else if (vaultKeyMissing) { banner.textContent = "AGNES_VAULT_KEY is not configured on the server."; }
    else { banner.textContent = "Slack secrets saved."; }
    await refreshSlackSecrets();
  });

  refreshSlackSecrets();
```

(Adapt selector/banner names to match the file's existing helpers if they differ — follow the chat-section's exact idiom for `fetch`, auth, and banner display.)

- [ ] **Step 3: Verify the design-system contract guard still passes** (the template must not introduce raw hex / `var(--primary)` etc.):

Run: `.venv/bin/pytest tests/test_design_system_contract.py --tb=short -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/admin_server_config.html
git commit -m "feat(web): Slack bot secrets section on /admin/server-config"
```

---

## Task 10: Docs + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (Added bullet under `## [Unreleased]`)
- Modify: `config/.env.template` (note env precedence)
- Modify: `docs/slack-manifest-http.md`, `docs/slack-manifest-socket.md` (mention UI/vault option)

- [ ] **Step 1: CHANGELOG** — add under `## [Unreleased]` → `### Added`:

```markdown
- Slack bot tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`) can now be set and rotated from the admin UI (`/admin/server-config` → Slack bot secrets), stored encrypted in the server vault. Environment variables still take precedence, so Terraform-managed deployments are unaffected. Requires `AGNES_VAULT_KEY` on the server.
```

- [ ] **Step 2: `config/.env.template`** — near the existing Slack vars, add a comment:

```bash
# Slack bot tokens may also be set from the admin UI (stored encrypted in the
# server vault). If set here in the environment, the env value always wins.
```

- [ ] **Step 3: `docs/slack-manifest-http.md` and `docs/slack-manifest-socket.md`** — under "Required environment", add a line to each:

```markdown
- These tokens may instead be set from the admin UI (`/admin/server-config` → Slack bot secrets), stored encrypted in the vault (`AGNES_VAULT_KEY` required). Environment variables, if present, take precedence.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md config/.env.template docs/slack-manifest-http.md docs/slack-manifest-socket.md
git commit -m "docs: Slack bot tokens via admin UI / vault"
```

---

## Task 11: Full suite + release-cut (0.66.0)

**Files:**
- Modify: `pyproject.toml` (version → `0.66.0`)
- Modify: `CHANGELOG.md` (rename `[Unreleased]` → `[0.66.0] - 2026-06-04`; add fresh empty `[Unreleased]`)

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS. Failures in code you touched → fix before proceeding. Pre-existing unrelated failures → confirm on a clean tree (`git stash`), note in PR body, don't block.

- [ ] **Step 2: Bump the version** in `pyproject.toml` (line ~3):

```toml
version = "0.66.0"
```

- [ ] **Step 3: Cut the CHANGELOG** — rename the `## [Unreleased]` heading to `## [0.66.0] - 2026-06-04` and insert a fresh empty section above it:

```markdown
## [Unreleased]

## [0.66.0] - 2026-06-04
```

- [ ] **Step 4: Commit the release-cut** (last commit on the PR):

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore(release): 0.66.0"
```

- [ ] **Step 5: Push + open PR** (only when the user asks to merge — per project workflow the release tag + GitHub Release happen after merge):

```bash
git push -u origin zs/slack-bot-tokens-vault
```

---

## Final verification checklist (run before requesting review)

- [ ] `.venv/bin/pytest tests/ --tb=short -n auto -q` green.
- [ ] `.venv/bin/pytest tests/db_pg/test_system_secrets_contract.py tests/db_pg/test_schema_parity.py tests/db_pg/test_data_migration.py --tb=short -q` green.
- [ ] `grep -rn 'os.environ.get("SLACK_' app/ services/` returns only `services/slack_bot/secrets.py` (the resolver) — all other reads now go through `slack_secret()`.
- [ ] No secret value appears in any API response or audit row (covered by `tests/test_admin_slack_secrets.py`).
- [ ] CHANGELOG has the Added bullet and the release-cut.
