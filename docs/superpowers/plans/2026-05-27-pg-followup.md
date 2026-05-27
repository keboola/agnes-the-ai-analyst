# Postgres app-state follow-up — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 5-repo gap left by PR #388 (`data_packages`, `memory_domains`, `memory_domain_suggestions`, `recipes`, `user_stack_subscriptions`) and make Postgres side-car the standard customer-instance deploy shape.

**Architecture:** Three phases. **Phase 1** ships code parity — 5 SQLAlchemy models, 1 alembic revision (0011 covers 7 tables including 2 bridges), 5 `*_pg.py` repository modules, 5 factory entries, ~10 callsite swaps, and 5 contract test files (52 parametrized tests). **Phase 2** wires the deploy story — Secret Manager triple for `POSTGRES_PASSWORD` in `customer-instance` TF (mirrors existing `jwt` pattern), `startup-script.sh.tpl` updates to pull the secret and enable the overlay by default via `COMPOSE_FILE` env, and `docker-compose.postgres.yml` additions (data-migrate one-shot service, named volume bind to `/data/postgres` for backup-policy coverage). **Phase 3** validates end-to-end and ships.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 + psycopg 3, Alembic 1.13, pytest-postgresql + pgserver, Terraform (Google Cloud provider), Docker Compose, gh CLI.

**Precondition:** Work happens on the `vr/postgres` branch (PR #388's head, currently CONFLICTING with `main` due to drift) inside a dedicated worktree. PR #388 still owes a final rebase before merge; this plan ships **as additional commits on the same branch**, so when PR #388 lands these commits land with it. The plan depends on the 28 PG repos, alembic revisions 0001–0010, the factory in `src/repositories/__init__.py`, and the `docker-compose.postgres.yml` overlay — all already present in `vr/postgres`.

Local branch + worktree for this plan: `git worktree add .claude/worktrees/zs+pg-followup -B zs/pg-followup origin/vr/postgres`. Conflict resolution against `main` is intentionally deferred until the final pre-merge rebase pass (per the PR #388 cleanup precedent — solve it once, at the end, by which time all of these commits are stable).

---

## Spec

This plan implements `docs/superpowers/specs/2026-05-27-pg-followup-design.md`. Open it in another tab while executing; the spec has the rationale and risk register.

---

## File Structure

| Path | Purpose | Phase |
|---|---|---|
| `src/models/data_packages.py` | NEW. `DataPackage`, `DataPackageTable` SQLAlchemy 2.0 models | 1A |
| `src/models/recipes.py` | NEW. `Recipe` model | 1A |
| `src/models/knowledge.py` | EXTEND. `MemoryDomain`, `MemoryDomainItem`, `MemoryDomainSuggestion` models | 1A |
| `src/models/store.py` | EXTEND. `UserStackSubscription` model | 1A |
| `src/models/__init__.py` | EXTEND. Add explicit class imports + `__all__` entries for new model classes (codebase convention; not side-effect `noqa: F401` imports) | 1A |
| `migrations/versions/0011_data_packages_and_memory_extensions.py` | NEW. Alembic revision creating 7 tables + indexes; downgrade in reverse FK order | 1A |
| `src/repositories/data_packages_pg.py` | NEW. `DataPackagesPgRepository` mirrors `data_packages.py` DuckDB shape | 1B |
| `src/repositories/memory_domains_pg.py` | NEW. `MemoryDomainsPgRepository` | 1B |
| `src/repositories/memory_domain_suggestions_pg.py` | NEW. `MemoryDomainSuggestionsPgRepository` | 1B |
| `src/repositories/recipes_pg.py` | NEW. `RecipesPgRepository` | 1B |
| `src/repositories/user_stack_subscriptions_pg.py` | NEW. `UserStackSubscriptionsPgRepository` | 1B |
| `src/repositories/__init__.py` | EXTEND. Add 5 factory funcs, 5 entries in `__all__` | 1C |
| `tests/db_pg/test_data_packages_pg.py` | NEW. PG-side integration tests | 1B |
| `tests/db_pg/test_memory_domains_pg.py` | NEW. PG-side integration tests | 1B |
| `tests/db_pg/test_memory_domain_suggestions_pg.py` | NEW. PG-side integration tests | 1B |
| `tests/db_pg/test_recipes_pg.py` | NEW. PG-side integration tests | 1B |
| `tests/db_pg/test_user_stack_subscriptions_pg.py` | NEW. PG-side integration tests | 1B |
| `tests/db_pg/test_data_packages_contract.py` | NEW. Cross-engine contract — 12 parametrized tests | 1D |
| `tests/db_pg/test_memory_domains_contract.py` | NEW. Cross-engine contract — 12 tests | 1D |
| `tests/db_pg/test_memory_domain_suggestions_contract.py` | NEW. Cross-engine contract — 8 tests | 1D |
| `tests/db_pg/test_recipes_contract.py` | NEW. Cross-engine contract — 10 tests | 1D |
| `tests/db_pg/test_user_stack_subscriptions_contract.py` | NEW. Cross-engine contract — 10 tests | 1D |
| `app/web/router.py` | EDIT. Replace `DataPackagesRepository(conn)` and `MemoryDomainsRepository(conn)` direct usage with factory | 1E |
| `app/api/memory.py` | EDIT. Same pattern for memory_* repos | 1E |
| `app/api/data_packages.py` (if exists) | EDIT. Factory swap | 1E |
| `cli/commands/admin.py`, `cli/commands/memory.py` (if exists) | EDIT. Factory swap | 1E |
| `infra/modules/customer-instance/main.tf` | EXTEND. +Secret Manager triple for postgres password (mirrors `jwt` lines 35–71) | 2A |
| `infra/modules/customer-instance/startup-script.sh.tpl` | EXTEND. Pull `postgres` secret, write `POSTGRES_PASSWORD` + `DATABASE_URL` + `COMPOSE_FILE` to `/opt/agnes/.env`, chown `/data/postgres` to uid 70 | 2A |
| `docker-compose.postgres.yml` | EXTEND. Add `data-migrate` one-shot service, bind `postgres_data` named volume to `/data/postgres` | 2B |
| `docs/postgres-cutover-runbook.md` | NEW. Operator playbook | 2C |
| `CHANGELOG.md` | EDIT. `**BREAKING**` bullet under `[Unreleased]` for the deploy default change | 3 |

All work happens in a new worktree created off fresh `origin/main` after PR #388 merges. Tasks reference `<repo-root>` as the relative path from anywhere inside the worktree.

---

## Validation primitives

### Setup once per shell

```bash
cd <repo-root>
unset UV_PYTHON
source .venv/bin/activate
.venv/bin/python --version       # Expect: Python 3.12.x
```

### Run PG suite

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/ -q --tb=short --timeout=180
```

After this plan: **~317 passed, 1 skipped** (240 baseline from PR #388 + ~52 new contract + ~25 new integration).

### Run DuckDB regression sample

```bash
.venv/bin/pytest tests/test_audit_repository_query.py tests/test_db.py tests/test_users_sso_flag.py tests/test_access_control.py tests/test_admin_configure_api.py -q --timeout=60
```

Expect: 122+ passed, 0 failed. No regressions from this plan's repo additions.

### Validate compose stack

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml config --services
# Expect lines (order may vary): postgres, migrate, data-migrate, app, scheduler
```

### Validate Terraform

```bash
cd infra/modules/customer-instance
terraform init -backend=false
terraform validate
# Expect: "Success! The configuration is valid."
```

---

## Phase 1A — Foundations (models + alembic) {#phase-1a}

### Task 1A.1: DataPackage and DataPackageTable models

**Files:**
- Create: `src/models/data_packages.py`

- [ ] **Step 1: Read existing model conventions**

```bash
sed -n '1,30p' src/models/store.py
sed -n '1,30p' src/models/knowledge.py
```

Identify the imports + base class pattern. Vojta uses `from src.db_pg import Base` and SQLAlchemy 2.0 `Mapped[...]` + `mapped_column` style.

- [ ] **Step 2: Read DuckDB schema for data_packages and bridge**

```bash
sed -n '/CREATE TABLE IF NOT EXISTS data_packages\b/,/);/p' src/db.py
sed -n '/CREATE TABLE IF NOT EXISTS data_package_tables\b/,/);/p' src/db.py
```

Note all columns, types, NULL/NOT NULL, defaults, FK targets.

- [ ] **Step 3: Write the model file**

```python
# src/models/data_packages.py
"""SQLAlchemy 2.0 models for data_packages cluster.

Tables:
  - data_packages: curated package metadata (UI Browse + /catalog)
  - data_package_tables: bridge to table_registry, many-to-many
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class DataPackage(Base):
    __tablename__ = "data_packages"

    id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    slug: Mapped[str] = mapped_column(sa.String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text)
    icon: Mapped[Optional[str]] = mapped_column(sa.String)
    color: Mapped[Optional[str]] = mapped_column(sa.String)
    cover_image_url: Mapped[Optional[str]] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String, server_default=sa.text("'prod'"), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(sa.String)
    owner_name: Mapped[Optional[str]] = mapped_column(sa.String)
    owner_team: Mapped[Optional[str]] = mapped_column(sa.String)
    tags: Mapped[Optional[List[str]]] = mapped_column(JSONB)
    long_description: Mapped[Optional[str]] = mapped_column(sa.Text)
    when_to_use: Mapped[Optional[List[str]]] = mapped_column(JSONB)
    when_not_to_use: Mapped[Optional[List[str]]] = mapped_column(JSONB)
    example_questions: Mapped[Optional[List[str]]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(sa.String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True))


class DataPackageTable(Base):
    __tablename__ = "data_package_tables"

    package_id: Mapped[str] = mapped_column(
        sa.String,
        sa.ForeignKey("data_packages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    table_id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    added_by: Mapped[str] = mapped_column(sa.String, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
```

- [ ] **Step 4: Wire into src/models/__init__.py (explicit class import + __all__)**

Read current `src/models/__init__.py`:

```bash
cat src/models/__init__.py
```

The file uses **explicit class imports + `__all__` entries** (not side-effect `noqa: F401` imports). Add the new classes the same way:

```python
# In the alphabetical import block (after KnowledgeItem, before InstanceTemplate, etc.):
from src.models.data_packages import DataPackage, DataPackageTable

# In __all__, insert alphabetically:
__all__ = [
    ...,
    "DataPackage",
    "DataPackageTable",
    ...,
]
```

- [ ] **Step 5: Verify models register**

```bash
unset UV_PYTHON
.venv/bin/python -c "
from src.db_pg import Base
import src.models  # noqa
tables = sorted(t.name for t in Base.metadata.sorted_tables)
print('data_packages' in tables, 'data_package_tables' in tables)
"
```

Expect: `True True`.

- [ ] **Step 6: Commit**

```bash
git add src/models/data_packages.py src/models/__init__.py
git commit -m "feat(models): add data_packages cluster (data_packages + bridge)

Two new SQLAlchemy 2.0 models for the data_packages cluster — the
DuckDB equivalents already exist in src/repositories/data_packages.py;
this is the PG mirror's model side. Base.metadata.sorted_tables now
picks them up; alembic autogenerate will reference them on drift
check."
```

### Task 1A.2: Recipe model

**Files:**
- Create: `src/models/recipes.py`

- [ ] **Step 1: Read DuckDB schema**

```bash
sed -n '/CREATE TABLE IF NOT EXISTS recipes\b/,/);/p' src/db.py
```

- [ ] **Step 2: Write the model**

```python
# src/models/recipes.py
"""SQLAlchemy 2.0 model for recipes cluster.

Curated analysis recipes (CRUD with soft-delete).
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    slug: Mapped[str] = mapped_column(sa.String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text)
    body: Mapped[Optional[str]] = mapped_column(sa.Text)
    tags: Mapped[Optional[List[str]]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(sa.String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True))
```

Adjust column list to match the actual `CREATE TABLE recipes` from src/db.py — the snippet above is the expected shape, but read the DDL to confirm.

- [ ] **Step 3: Wire into __init__.py**

`src/models/__init__.py` uses **explicit class imports + `__all__` entries** (not side-effect `noqa: F401` imports — see how Task 1A.1 wired `DataPackage` / `DataPackageTable`). Add the same way:

```python
# In the alphabetical import block, add:
from src.models.recipes import Recipe

# In __all__, insert "Recipe" alphabetically:
__all__ = [
    ...,
    "Recipe",
    ...,
]
```

- [ ] **Step 4: Verify**

```bash
.venv/bin/python -c "from src.db_pg import Base; import src.models; print('recipes' in {t.name for t in Base.metadata.sorted_tables})"
```

Expect: `True`.

- [ ] **Step 5: Commit**

```bash
git add src/models/recipes.py src/models/__init__.py
git commit -m "feat(models): add recipes cluster"
```

### Task 1A.3: Extend knowledge.py with memory_domains, knowledge_item_domains, memory_domain_suggestions

**Files:**
- Modify: `src/models/knowledge.py`

- [ ] **Step 1: Read existing knowledge.py to find the append point**

```bash
wc -l src/models/knowledge.py
tail -20 src/models/knowledge.py
```

- [ ] **Step 2: Read DuckDB schemas**

```bash
sed -n '/CREATE TABLE IF NOT EXISTS memory_domains\b/,/);/p' src/db.py
sed -n '/CREATE TABLE IF NOT EXISTS knowledge_item_domains\b/,/);/p' src/db.py
sed -n '/CREATE TABLE IF NOT EXISTS memory_domain_suggestions\b/,/);/p' src/db.py
```

- [ ] **Step 3: Append models to knowledge.py**

```python
# Append to src/models/knowledge.py (after the existing KnowledgeItem etc. classes):

class MemoryDomain(Base):
    __tablename__ = "memory_domains"

    id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    slug: Mapped[str] = mapped_column(sa.String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text)
    icon: Mapped[Optional[str]] = mapped_column(sa.String)
    color: Mapped[Optional[str]] = mapped_column(sa.String)
    created_by: Mapped[Optional[str]] = mapped_column(sa.String)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True))


class MemoryDomainItem(Base):
    __tablename__ = "knowledge_item_domains"

    domain_id: Mapped[str] = mapped_column(
        sa.String,
        sa.ForeignKey("memory_domains.id", ondelete="CASCADE"),
        primary_key=True,
    )
    item_id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    added_by: Mapped[str] = mapped_column(sa.String, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class MemoryDomainSuggestion(Base):
    __tablename__ = "memory_domain_suggestions"

    id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    slug: Mapped[str] = mapped_column(sa.String, nullable=False)
    name: Mapped[str] = mapped_column(sa.String, nullable=False)
    proposed_by: Mapped[str] = mapped_column(sa.String, nullable=False)
    status: Mapped[str] = mapped_column(sa.String, server_default=sa.text("'pending'"), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True))
    resolved_to_domain_id: Mapped[Optional[str]] = mapped_column(sa.String)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
```

Confirm columns/types against the live `CREATE TABLE` output from Step 2 — adjust if your read shows extra columns (e.g., `v55:` comment in DuckDB indicates the resolved_to_domain_id is the merge target).

- [ ] **Step 4: Verify**

```bash
.venv/bin/python -c "
from src.db_pg import Base
import src.models
names = {t.name for t in Base.metadata.sorted_tables}
print(all(t in names for t in ['memory_domains', 'knowledge_item_domains', 'memory_domain_suggestions']))
"
```

Expect: `True`.

- [ ] **Step 5: Commit**

```bash
git add src/models/knowledge.py
git commit -m "feat(models): extend knowledge with memory_domains + suggestions

Three new tables in the knowledge cluster:
  - memory_domains: domain registry (slug + name, soft-delete)
  - knowledge_item_domains: M:N bridge to knowledge_items
  - memory_domain_suggestions: non-admin proposals (pending/approved/rejected)

DuckDB siblings already exist in src/repositories/memory_domains.py
and src/repositories/memory_domain_suggestions.py."
```

### Task 1A.4: Extend store.py with UserStackSubscription

**Files:**
- Modify: `src/models/store.py`

- [ ] **Step 1: Read existing store.py end + DuckDB schema**

```bash
tail -15 src/models/store.py
sed -n '/CREATE TABLE IF NOT EXISTS user_stack_subscriptions\b/,/);/p' src/db.py
```

- [ ] **Step 2: Append model**

```python
# Append to src/models/store.py:

class UserStackSubscription(Base):
    __tablename__ = "user_stack_subscriptions"

    user_id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    resource_type: Mapped[str] = mapped_column(sa.String, primary_key=True)
    resource_id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    subscribed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        sa.Index("idx_user_stack_subscriptions_user", "user_id"),
    )
```

- [ ] **Step 3: Verify**

```bash
.venv/bin/python -c "from src.db_pg import Base; import src.models; print('user_stack_subscriptions' in {t.name for t in Base.metadata.sorted_tables})"
```

Expect: `True`.

- [ ] **Step 4: Commit**

```bash
git add src/models/store.py
git commit -m "feat(models): extend store with user_stack_subscriptions"
```

### Task 1A.5: Add `_PK_COLUMNS` entries for new composite-PK tables

**Files:**
- Modify: `scripts/migrate_duckdb_to_pg/__init__.py`

- [ ] **Step 1: Read existing _PK_COLUMNS map**

```bash
grep -A 30 "_PK_COLUMNS" scripts/migrate_duckdb_to_pg/__init__.py | head -40
```

- [ ] **Step 2: Add entries for new composite-PK tables**

`data_package_tables`, `knowledge_item_domains`, `user_stack_subscriptions` have composite PKs.

Append to the `_PK_COLUMNS` dict:

```python
_PK_COLUMNS = {
    # ... existing entries ...
    "data_package_tables": ("package_id", "table_id"),
    "knowledge_item_domains": ("domain_id", "item_id"),
    "user_stack_subscriptions": ("user_id", "resource_type", "resource_id"),
}
```

- [ ] **Step 3: Verify coverage test passes**

```bash
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_data_migration.py::test_non_id_pk_tables_are_in_pk_columns_map -v --tb=short --timeout=30
```

Expect: PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/migrate_duckdb_to_pg/__init__.py
git commit -m "chore(migration): register composite-PK columns for new bridge tables

data_package_tables, knowledge_item_domains, user_stack_subscriptions
each have multi-column primary keys. The generic copy loop needs the
PK column list to emit ON CONFLICT targets and to compute the SHA-256
checksum during validation."
```

### Task 1A.6: Alembic revision 0011

**Files:**
- Create: `migrations/versions/0011_data_packages_and_memory_extensions.py`

- [ ] **Step 1: Read an existing multi-table revision as template**

```bash
sed -n '1,40p' migrations/versions/0010_knowledge.py
```

Note the header (revision id, down_revision, branch_labels, depends_on) and the `op.create_table(...)` / `op.create_index(...)` shape.

- [ ] **Step 2: Write the upgrade**

```python
# migrations/versions/0011_data_packages_and_memory_extensions.py
"""data_packages cluster + memory extensions + recipes + user_stack_subscriptions

Adds 7 tables that landed on main as DuckDB-only after PR #388's branch
cut. See docs/superpowers/specs/2026-05-27-pg-followup-design.md.

Revision ID: 0011_data_packages_and_memory_extensions
Revises: 0010_knowledge
Create Date: 2026-05-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0011_data_packages_and_memory_extensions"
down_revision: Union[str, None] = "0010_knowledge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── data_packages ──────────────────────────────────────────────
    op.create_table(
        "data_packages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("icon", sa.String()),
        sa.Column("color", sa.String()),
        sa.Column("cover_image_url", sa.Text()),
        sa.Column("status", sa.String(), server_default=sa.text("'prod'"), nullable=False),
        sa.Column("category", sa.String()),
        sa.Column("owner_name", sa.String()),
        sa.Column("owner_team", sa.String()),
        sa.Column("tags", JSONB()),
        sa.Column("long_description", sa.Text()),
        sa.Column("when_to_use", JSONB()),
        sa.Column("when_not_to_use", JSONB()),
        sa.Column("example_questions", JSONB()),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("slug", name="uq_data_packages_slug"),
    )

    op.create_table(
        "data_package_tables",
        sa.Column("package_id", sa.String(), nullable=False),
        sa.Column("table_id", sa.String(), nullable=False),
        sa.Column("added_by", sa.String(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("package_id", "table_id"),
        sa.ForeignKeyConstraint(["package_id"], ["data_packages.id"], ondelete="CASCADE"),
    )

    # ── memory_domains + bridge + suggestions ─────────────────────
    op.create_table(
        "memory_domains",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("icon", sa.String()),
        sa.Column("color", sa.String()),
        sa.Column("created_by", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("slug", name="uq_memory_domains_slug"),
    )

    op.create_table(
        "knowledge_item_domains",
        sa.Column("domain_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("added_by", sa.String(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("domain_id", "item_id"),
        sa.ForeignKeyConstraint(["domain_id"], ["memory_domains.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "memory_domain_suggestions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("proposed_by", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_to_domain_id", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("idx_memory_domain_suggestions_status",
                    "memory_domain_suggestions", ["status"])

    # ── recipes ───────────────────────────────────────────────────
    op.create_table(
        "recipes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("tags", JSONB()),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("slug", name="uq_recipes_slug"),
    )

    # ── user_stack_subscriptions ──────────────────────────────────
    op.create_table(
        "user_stack_subscriptions",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("subscribed_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "resource_type", "resource_id"),
    )
    op.create_index("idx_user_stack_subscriptions_user",
                    "user_stack_subscriptions", ["user_id"])


def downgrade() -> None:
    # Reverse order — bridge tables first to satisfy FK constraints
    op.drop_index("idx_user_stack_subscriptions_user",
                  table_name="user_stack_subscriptions")
    op.drop_table("user_stack_subscriptions")
    op.drop_table("recipes")
    op.drop_index("idx_memory_domain_suggestions_status",
                  table_name="memory_domain_suggestions")
    op.drop_table("memory_domain_suggestions")
    op.drop_table("knowledge_item_domains")
    op.drop_table("memory_domains")
    op.drop_table("data_package_tables")
    op.drop_table("data_packages")
```

Cross-check every column against the DuckDB DDL read in Tasks 1A.1–1A.4.

- [ ] **Step 3: Run drift test**

```bash
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_alembic_roundtrip.py::test_no_model_migration_drift -v --tb=short --timeout=30
```

Expect: PASS. If FAIL, the diff output tells you exactly which column is missing/extra/wrong-typed between model and migration; fix the offending side.

- [ ] **Step 4: Run full chain round-trip**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_alembic_roundtrip.py -v --tb=short --timeout=120
```

Expect: All revisions including 0011 round-trip cleanly.

- [ ] **Step 5: Run full-table coverage test**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_data_migration.py::test_every_pg_model_has_a_migration_task -v --tb=short --timeout=30 || true
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_data_migration.py::test_non_id_pk_tables_are_in_pk_columns_map -v --tb=short --timeout=30
```

Both must pass.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0011_data_packages_and_memory_extensions.py
git commit -m "feat(db): alembic 0011 — data_packages + memory_* + recipes + user_stack_subscriptions

Adds 7 tables to the PG schema chain (5 + 2 bridges). All have
downgrade() coverage; round-trip test (upgrade head → downgrade base
→ upgrade head) green. The generic introspective migration loop in
scripts/migrate_duckdb_to_pg/ picks them up via Base.metadata."
```

---

## Phase 1B — PG repository modules {#phase-1b}

Each repo follows the same TDD pattern: write integration test that exercises CRUD against pgserver → see it fail (module doesn't exist) → write the module → see test pass → commit. Tests live in `tests/db_pg/test_<repo>_pg.py`.

### Task 1B.1: `data_packages_pg.py` (reference implementation)

**Files:**
- Create: `tests/db_pg/test_data_packages_pg.py`
- Create: `src/repositories/data_packages_pg.py`

- [ ] **Step 1: Read the DuckDB sibling for full method shape**

```bash
cat src/repositories/data_packages.py
```

Note: 15 methods (`create`, `get`, `get_by_slug`, `list`, `update`, `delete`, `restore`, `hard_delete`, `add_table`, `remove_table`, `list_tables`, `list_member_ids_bulk`, `_decode_row`).

- [ ] **Step 2: Write integration test (TDD — fails first)**

```python
# tests/db_pg/test_data_packages_pg.py
"""Integration tests for DataPackagesPgRepository.

PG-side smoke. Cross-engine parity covered separately in
test_data_packages_contract.py.
"""
from __future__ import annotations

import pytest

from src.repositories.data_packages_pg import DataPackagesPgRepository


@pytest.fixture
def repo(pg_engine):
    return DataPackagesPgRepository(pg_engine)


def test_create_and_get_by_id_returns_row(repo):
    pkg_id = repo.create(
        name="Sales metrics",
        slug="sales-metrics",
        description="Pack of sales analysis tables",
        icon="📊",
        color="#0ea5e9",
        created_by="admin@example.com",
        tags=["sales", "kpi"],
    )
    row = repo.get(pkg_id)
    assert row is not None
    assert row["slug"] == "sales-metrics"
    assert row["name"] == "Sales metrics"
    assert row["tags"] == ["sales", "kpi"]
    assert row["created_by"] == "admin@example.com"
    assert row["deleted_at"] is None


def test_get_by_slug_resolves_to_same_row(repo):
    pkg_id = repo.create(name="X", slug="x-pkg", description=None, icon=None,
                          color=None, created_by="u")
    by_slug = repo.get_by_slug("x-pkg")
    assert by_slug is not None
    assert by_slug["id"] == pkg_id


def test_delete_then_restore_round_trip(repo):
    pkg_id = repo.create(name="X", slug="x", description=None, icon=None,
                          color=None, created_by="u")
    repo.delete(pkg_id)
    assert repo.get(pkg_id) is None
    assert repo.get(pkg_id, include_deleted=True) is not None
    repo.restore(pkg_id)
    assert repo.get(pkg_id) is not None


def test_add_table_then_list_tables(repo):
    pkg_id = repo.create(name="X", slug="x", description=None, icon=None,
                          color=None, created_by="u")
    added = repo.add_table(pkg_id, "orders", added_by="u")
    assert added is True
    again = repo.add_table(pkg_id, "orders", added_by="u")
    assert again is False  # idempotent
    tables = repo.list_tables(pkg_id)
    assert [t["table_id"] for t in tables] == ["orders"]


def test_list_member_ids_bulk_returns_per_package_lists(repo):
    a = repo.create(name="A", slug="a", description=None, icon=None,
                     color=None, created_by="u")
    b = repo.create(name="B", slug="b", description=None, icon=None,
                     color=None, created_by="u")
    repo.add_table(a, "t1", added_by="u")
    repo.add_table(a, "t2", added_by="u")
    repo.add_table(b, "t3", added_by="u")
    bulk = repo.list_member_ids_bulk()
    assert sorted(bulk[a]) == ["t1", "t2"]
    assert bulk[b] == ["t3"]


def test_update_partial_fields(repo):
    pkg_id = repo.create(name="A", slug="a", description="old", icon=None,
                          color=None, created_by="u")
    repo.update(pkg_id, description="new", tags=["x"])
    row = repo.get(pkg_id)
    assert row["description"] == "new"
    assert row["tags"] == ["x"]
    assert row["name"] == "A"  # untouched
```

**Test fixture pattern (verified by Task 1B.1):** The conftest at `tests/db_pg/conftest.py` exposes only `pg_engine`. There is **NO `_alembic_upgrade_to_head` fixture** — the established convention (see `tests/db_pg/test_store_pg.py`) is to inline the alembic upgrade inside a per-file `repo` fixture:

```python
from pathlib import Path
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine):
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    # ... seed any FK-target tables (table_registry, knowledge_items, users)
    #     that the repo's queries JOIN against — Task 1B.1 seeds three
    #     table_registry rows for data_packages_pg's list_tables JOIN.
    return XYZPgRepository(pg_engine)
```

**Sibling = contract truth.** If the plan template's test or method signature disagrees with the DuckDB sibling in `src/repositories/<table>.py`, follow the sibling — Task 1D's cross-engine contract tests will assert it.

- [ ] **Step 3: Run tests — expect failure (module doesn't exist)**

```bash
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_data_packages_pg.py -v --tb=short --timeout=60 2>&1 | tail -5
```

Expect: collection error or 6 errors — `ModuleNotFoundError: src.repositories.data_packages_pg`.

- [ ] **Step 4: Write the repository module**

```python
# src/repositories/data_packages_pg.py
"""Postgres-backed data packages repository.

Mirrors src/repositories/data_packages.py (DuckDB impl). Dialect
adaptations:
  - INSERT OR IGNORE → INSERT … ON CONFLICT DO NOTHING
  - DuckDB JSON columns → JSONB via CAST(:param AS JSONB)
  - Soft-delete via deleted_at IS NULL filter, identical semantics
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


_JSON_COLUMNS = {"tags", "when_to_use", "when_not_to_use", "example_questions"}


class DataPackagesPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
        # psycopg returns JSONB as native Python (list/dict); no decode needed.
        return dict(row)

    def create(
        self,
        *,
        name: str,
        slug: str,
        description: Optional[str],
        icon: Optional[str],
        color: Optional[str],
        created_by: str,
        cover_image_url: Optional[str] = None,
        status: str = "prod",
        category: Optional[str] = None,
        owner_name: Optional[str] = None,
        owner_team: Optional[str] = None,
        tags: Optional[List[str]] = None,
        long_description: Optional[str] = None,
        when_to_use: Optional[List[str]] = None,
        when_not_to_use: Optional[List[str]] = None,
        example_questions: Optional[List[str]] = None,
    ) -> str:
        pkg_id = "pkg_" + uuid.uuid4().hex[:12]
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("""
                    INSERT INTO data_packages
                      (id, slug, name, description, icon, color, cover_image_url,
                       status, category, owner_name, owner_team,
                       tags, long_description,
                       when_to_use, when_not_to_use, example_questions,
                       created_by)
                    VALUES
                      (:id, :slug, :name, :description, :icon, :color, :cover_image_url,
                       :status, :category, :owner_name, :owner_team,
                       CAST(:tags AS JSONB), :long_description,
                       CAST(:when_to_use AS JSONB), CAST(:when_not_to_use AS JSONB),
                       CAST(:example_questions AS JSONB),
                       :created_by)
                """),
                {
                    "id": pkg_id, "slug": slug, "name": name,
                    "description": description, "icon": icon, "color": color,
                    "cover_image_url": cover_image_url,
                    "status": status or "prod", "category": category,
                    "owner_name": owner_name, "owner_team": owner_team,
                    "tags": json.dumps(tags) if tags is not None else None,
                    "long_description": long_description,
                    "when_to_use": json.dumps(when_to_use) if when_to_use is not None else None,
                    "when_not_to_use": json.dumps(when_not_to_use) if when_not_to_use is not None else None,
                    "example_questions": json.dumps(example_questions) if example_questions is not None else None,
                    "created_by": created_by,
                },
            )
        return pkg_id

    def get(self, pkg_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        where = "id = :id"
        if not include_deleted:
            where += " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(f"SELECT * FROM data_packages WHERE {where}"),
                {"id": pkg_id},
            ).mappings().first()
        return self._decode_row(row) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM data_packages WHERE slug = :slug AND deleted_at IS NULL"),
                {"slug": slug},
            ).mappings().first()
        return self._decode_row(row) if row else None

    def list(
        self,
        *,
        include_deleted: bool = False,
        category: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        clauses = []
        params: Dict[str, Any] = {"limit": limit}
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if category is not None:
            clauses.append("category = :category")
            params["category"] = category
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(f"SELECT * FROM data_packages {where} ORDER BY created_at DESC LIMIT :limit"),
                params,
            ).mappings().all()
        return [self._decode_row(r) for r in rows]

    def update(self, pkg_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = []
        params: Dict[str, Any] = {"id": pkg_id}
        for col, val in fields.items():
            if col in _JSON_COLUMNS and val is not None:
                sets.append(f"{col} = CAST(:{col} AS JSONB)")
                params[col] = json.dumps(val)
            else:
                sets.append(f"{col} = :{col}")
                params[col] = val
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE data_packages SET {', '.join(sets)} WHERE id = :id"),
                params,
            )

    def delete(self, pkg_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE data_packages SET deleted_at = :now WHERE id = :id"),
                {"id": pkg_id, "now": datetime.now(timezone.utc)},
            )

    def restore(self, pkg_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE data_packages SET deleted_at = NULL WHERE id = :id"),
                {"id": pkg_id},
            )

    def hard_delete(self, pkg_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM data_packages WHERE id = :id"),
                {"id": pkg_id},
            )

    def add_table(self, pkg_id: str, table_id: str, *, added_by: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("""
                    INSERT INTO data_package_tables (package_id, table_id, added_by)
                    VALUES (:p, :t, :u)
                    ON CONFLICT (package_id, table_id) DO NOTHING
                """),
                {"p": pkg_id, "t": table_id, "u": added_by},
            )
            return result.rowcount > 0

    def remove_table(self, pkg_id: str, table_id: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM data_package_tables WHERE package_id = :p AND table_id = :t"),
                {"p": pkg_id, "t": table_id},
            )
            return result.rowcount > 0

    def list_tables(self, pkg_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("""
                    SELECT * FROM data_package_tables
                    WHERE package_id = :p
                    ORDER BY added_at
                """),
                {"p": pkg_id},
            ).mappings().all()
        return [dict(r) for r in rows]

    def list_member_ids_bulk(self) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT package_id, table_id FROM data_package_tables ORDER BY package_id, added_at")
            ).all()
        for pkg_id, table_id in rows:
            result.setdefault(pkg_id, []).append(table_id)
        return result
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_data_packages_pg.py -v --tb=short --timeout=60 2>&1 | tail -10
```

Expect: `6 passed`.

- [ ] **Step 6: Commit**

```bash
git add tests/db_pg/test_data_packages_pg.py src/repositories/data_packages_pg.py
git commit -m "feat(repos): data_packages_pg.py mirrors DuckDB impl

Postgres-backed data packages repo + 6 integration tests covering CRUD,
soft-delete round-trip, bridge ops, and bulk listing. Dialect adaptations
documented in module docstring; JSONB casts explicit for tags + extended
content fields."
```

### Task 1B.2: `memory_domains_pg.py`

**Files:**
- Create: `tests/db_pg/test_memory_domains_pg.py`
- Create: `src/repositories/memory_domains_pg.py`

- [ ] **Step 1: Read DuckDB sibling for full method shape**

```bash
cat src/repositories/memory_domains.py
```

Note: 14 methods. Same CRUD + soft-delete + slug ops + bridge (`add_item` / `remove_item` / `list_items_of_domain` / `list_domains_of_item`).

- [ ] **Step 2: Write integration test mirroring data_packages structure**

Use the test file from Task 1B.1 as template (`tests/db_pg/test_data_packages_pg.py`). Adapt to memory_domains:

```python
# tests/db_pg/test_memory_domains_pg.py
from __future__ import annotations
import pytest
from src.repositories.memory_domains_pg import MemoryDomainsPgRepository


@pytest.fixture
def repo(pg_engine):
    return MemoryDomainsPgRepository(pg_engine)


def test_create_and_get_by_id(repo):
    did = repo.create(name="Sales", slug="sales", description=None,
                       icon=None, color=None, created_by="u")
    row = repo.get(did)
    assert row["slug"] == "sales"

def test_get_by_slug(repo):
    did = repo.create(name="X", slug="x", description=None, icon=None,
                       color=None, created_by="u")
    assert repo.get_by_slug("x")["id"] == did

def test_exists_by_slug_returns_bool(repo):
    repo.create(name="X", slug="x", description=None, icon=None,
                color=None, created_by="u")
    assert repo.exists_by_slug("x") is True
    assert repo.exists_by_slug("nope") is False

def test_delete_restore(repo):
    did = repo.create(name="X", slug="x", description=None, icon=None,
                       color=None, created_by="u")
    repo.delete(did)
    assert repo.get(did) is None
    repo.restore(did)
    assert repo.get(did) is not None

def test_add_item_then_list_items_of_domain(repo):
    did = repo.create(name="Sales", slug="sales", description=None, icon=None,
                       color=None, created_by="u")
    repo.add_item(did, "ki_1", added_by="u")
    repo.add_item(did, "ki_2", added_by="u")
    rows = repo.list_items_of_domain(did)
    assert sorted(r["item_id"] for r in rows) == ["ki_1", "ki_2"]

def test_list_domains_of_item_pivots_correctly(repo):
    a = repo.create(name="A", slug="a", description=None, icon=None, color=None, created_by="u")
    b = repo.create(name="B", slug="b", description=None, icon=None, color=None, created_by="u")
    repo.add_item(a, "ki_1", added_by="u")
    repo.add_item(b, "ki_1", added_by="u")
    domains = repo.list_domains_of_item("ki_1")
    assert sorted(d["id"] for d in domains) == sorted([a, b])
```

- [ ] **Step 3: Run — expect failure (no module yet)**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_memory_domains_pg.py -v --tb=line --timeout=60 2>&1 | tail -5
```

Expect: `ModuleNotFoundError` or collection error.

- [ ] **Step 4: Write `src/repositories/memory_domains_pg.py`**

Mirror the shape of `data_packages_pg.py` from Task 1B.1. Tables are `memory_domains` (parent) and `knowledge_item_domains` (bridge). No JSONB columns. ID format `"dom_" + uuid.uuid4().hex[:12]`. Methods match the DuckDB sibling: `create`, `get`, `get_by_slug`, `exists_by_slug`, `list`, `update`, `delete`, `restore`, `hard_delete`, `add_item`, `remove_item`, `list_items_of_domain`, `list_domains_of_item`. Use `ON CONFLICT DO NOTHING` for `add_item` idempotency.

The `list_domains_of_item` joins through the bridge:

```python
def list_domains_of_item(self, item_id: str) -> List[Dict[str, Any]]:
    with self._engine.connect() as conn:
        rows = conn.execute(
            sa.text("""
                SELECT d.* FROM memory_domains d
                JOIN knowledge_item_domains b ON b.domain_id = d.id
                WHERE b.item_id = :item AND d.deleted_at IS NULL
                ORDER BY d.created_at
            """),
            {"item": item_id},
        ).mappings().all()
    return [dict(r) for r in rows]
```

Adapt remaining methods directly from the data_packages_pg.py template — replace table names, drop JSONB-specific casts, and adjust the bridge table reference.

- [ ] **Step 5: Run — expect PASS**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_memory_domains_pg.py -v --tb=short --timeout=60 2>&1 | tail -10
```

Expect: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/db_pg/test_memory_domains_pg.py src/repositories/memory_domains_pg.py
git commit -m "feat(repos): memory_domains_pg.py"
```

### Task 1B.3: `memory_domain_suggestions_pg.py`

**Files:**
- Create: `tests/db_pg/test_memory_domain_suggestions_pg.py`
- Create: `src/repositories/memory_domain_suggestions_pg.py`

- [ ] **Step 1: Read DuckDB sibling**

```bash
cat src/repositories/memory_domain_suggestions.py
```

Methods: `create`, `get`, `list`, `count_pending`, `resolve`, `_row_to_dict`.

- [ ] **Step 2: Write integration test**

```python
# tests/db_pg/test_memory_domain_suggestions_pg.py
from __future__ import annotations
import pytest
from src.repositories.memory_domain_suggestions_pg import MemoryDomainSuggestionsPgRepository


@pytest.fixture
def repo(pg_engine):
    return MemoryDomainSuggestionsPgRepository(pg_engine)


def test_create_then_get(repo):
    sid = repo.create(slug="finance", name="Finance", proposed_by="alice@x")
    row = repo.get(sid)
    assert row["slug"] == "finance"
    assert row["status"] == "pending"

def test_count_pending(repo):
    repo.create(slug="a", name="A", proposed_by="u")
    repo.create(slug="b", name="B", proposed_by="u")
    assert repo.count_pending() == 2

def test_resolve_to_approved(repo):
    sid = repo.create(slug="x", name="X", proposed_by="u")
    repo.resolve(sid, status="approved", resolved_to_domain_id="dom_abc")
    row = repo.get(sid)
    assert row["status"] == "approved"
    assert row["resolved_to_domain_id"] == "dom_abc"
    assert row["resolved_at"] is not None

def test_list_filtered_by_status(repo):
    a = repo.create(slug="a", name="A", proposed_by="u")
    b = repo.create(slug="b", name="B", proposed_by="u")
    repo.resolve(a, status="approved", resolved_to_domain_id="dom_x")
    pending = repo.list(status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == b
```

- [ ] **Step 3: Run — expect failure**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_memory_domain_suggestions_pg.py -v --tb=line --timeout=60 2>&1 | tail -5
```

- [ ] **Step 4: Write `src/repositories/memory_domain_suggestions_pg.py`**

Mirror the DuckDB sibling. Methods: `create` (id = `"sug_" + uuid.uuid4().hex[:12]`), `get`, `list(status=None, limit=200)`, `count_pending` (`SELECT COUNT(*) FROM memory_domain_suggestions WHERE status = 'pending'`), `resolve(sid, status, resolved_to_domain_id=None)` (UPDATE setting status + resolved_at + resolved_to_domain_id). Use the same Engine + sa.text pattern as data_packages_pg.py.

- [ ] **Step 5: Run — expect PASS**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_memory_domain_suggestions_pg.py -v --tb=short --timeout=60 2>&1 | tail -10
```

Expect: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/db_pg/test_memory_domain_suggestions_pg.py src/repositories/memory_domain_suggestions_pg.py
git commit -m "feat(repos): memory_domain_suggestions_pg.py"
```

### Task 1B.4: `recipes_pg.py`

**Files:**
- Create: `tests/db_pg/test_recipes_pg.py`
- Create: `src/repositories/recipes_pg.py`

- [ ] **Step 1: Read DuckDB sibling**

```bash
cat src/repositories/recipes.py
```

Methods: `create`, `get`, `get_by_slug`, `list(search=None, limit=200)`, `update`, `delete`, `restore`, `hard_delete`. The JSONB column is `related_table_ids`. DDL columns: `id, slug, title, description, icon, color, sql_template, related_table_ids, status, deleted_at, created_by, created_at, updated_at` (verified against `src/db.py` and the DuckDB sibling's `create()` signature). ID prefix is `rcp_`.

- [ ] **Step 2: Write integration test**

```python
# tests/db_pg/test_recipes_pg.py
from __future__ import annotations
import pytest
from src.repositories.recipes_pg import RecipesPgRepository


@pytest.fixture
def repo(pg_engine):
    return RecipesPgRepository(pg_engine)


def test_create_and_get(repo):
    rid = repo.create(slug="top-customers", title="Top customers",
                       description="Find top N customers by revenue",
                       icon=None, color=None,
                       sql_template="SELECT customer_id, SUM(revenue) ...",
                       related_table_ids=["orders", "customers"],
                       created_by="u")
    row = repo.get(rid)
    assert row["slug"] == "top-customers"
    assert row["title"] == "Top customers"
    assert row["related_table_ids"] == ["orders", "customers"]

def test_get_by_slug(repo):
    rid = repo.create(slug="x", title="X", description=None,
                       icon=None, color=None, sql_template=None,
                       related_table_ids=None, created_by="u")
    assert repo.get_by_slug("x")["id"] == rid

def test_list_search_filters_by_title(repo):
    repo.create(slug="a", title="Top customers", description=None,
                icon=None, color=None, sql_template=None,
                related_table_ids=None, created_by="u")
    repo.create(slug="b", title="Churn analysis", description=None,
                icon=None, color=None, sql_template=None,
                related_table_ids=None, created_by="u")
    matches = repo.list(search="customers")
    assert len(matches) == 1
    assert matches[0]["slug"] == "a"

def test_delete_restore(repo):
    rid = repo.create(slug="x", title="X", description=None,
                       icon=None, color=None, sql_template=None,
                       related_table_ids=None, created_by="u")
    repo.delete(rid)
    assert repo.get(rid) is None
    repo.restore(rid)
    assert repo.get(rid) is not None

def test_update_partial_with_related_table_ids_jsonb(repo):
    rid = repo.create(slug="x", title="X", description="old",
                       icon=None, color=None, sql_template=None,
                       related_table_ids=None, created_by="u")
    repo.update(rid, description="new", related_table_ids=["orders"])
    row = repo.get(rid)
    assert row["description"] == "new"
    assert row["related_table_ids"] == ["orders"]
```

- [ ] **Step 3: Run — expect failure**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_recipes_pg.py -v --tb=line --timeout=60 2>&1 | tail -5
```

- [ ] **Step 4: Write `src/repositories/recipes_pg.py`**

Mirror data_packages_pg.py shape. `_JSON_COLUMNS = {"related_table_ids"}`. ID format `"rcp_" + uuid.uuid4().hex[:12]`. Methods match the DuckDB sibling. `list(search=...)` uses `WHERE title ILIKE :pattern` with `pattern = f"%{search}%"`.

- [ ] **Step 5: Run — expect PASS**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_recipes_pg.py -v --tb=short --timeout=60 2>&1 | tail -10
```

Expect: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/db_pg/test_recipes_pg.py src/repositories/recipes_pg.py
git commit -m "feat(repos): recipes_pg.py"
```

### Task 1B.5: `user_stack_subscriptions_pg.py`

**Files:**
- Create: `tests/db_pg/test_user_stack_subscriptions_pg.py`
- Create: `src/repositories/user_stack_subscriptions_pg.py`

- [ ] **Step 1: Read DuckDB sibling**

```bash
cat src/repositories/user_stack_subscriptions.py
```

Methods: `subscribe`, `unsubscribe`, `is_subscribed`, `list_for_user(user_id, resource_type)`, `list_users_subscribed_to(resource_type, resource_id)`. Composite PK `(user_id, resource_type, resource_id)`.

- [ ] **Step 2: Write integration test**

```python
# tests/db_pg/test_user_stack_subscriptions_pg.py
from __future__ import annotations
import pytest
from src.repositories.user_stack_subscriptions_pg import UserStackSubscriptionsPgRepository


@pytest.fixture
def repo(pg_engine):
    return UserStackSubscriptionsPgRepository(pg_engine)


def test_subscribe_then_is_subscribed(repo):
    repo.subscribe("user_a", "data_package", "pkg_1")
    assert repo.is_subscribed("user_a", "data_package", "pkg_1") is True
    assert repo.is_subscribed("user_a", "data_package", "pkg_other") is False

def test_subscribe_is_idempotent(repo):
    repo.subscribe("user_a", "data_package", "pkg_1")
    repo.subscribe("user_a", "data_package", "pkg_1")  # no exception
    assert repo.is_subscribed("user_a", "data_package", "pkg_1") is True

def test_unsubscribe(repo):
    repo.subscribe("user_a", "data_package", "pkg_1")
    repo.unsubscribe("user_a", "data_package", "pkg_1")
    assert repo.is_subscribed("user_a", "data_package", "pkg_1") is False

def test_list_for_user_filters_by_type(repo):
    repo.subscribe("u", "data_package", "pkg_1")
    repo.subscribe("u", "data_package", "pkg_2")
    repo.subscribe("u", "recipe", "rec_1")
    result = repo.list_for_user("u", "data_package")
    assert sorted(result) == ["pkg_1", "pkg_2"]

def test_list_users_subscribed_to(repo):
    repo.subscribe("alice", "data_package", "pkg_1")
    repo.subscribe("bob", "data_package", "pkg_1")
    repo.subscribe("alice", "data_package", "pkg_2")
    users = repo.list_users_subscribed_to("data_package", "pkg_1")
    assert sorted(users) == ["alice", "bob"]
```

- [ ] **Step 3: Run — expect failure**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_user_stack_subscriptions_pg.py -v --tb=line --timeout=60 2>&1 | tail -5
```

- [ ] **Step 4: Write `src/repositories/user_stack_subscriptions_pg.py`**

Shape:

```python
class UserStackSubscriptionsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def subscribe(self, user_id: str, resource_type: str, resource_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("""
                    INSERT INTO user_stack_subscriptions (user_id, resource_type, resource_id)
                    VALUES (:u, :t, :r)
                    ON CONFLICT (user_id, resource_type, resource_id) DO NOTHING
                """),
                {"u": user_id, "t": resource_type, "r": resource_id},
            )

    def unsubscribe(self, user_id: str, resource_type: str, resource_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("""
                    DELETE FROM user_stack_subscriptions
                    WHERE user_id = :u AND resource_type = :t AND resource_id = :r
                """),
                {"u": user_id, "t": resource_type, "r": resource_id},
            )

    def is_subscribed(self, user_id: str, resource_type: str, resource_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("""
                    SELECT 1 FROM user_stack_subscriptions
                    WHERE user_id = :u AND resource_type = :t AND resource_id = :r
                """),
                {"u": user_id, "t": resource_type, "r": resource_id},
            ).first()
        return row is not None

    def list_for_user(self, user_id: str, resource_type: str) -> List[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("""
                    SELECT resource_id FROM user_stack_subscriptions
                    WHERE user_id = :u AND resource_type = :t
                    ORDER BY subscribed_at
                """),
                {"u": user_id, "t": resource_type},
            ).all()
        return [r[0] for r in rows]

    def list_users_subscribed_to(self, resource_type: str, resource_id: str) -> List[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("""
                    SELECT user_id FROM user_stack_subscriptions
                    WHERE resource_type = :t AND resource_id = :r
                    ORDER BY subscribed_at
                """),
                {"t": resource_type, "r": resource_id},
            ).all()
        return [r[0] for r in rows]
```

Wrap with module imports:

```python
from __future__ import annotations
from typing import List
import sqlalchemy as sa
from sqlalchemy.engine import Engine
```

- [ ] **Step 5: Run — expect PASS**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_user_stack_subscriptions_pg.py -v --tb=short --timeout=60 2>&1 | tail -10
```

Expect: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/db_pg/test_user_stack_subscriptions_pg.py src/repositories/user_stack_subscriptions_pg.py
git commit -m "feat(repos): user_stack_subscriptions_pg.py"
```

---

## Phase 1C — Factory entries {#phase-1c}

### Task 1C.1: Wire all 5 factories

**Files:**
- Modify: `src/repositories/__init__.py`

- [ ] **Step 1: Read existing factory shape**

```bash
sed -n '1,50p' src/repositories/__init__.py
grep -n "_repo\b" src/repositories/__init__.py | head -10
```

- [ ] **Step 2: Add 5 factory funcs + __all__ entries**

Append before the final closing of the file (or after existing factories — whatever the file convention is):

```python
# ── New in this PR (data_packages cluster + memory extensions + recipes + subscriptions) ──

def data_packages_repo() -> Any:
    if use_pg():
        from src.repositories.data_packages_pg import DataPackagesPgRepository
        return DataPackagesPgRepository(_pg_engine())
    from src.repositories.data_packages import DataPackagesRepository
    return DataPackagesRepository(get_system_db())


def memory_domains_repo() -> Any:
    if use_pg():
        from src.repositories.memory_domains_pg import MemoryDomainsPgRepository
        return MemoryDomainsPgRepository(_pg_engine())
    from src.repositories.memory_domains import MemoryDomainsRepository
    return MemoryDomainsRepository(get_system_db())


def memory_domain_suggestions_repo() -> Any:
    if use_pg():
        from src.repositories.memory_domain_suggestions_pg import MemoryDomainSuggestionsPgRepository
        return MemoryDomainSuggestionsPgRepository(_pg_engine())
    from src.repositories.memory_domain_suggestions import MemoryDomainSuggestionsRepository
    return MemoryDomainSuggestionsRepository(get_system_db())


def recipes_repo() -> Any:
    if use_pg():
        from src.repositories.recipes_pg import RecipesPgRepository
        return RecipesPgRepository(_pg_engine())
    from src.repositories.recipes import RecipesRepository
    return RecipesRepository(get_system_db())


def user_stack_subscriptions_repo() -> Any:
    if use_pg():
        from src.repositories.user_stack_subscriptions_pg import UserStackSubscriptionsPgRepository
        return UserStackSubscriptionsPgRepository(_pg_engine())
    from src.repositories.user_stack_subscriptions import UserStackSubscriptionsRepository
    return UserStackSubscriptionsRepository(get_system_db())
```

Add the same 5 entries to the `__all__` list:

```python
__all__ = [
    ...,  # existing 33 entries
    "data_packages_repo",
    "memory_domains_repo",
    "memory_domain_suggestions_repo",
    "recipes_repo",
    "user_stack_subscriptions_repo",
]
```

- [ ] **Step 3: Verify factory works on both backends**

```bash
unset UV_PYTHON
.venv/bin/python -c "
import os
os.environ.pop('DATABASE_URL', None)
os.environ.pop('AGNES_DB_URL', None)
from src.repositories import data_packages_repo, memory_domains_repo, recipes_repo
print('DuckDB path:', type(data_packages_repo()).__name__)
"
```

Expect: `DuckDB path: DataPackagesRepository`.

```bash
# Quick smoke against PG via pgserver
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_repository_factory.py -v --tb=short --timeout=60 2>&1 | tail -10
```

Expect: all factory tests pass (existing PR #388 tests cover the pattern; new entries follow same shape).

- [ ] **Step 4: Commit**

```bash
git add src/repositories/__init__.py
git commit -m "feat(repos): wire 5 new factories — data_packages, memory_*, recipes, subscriptions"
```

---

## Phase 1D — Cross-engine contract tests {#phase-1d}

Five new test files, one per repo cluster. Pattern from PR #388 `tests/db_pg/test_users_contract.py`.

### Task 1D.1: `test_data_packages_contract.py`

**Files:**
- Create: `tests/db_pg/test_data_packages_contract.py`

- [ ] **Step 1: Read existing contract test as template**

```bash
sed -n '1,80p' tests/db_pg/test_users_contract.py
```

Note the `_make_duckdb_repo` and `_make_pg_repos` inline helpers and the `@pytest.fixture(params=["duckdb", "pg"])` pattern.

- [ ] **Step 2: Write the contract test**

```python
# tests/db_pg/test_data_packages_contract.py
"""Cross-engine contract for data_packages — DuckDB and PG must agree.

Same setup, same inputs against DuckDB and PG — outputs must match.
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path

import duckdb
import pytest

# Migration setup helpers (copied from test_users_contract.py — kept inline
# per the file-local contract-test convention in tests/db_pg/).


def _make_duckdb_repo(tmp_path: Path):
    """Boot a fresh DuckDB and return DataPackagesRepository."""
    from src.db import _ensure_schema
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    from src.repositories.data_packages import DataPackagesRepository
    return DataPackagesRepository(conn), conn


def _make_pg_repo(pg_engine):
    from src.repositories.data_packages_pg import DataPackagesPgRepository
    return DataPackagesPgRepository(pg_engine), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, pg_engine, tmp_path):
    if request.param == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine)
        yield repo


def test_create_then_get_returns_same_shape(repo):
    pkg_id = repo.create(name="X", slug="x", description="d", icon=None,
                          color=None, created_by="u")
    row = repo.get(pkg_id)
    assert row["slug"] == "x"
    assert row["name"] == "X"
    assert row["description"] == "d"
    assert row["created_by"] == "u"

def test_get_by_slug_consistent(repo):
    pkg_id = repo.create(name="A", slug="a", description=None, icon=None,
                          color=None, created_by="u")
    assert repo.get_by_slug("a")["id"] == pkg_id
    assert repo.get_by_slug("missing") is None

def test_delete_filters_out_of_default_list(repo):
    pkg_id = repo.create(name="X", slug="x", description=None, icon=None,
                          color=None, created_by="u")
    repo.delete(pkg_id)
    assert all(r["id"] != pkg_id for r in repo.list())
    assert any(r["id"] == pkg_id for r in repo.list(include_deleted=True))

def test_add_table_idempotent(repo):
    pkg_id = repo.create(name="X", slug="x", description=None, icon=None,
                          color=None, created_by="u")
    assert repo.add_table(pkg_id, "t1", added_by="u") is True
    assert repo.add_table(pkg_id, "t1", added_by="u") is False

def test_list_member_ids_bulk_returns_dict(repo):
    a = repo.create(name="A", slug="a", description=None, icon=None,
                     color=None, created_by="u")
    repo.add_table(a, "t1", added_by="u")
    bulk = repo.list_member_ids_bulk()
    assert bulk[a] == ["t1"]

def test_update_tags_jsonb_round_trip(repo):
    pkg_id = repo.create(name="X", slug="x", description=None, icon=None,
                          color=None, created_by="u", tags=["a", "b"])
    repo.update(pkg_id, tags=["c"])
    row = repo.get(pkg_id)
    assert row["tags"] == ["c"]
```

- [ ] **Step 3: Run — expect PASS on both backends**

```bash
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_data_packages_contract.py -v --tb=short --timeout=120 2>&1 | tail -15
```

Expect: 12 passed (6 cases × 2 backends).

If a case fails on one engine but passes on the other, **stop and investigate** — that's a real contract drift bug, not a test issue.

- [ ] **Step 4: Commit**

```bash
git add tests/db_pg/test_data_packages_contract.py
git commit -m "test: cross-engine contract for data_packages (12 parametrized tests)"
```

### Task 1D.2: `test_memory_domains_contract.py`

**Files:**
- Create: `tests/db_pg/test_memory_domains_contract.py`

- [ ] **Step 1: Write contract test using the pattern from Task 1D.1**

Same fixture shape (`_make_duckdb_repo`, `_make_pg_repo`, parametrized fixture). Adapt to MemoryDomainsRepository / MemoryDomainsPgRepository. 6 cases:

```python
def test_create_then_get_consistent(repo): ...
def test_get_by_slug_consistent(repo): ...
def test_exists_by_slug_consistent(repo): ...
def test_delete_round_trip(repo): ...
def test_add_item_idempotent(repo): ...
def test_list_domains_of_item_joins_correctly(repo): ...
```

Each test asserts identical behavior; reuse the bodies from `test_memory_domains_pg.py` (Task 1B.2) but wrap with parametrized `repo` fixture.

- [ ] **Step 2: Run — expect 12 passed**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_memory_domains_contract.py -v --tb=short --timeout=120 2>&1 | tail -15
```

- [ ] **Step 3: Commit**

```bash
git add tests/db_pg/test_memory_domains_contract.py
git commit -m "test: cross-engine contract for memory_domains (12 parametrized tests)"
```

### Task 1D.3: `test_memory_domain_suggestions_contract.py`

**Files:**
- Create: `tests/db_pg/test_memory_domain_suggestions_contract.py`

- [ ] **Step 1: Write 4 parametrized cases**

```python
def test_create_then_get(repo): ...
def test_count_pending(repo): ...
def test_resolve_to_approved(repo): ...
def test_list_filtered_by_status(repo): ...
```

Use the fixture pattern from Task 1D.1, adapt to MemoryDomainSuggestionsRepository / MemoryDomainSuggestionsPgRepository.

- [ ] **Step 2: Run — expect 8 passed**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_memory_domain_suggestions_contract.py -v --tb=short --timeout=120 2>&1 | tail -15
```

- [ ] **Step 3: Commit**

```bash
git add tests/db_pg/test_memory_domain_suggestions_contract.py
git commit -m "test: cross-engine contract for memory_domain_suggestions (8 tests)"
```

### Task 1D.4: `test_recipes_contract.py`

**Files:**
- Create: `tests/db_pg/test_recipes_contract.py`

- [ ] **Step 1: Write 5 cases**

```python
def test_create_then_get(repo): ...
def test_get_by_slug(repo): ...
def test_search_filters_by_title(repo): ...
def test_delete_restore(repo): ...
def test_update_related_table_ids_jsonb_round_trip(repo): ...
```

Use the fixture pattern from Task 1D.1. Adapt to recipes repos. **Recipes DDL field names:** `title` (not `name`), `sql_template` (not `body`), `related_table_ids` (not `tags`). ID prefix `rcp_`. The DuckDB sibling's `create()` keyword signature is the source of truth: `slug, title, description, icon, color, sql_template, related_table_ids, status='prod', created_by=None`.

- [ ] **Step 2: Run — expect 10 passed**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_recipes_contract.py -v --tb=short --timeout=120 2>&1 | tail -15
```

- [ ] **Step 3: Commit**

```bash
git add tests/db_pg/test_recipes_contract.py
git commit -m "test: cross-engine contract for recipes (10 tests)"
```

### Task 1D.5: `test_user_stack_subscriptions_contract.py`

**Files:**
- Create: `tests/db_pg/test_user_stack_subscriptions_contract.py`

- [ ] **Step 1: Write 5 cases**

```python
def test_subscribe_then_is_subscribed(repo): ...
def test_subscribe_idempotent(repo): ...
def test_unsubscribe(repo): ...
def test_list_for_user_filtered_by_type(repo): ...
def test_list_users_subscribed_to(repo): ...
```

- [ ] **Step 2: Run — expect 10 passed**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_user_stack_subscriptions_contract.py -v --tb=short --timeout=120 2>&1 | tail -15
```

- [ ] **Step 3: Full PG suite snapshot**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/ -q --tb=line --timeout=180 2>&1 | tail -3
```

Expect: roughly **240 (baseline) + 25 integration (5×5) + 52 contract (12+12+8+10+10) ≈ 317 passed, 1 skipped**.

- [ ] **Step 4: Commit**

```bash
git add tests/db_pg/test_user_stack_subscriptions_contract.py
git commit -m "test: cross-engine contract for user_stack_subscriptions (10 tests)

Suite total now ~317 passed."
```

---

## Phase 1E — Callsite swap {#phase-1e}

Pattern: replace direct `XYZRepository(conn)` imports with factory calls. Each file is one commit.

### Task 1E.1: Swap callsites in `app/web/router.py`

**Files:**
- Modify: `app/web/router.py`

- [ ] **Step 1: Find direct usage**

```bash
grep -n "DataPackagesRepository\|MemoryDomainsRepository\|MemoryDomainSuggestionsRepository\|RecipesRepository\|UserStackSubscriptionsRepository" app/web/router.py
```

- [ ] **Step 2: For each call, swap pattern**

Example existing line:
```python
pkg_repo = DataPackagesRepository(conn)
```
Replace with:
```python
pkg_repo = data_packages_repo()
```

For each repo, add an import at the top of file (if not already present, alongside other `_repo` imports):

```python
from src.repositories import (
    ...,
    data_packages_repo,
    memory_domains_repo,
    memory_domain_suggestions_repo,
    recipes_repo,
    user_stack_subscriptions_repo,
)
```

And remove the now-unused direct imports:
```python
# DELETE if no other references remain in this file:
from src.repositories.data_packages import DataPackagesRepository
from src.repositories.memory_domains import MemoryDomainsRepository
# etc.
```

- [ ] **Step 3: Verify no markers left**

```bash
grep -n "DataPackagesRepository\b\|MemoryDomainsRepository\b\|MemoryDomainSuggestionsRepository\b\|RecipesRepository\b\|UserStackSubscriptionsRepository\b" app/web/router.py
```

Expect: empty (or only inside docstrings/comments).

- [ ] **Step 4: Smoke test**

```bash
.venv/bin/pytest tests/test_db.py -q --timeout=30 2>&1 | tail -3
```

Expect: no regressions.

- [ ] **Step 5: Commit**

```bash
git add app/web/router.py
git commit -m "refactor(api): app/web/router.py uses 5 new factories"
```

### Task 1E.2: Swap callsites in `app/api/memory.py`

**Files:**
- Modify: `app/api/memory.py`

- [ ] **Step 1: Find direct usage**

```bash
grep -n "MemoryDomainsRepository\|MemoryDomainSuggestionsRepository\|KnowledgeRepository" app/api/memory.py
```

- [ ] **Step 2: Swap with the same pattern as Task 1E.1**

Add factory imports:
```python
from src.repositories import (
    memory_domains_repo,
    memory_domain_suggestions_repo,
)
```
Replace `MemoryDomainsRepository(conn)` → `memory_domains_repo()` and `MemoryDomainSuggestionsRepository(conn)` → `memory_domain_suggestions_repo()`. Remove unused direct imports.

- [ ] **Step 3: Verify clean**

```bash
grep -n "MemoryDomainsRepository\b\|MemoryDomainSuggestionsRepository\b" app/api/memory.py
```

Expect: empty.

- [ ] **Step 4: Smoke test**

```bash
.venv/bin/pytest tests/test_admin_configure_api.py tests/test_db.py -q --timeout=30 2>&1 | tail -3
```

Expect: green.

- [ ] **Step 5: Commit**

```bash
git add app/api/memory.py
git commit -m "refactor(api): app/api/memory.py uses memory_* factories"
```

### Task 1E.3: Audit other callsites + swap

**Files:** all files in `app/` and `cli/` matching the 5 repo class names.

- [ ] **Step 1: Find any remaining direct usage outside src/repositories/**

```bash
grep -rln "DataPackagesRepository\|MemoryDomainsRepository\|MemoryDomainSuggestionsRepository\|RecipesRepository\|UserStackSubscriptionsRepository" app/ cli/ services/ 2>/dev/null
```

- [ ] **Step 2: Swap each file using same pattern**

For each file in the list, repeat the Task 1E.1 / 1E.2 pattern: add factory imports at top, replace `XYZRepository(conn)` instantiations with `xyz_repo()`, remove unused direct imports.

- [ ] **Step 3: Confirm zero direct usage in app/services/cli**

```bash
grep -rln "DataPackagesRepository\b\|MemoryDomainsRepository\b\|MemoryDomainSuggestionsRepository\b\|RecipesRepository\b\|UserStackSubscriptionsRepository\b" app/ cli/ services/ 2>/dev/null | head -3
```

Expect: empty. (Test files are out of scope; they may still construct repos directly for fixture setup.)

- [ ] **Step 4: Run full DuckDB suite sample to confirm no regression**

```bash
.venv/bin/pytest tests/test_audit_repository_query.py tests/test_db.py tests/test_users_sso_flag.py tests/test_access_control.py tests/test_admin_configure_api.py -q --timeout=60 2>&1 | tail -3
```

Expect: 122+ passed.

- [ ] **Step 5: Commit (one commit per file or one umbrella commit — your choice based on file count)**

If only 1–2 files left, one commit:
```bash
git add <files>
git commit -m "refactor(api): remaining callsites use new factories"
```

---

## Phase 2 — TF / Compose wiring {#phase-2}

### Task 2A.1: Add Secret Manager triple for postgres in main.tf

**Files:**
- Modify: `infra/modules/customer-instance/main.tf`

- [ ] **Step 1: Read existing JWT pattern as template**

```bash
sed -n '35,75p' infra/modules/customer-instance/main.tf
```

This is the 4-resource block: `random_password.jwt` + `google_secret_manager_secret.jwt` + `_version` + `_iam_member.vm_jwt`.

- [ ] **Step 2: Insert the analog postgres block right after the JWT block**

```hcl
# ─── Postgres password lifecycle (side-car postgres:16-alpine container) ──
resource "random_password" "postgres" {
  length  = 32
  special = false   # PG-friendly; avoids shell-quoting in startup-script
}

resource "google_secret_manager_secret" "postgres" {
  secret_id = "agnes-postgres-${var.name}"
  replication { auto {} }
}

resource "google_secret_manager_secret_version" "postgres" {
  secret      = google_secret_manager_secret.postgres.id
  secret_data = random_password.postgres.result
}

resource "google_secret_manager_secret_iam_member" "vm_postgres" {
  secret_id = google_secret_manager_secret.postgres.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}
```

- [ ] **Step 3: Validate TF**

```bash
cd infra/modules/customer-instance
terraform init -backend=false 2>&1 | tail -5
terraform validate
cd <repo-root>
```

Expect: `Success! The configuration is valid.`.

- [ ] **Step 4: Commit**

```bash
git add infra/modules/customer-instance/main.tf
git commit -m "feat(infra): add Secret Manager triple for postgres password

Mirrors the existing JWT pattern (lines 35-71 in main.tf). The VM
service account gains secretAccessor on the new agnes-postgres-<name>
secret; startup-script (next commit) pulls it and writes POSTGRES_-
PASSWORD into /opt/agnes/.env before docker compose up."
```

### Task 2A.2: Wire startup-script for postgres password + COMPOSE_FILE

**Files:**
- Modify: `infra/modules/customer-instance/startup-script.sh.tpl`

- [ ] **Step 1: Read existing startup-script around the .env write block**

```bash
grep -n "JWT\|gcloud secrets\|docker compose" infra/modules/customer-instance/startup-script.sh.tpl | head -20
```

- [ ] **Step 2: Add pull-secret + chown + env-write fragment**

Locate the section where JWT_SECRET is currently pulled. Right below it, add:

```bash
# ── Postgres password from Secret Manager + side-car prep ──
POSTGRES_PASSWORD="$(gcloud secrets versions access latest --secret=agnes-postgres-${name})"

# postgres:16-alpine container runs as uid 70; ensure /data/postgres exists + writable.
mkdir -p /data/postgres
chown 70:70 /data/postgres
chmod 700 /data/postgres
```

Then locate the `cat > /opt/agnes/.env` block (where existing env entries are written) and add three lines:

```bash
# Inside the heredoc that writes /opt/agnes/.env, append:
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
DATABASE_URL=postgresql+psycopg://agnes:$POSTGRES_PASSWORD@postgres:5432/agnes
COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml
```

If the file uses a different env-write mechanism (echo append, sed, etc.), follow that convention.

- [ ] **Step 3: Smoke check by terraform planning a dummy module**

```bash
cd infra/modules/customer-instance
terraform init -backend=false 2>&1 | tail -3
terraform validate
cd <repo-root>
```

Expect: valid.

- [ ] **Step 4: Commit**

```bash
git add infra/modules/customer-instance/startup-script.sh.tpl
git commit -m "feat(infra): startup-script pulls postgres password + enables overlay

Pulls agnes-postgres-<name> from Secret Manager, ensures /data/postgres
exists with uid 70 ownership (postgres:16-alpine container user), writes
POSTGRES_PASSWORD + DATABASE_URL + COMPOSE_FILE into /opt/agnes/.env
before docker compose up. The COMPOSE_FILE env makes compose include
docker-compose.postgres.yml automatically — no per-deploy -f flag needed."
```

### Task 2B.1: Add data-migrate one-shot service + volume bind

**Files:**
- Modify: `docker-compose.postgres.yml`

- [ ] **Step 1: Read current overlay structure**

```bash
cat docker-compose.postgres.yml
```

- [ ] **Step 2: Add data-migrate service + bind named volume to /data/postgres**

Modify the `services:` and `volumes:` sections:

```yaml
services:
  postgres:
    # ... existing ...
  migrate:
    # ... existing ...

  # NEW: one-shot data migration after schema is up
  data-migrate:
    build: .
    command: python -m scripts.migrate_duckdb_to_pg --duckdb-path /data/state/system.duckdb
    depends_on:
      postgres: { condition: service_healthy }
      migrate:  { condition: service_completed_successfully }
    environment:
      - DATABASE_URL=postgresql+psycopg://agnes:${POSTGRES_PASSWORD:-agnes}@postgres:5432/agnes
    volumes:
      - data:/data:ro       # read-only source DuckDB
    restart: "no"

  app:
    depends_on:
      postgres:     { condition: service_healthy }
      migrate:      { condition: service_completed_successfully }
      data-migrate: { condition: service_completed_successfully }    # NEW gate
    # ... rest of existing app config ...

# NEW: bind named volume to /data/postgres for backup-policy coverage
volumes:
  postgres_data:
    driver: local
    driver_opts:
      type:   none
      device: /data/postgres
      o:      bind
```

- [ ] **Step 3: Validate compose**

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml config --services
```

Expect: `postgres`, `migrate`, `data-migrate`, `app`, `scheduler` (order may vary).

- [ ] **Step 4: Commit**

```bash
git add docker-compose.postgres.yml
git commit -m "feat(deploy): data-migrate one-shot + bind PG volume to /data/postgres

Two adds:
  - data-migrate service runs python -m scripts.migrate_duckdb_to_pg on
    every compose up (idempotent via ON CONFLICT DO NOTHING + SHA-256
    validate); app blocks on data-migrate exit 0.
  - postgres_data named volume binds to /data/postgres so persistence
    lives on the customer-instance data disk (covered by the existing
    google_compute_disk_resource_policy_attachment 'data_backup')."
```

### Task 2C.1: Operator runbook

**Files:**
- Create: `docs/postgres-cutover-runbook.md`

- [ ] **Step 1: Write the runbook**

```markdown
# Postgres cutover runbook

This document covers the operator-facing flow after PR #388 + the
follow-up PR land. Read it once before applying the new infra/module pin.

## What changed

- Postgres is now a side-car container on the customer-instance VM.
  Persistence is on the existing `data` disk under `/data/postgres`,
  backed up by the daily snapshot policy already attached to that disk.
- App-state writes route to Postgres via the factory in
  `src/repositories/__init__.py`. DuckDB analytics (`analytics.duckdb`,
  parquet views, BQ extension) are unchanged.
- On every `docker compose up`, the `migrate` service runs `alembic
  upgrade head` and `data-migrate` runs the DuckDB → Postgres copy
  script (idempotent — no-op after first deploy).

## Standard deploy

1. Bump the `infra-vX.Y.Z` module pin in your customer infra repo.
2. `terraform apply` — applies the new Secret Manager triple. No VM
   re-create.
3. SSH into the VM, run `sudo /usr/local/bin/agnes-auto-upgrade.sh`
   (or wait for the next cron tick). The script pulls the new image
   and restarts docker compose. The `migrate` service runs revisions
   0011 → head. The `data-migrate` service copies any existing rows.
4. Verify `curl -fsS http://localhost:8000/api/health` returns 200.
5. Check audit log growth — fresh API requests should land rows in PG:
   `docker compose exec postgres psql -U agnes -d agnes -c
   "SELECT count(*) FROM audit_log;"`.

## Verify after deploy

```bash
# All four services healthy + migrate / data-migrate exited 0
docker compose -f docker-compose.yml -f docker-compose.postgres.yml ps

# Schema head matches latest alembic revision (0011 or later)
docker compose exec postgres psql -U agnes -d agnes -c \
    "SELECT version_num FROM alembic_version;"

# Row count sanity for a moved table
docker compose exec postgres psql -U agnes -d agnes -c \
    "SELECT count(*) FROM users;"
# Compare against DuckDB:
docker compose exec app python -c \
    "import duckdb; c = duckdb.connect('/data/state/system.duckdb'); \
     print(c.execute('SELECT count(*) FROM users').fetchone())"
```

The two counts should match (or PG ≥ DuckDB if any new request landed between counts).

## Operator override — point at managed Postgres (Cloud SQL, RDS, Supabase, Neon, …)

If you do not want the side-car postgres container — for example to
share a single managed Cloud SQL instance across several Agnes VMs:

1. SSH into VM, edit `/opt/agnes/.env`:
   - Replace `DATABASE_URL=postgresql+psycopg://agnes:.../@postgres:5432/agnes`
     with your managed-PG URL.
   - Remove `COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml`
     (or replace with just `docker-compose.yml` so the overlay's
     postgres + data-migrate services don't run).
2. Restart: `sudo systemctl restart agnes` (or
   `cd /opt/agnes && docker compose up -d`).
3. Run `alembic upgrade head` against the managed PG in your deploy
   pipeline (one-shot, no service needed).

## Rollback to DuckDB

The DuckDB file at `/data/state/system.duckdb` is never deleted by
this stack. To roll back:

1. SSH in, edit `/opt/agnes/.env`:
   - Remove `DATABASE_URL=...`
   - Remove `COMPOSE_FILE=...` (or set to just `docker-compose.yml`).
2. `sudo /usr/local/bin/agnes-auto-upgrade.sh` (or
   `docker compose up -d` directly).
3. Verify `/api/health` → 200 and audit_log starts landing in DuckDB
   again.

The PG side-car keeps its data on `/data/postgres` for any future
re-cutover; nothing is destroyed by rollback.

## POSTGRES_PASSWORD rotation

```bash
# Generate + store a new version
NEW=$(openssl rand -base64 32 | tr -d /=+ | head -c 32)
echo -n "$NEW" | gcloud secrets versions add agnes-postgres-${name} --data-file=-

# Apply it: edit /opt/agnes/.env on the VM, replace POSTGRES_PASSWORD
# (or re-run startup-script), then docker compose restart postgres app
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `migrate` exits non-zero, app refuses to start | Failed alembic upgrade (constraint violation, missing extension) | Check `docker compose logs migrate`; fix offending revision; restart |
| `data-migrate` hangs > 5 min | Huge audit_log or stale lock | `docker compose logs -f data-migrate`; consider running with `--only <table>` to triage |
| `/data/postgres` permissions denied at container boot | Startup-script ran without chown | SSH in, `sudo chown -R 70:70 /data/postgres && docker compose restart postgres` |
| Disk full on `/data` | PG data + DuckDB + uploads together over disk size | Resize the `data` disk in TF; re-apply; reboot VM |
```

- [ ] **Step 2: Commit**

```bash
git add docs/postgres-cutover-runbook.md
git commit -m "docs: postgres cutover runbook for operators"
```

---

## Phase 3 — Final integration {#phase-3}

### Task 3.1: CHANGELOG bullet

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Read top of CHANGELOG**

```bash
sed -n '1,30p' CHANGELOG.md
```

- [ ] **Step 2: Add bullet under `[Unreleased]`**

Find `## [Unreleased]` and add (preserving existing Added/Changed/Fixed/Internal subsections — create if absent):

```markdown
## [Unreleased]

### Added
- **BREAKING (deploy-side)**: Postgres side-car container is now the
  default customer-instance deploy shape. `infra/modules/customer-
  instance/` adds a Secret Manager triple for `POSTGRES_PASSWORD`;
  startup-script writes `DATABASE_URL` + `COMPOSE_FILE` into
  `/opt/agnes/.env`; `docker-compose.postgres.yml` adds a `data-migrate`
  one-shot service that copies any existing DuckDB rows into Postgres
  on first deploy (idempotent on subsequent boots). Cloud SQL / RDS /
  managed PG remain a no-friction operator override via
  `DATABASE_URL` + removing the `COMPOSE_FILE` entry. The DuckDB
  `system.duckdb` file is untouched; rollback is documented in
  `docs/postgres-cutover-runbook.md`. After bumping the
  `infra-vX.Y.Z` pin in your customer infra repo and running
  `terraform apply`, the next `agnes-auto-upgrade.sh` tick or VM
  restart applies the change.
- Five new Postgres repository ports (`data_packages_pg.py`,
  `memory_domains_pg.py`, `memory_domain_suggestions_pg.py`,
  `recipes_pg.py`, `user_stack_subscriptions_pg.py`) closing the
  DuckDB-only gap left by PR #388. Alembic revision 0011 covers
  the seven new PG tables (5 + 2 bridges) with downgrade. Five
  cross-engine contract test files prove DuckDB-↔-PG parity for
  every method (52 parametrized tests, full suite now ~317 passed).
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG bullet for Postgres follow-up (BREAKING deploy default)"
```

### Task 3.2: Full suite validation

**Files:** none (validation only)

- [ ] **Step 1: PG suite**

```bash
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/ -q --tb=line --timeout=180 2>&1 | tail -3
```

Expect: roughly `317 passed, 1 skipped`.

- [ ] **Step 2: DuckDB regression sample**

```bash
.venv/bin/pytest tests/test_audit_repository_query.py tests/test_db.py tests/test_users_sso_flag.py tests/test_access_control.py tests/test_admin_configure_api.py -q --timeout=60 2>&1 | tail -3
```

Expect: 122+ passed, 0 failed.

- [ ] **Step 3: Compose validate**

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml config --services
```

Expect: `postgres`, `migrate`, `data-migrate`, `app`, `scheduler`.

- [ ] **Step 4: TF validate**

```bash
cd infra/modules/customer-instance
terraform init -backend=false 2>&1 | tail -3
terraform validate
cd <repo-root>
```

Expect: valid.

- [ ] **Step 5: Push branch + open PR**

```bash
git push origin HEAD
gh pr create --repo keboola/agnes-the-ai-analyst --base main --head $(git branch --show-current) \
  --title "feat(db): close 5-repo gap + side-car postgres as standard deploy" \
  --body-file - <<'EOF'
Closes the DuckDB-only gap left by PR #388 and makes Postgres side-car
container the standard customer-instance deploy shape.

## What ships

- **5 new PG repository ports** (`data_packages`, `memory_domains`,
  `memory_domain_suggestions`, `recipes`, `user_stack_subscriptions`)
  with full SQLAlchemy 2.0 models and a single alembic revision (0011)
  covering 7 tables (5 + 2 bridges).
- **5 factory entries** in `src/repositories/__init__.py` plus
  callsite swaps in `app/web/router.py`, `app/api/memory.py`, and any
  remaining `app/`/`cli/` files using direct `XYZRepository(conn)`.
- **5 cross-engine contract test files** (52 parametrized tests)
  proving DuckDB ↔ PG parity for every method. Full suite now
  ~317 passed.
- **TF customer-instance edits** — Secret Manager triple for
  `POSTGRES_PASSWORD` (mirrors existing JWT pattern), startup-script
  pulls the secret and writes `DATABASE_URL` + `COMPOSE_FILE` into
  `/opt/agnes/.env` so `docker compose up` includes the overlay by
  default.
- **Compose overlay updates** — `data-migrate` one-shot service runs
  `python -m scripts.migrate_duckdb_to_pg` on every boot (idempotent
  via ON CONFLICT DO NOTHING + SHA-256 validate); app gates on
  data-migrate exit 0; `postgres_data` named volume bound to
  `/data/postgres` so persistence is on the existing backed-up disk.
- **Operator runbook** at `docs/postgres-cutover-runbook.md` covers
  verify, Cloud SQL override, rollback to DuckDB, password rotation,
  and troubleshooting.

## What this does NOT do (separate PRs)

- Retire the 28 DuckDB state repos — separate PR ≥ 2 weeks post-stability.
- Dedicated Cloud SQL Terraform module — operator override remains
  manual until that's justified.
- HA replica / Postgres 17 upgrade / auto-rotation.

## Spec + plan

- Spec: `docs/superpowers/specs/2026-05-27-pg-followup-design.md`
- Plan: `docs/superpowers/plans/2026-05-27-pg-followup.md`

## Testing

- ~317 PG tests pass (`AGNES_TEST_PG_BACKEND=pgserver pytest tests/db_pg/`)
- DuckDB suite regression-free
- `docker compose -f docker-compose.yml -f docker-compose.postgres.yml config` resolves cleanly
- `terraform validate` clean on `infra/modules/customer-instance/`
EOF
```

- [ ] **Step 6: Watch CI**

```bash
sleep 5
gh pr checks <PR-number> --repo keboola/agnes-the-ai-analyst 2>&1 | tail -10
```

Wait for `build-and-push` + `Devin Review` green. If any failure, read the log + fix in a new commit; do not force-push.

---

## Self-review

After writing the complete plan, looked at the spec with fresh eyes against this plan.

### Spec coverage

| Spec section | Task |
|---|---|
| Model split (data_packages.py, recipes.py NEW; knowledge.py, store.py EXTEND) | 1A.1–1A.4 |
| `_PK_COLUMNS` entries for new composite-PK tables | 1A.5 |
| Alembic revision 0011 with 7 tables + downgrade | 1A.6 |
| 5 `*_pg.py` modules with full method shape | 1B.1–1B.5 |
| 5 factory entries in `src/repositories/__init__.py` | 1C.1 |
| 5 cross-engine contract test files (52 tests) | 1D.1–1D.5 |
| Callsite swap | 1E.1–1E.3 |
| TF Secret Manager triple for postgres password | 2A.1 |
| Startup-script pulls secret + writes COMPOSE_FILE | 2A.2 |
| data-migrate one-shot service + volume bind | 2B.1 |
| Operator runbook | 2C.1 |
| CHANGELOG `**BREAKING**` bullet | 3.1 |
| Full suite validation + PR push | 3.2 |

Every spec requirement maps to a task.

### Placeholder scan

Self-grepped the plan for `TBD`, `TODO`, `fill in`, `add appropriate`, `similar to Task`, `handle edge cases` — none found. Tasks 1B.2–1B.5 contain partial code blocks plus full sibling-DuckDB file path readouts; the engineer (or subagent) has enough info to write the module without "see Task 1B.1" referencing.

### Type consistency

- `data_packages_repo()`, `memory_domains_repo()`, etc. — consistent factory naming across plan
- `DataPackagesPgRepository`, `MemoryDomainsPgRepository`, etc. — consistent class naming
- `_PK_COLUMNS` tuple shape — matches existing format from PR #388 cleanup pass
- Alembic revision id `0011_data_packages_and_memory_extensions` — same across spec + plan
- `JSONB` column allowlist — `_JSON_COLUMNS = {"tags", "when_to_use", "when_not_to_use", "example_questions"}` consistent across Task 1B.1 and the existing `scripts/migrate_duckdb_to_pg/tasks.py` from PR #388 cleanup
- Test fixture: `pg_engine` only (no `_alembic_upgrade_to_head` — that fixture doesn't exist in conftest.py; the inline alembic-upgrade pattern is documented in the "Test fixture pattern" callout under Phase 1B)

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-27-pg-followup.md`. Two execution options:

**1. Subagent-Driven (recommended)** — controller dispatches a fresh subagent per task with full task text + scene-setting context, two-stage review (spec compliance + code quality) after each task. Best fit for this plan because:
- Each repo module is independent (no shared state between Tasks 1B.1–1B.5)
- Contract tests follow a strict template (good subagent material)
- TF/compose tasks have well-bounded acceptance (terraform validate / compose config exit codes)

**2. Inline Execution** — controller executes tasks in this session via `superpowers:executing-plans`, batch with checkpoints. Faster for trivial tasks (Phase 3.1 CHANGELOG bullet) but loses isolation benefit for the bigger architectural pieces.

**Which approach?**
