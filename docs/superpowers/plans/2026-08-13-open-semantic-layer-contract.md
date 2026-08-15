# Open Semantic-Layer Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store a semantic layer as a canonical Apache Ossie document and project today's flat tables from it, so any source format can be added additively and nothing is lost at import.

**Architecture:** An adapter returns a validated Ossie document and nothing else; the importer stores it whole, then projects `metric_definitions`, `glossary_terms` and `column_metadata` from it, stamped with the source's provenance and pruned only within that source. Two new tables (`semantic_models`, `semantic_sources`) plus a junction to data packages. Transports (git clone, upload, existing connection) feed adapters; export emits the stored document unchanged.

**Tech Stack:** Python 3.11+, FastAPI, DuckDB + Postgres (dual backend), Typer CLI, PyYAML, jsonschema, Alembic.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md`. Read it before Task 1.
- **Dual-backend parity is non-negotiable.** A method added to `src/repositories/X.py` gets its `X_pg.py` sibling in the SAME task, plus a factory dispatch entry and a cross-engine contract test.
- **Reach repos through the factory**, never instantiate a repo class at a callsite. Import `semantic_model_repo()` / `semantic_source_repo()` from `src.repositories`.
- **Migration ladders move together.** A DuckDB `_vN_to_v(N+1)` step gets a matching Alembic revision in the same task.
- **`SCHEMA_VERSION` is 115, so the DuckDB step is v115→v116. The Alembic revision is `0063` — `0062_knowledge_domains_backfill` is already taken on `origin/main` (a data-only backfill, which is why it does not move `SCHEMA_VERSION`). CONFIRM BOTH AGAINST `origin/main`, NOT against this branch, IMMEDIATELY BEFORE TASK 2.** Checking the local branch is what hid this collision: a feature branch that forked before the colliding revision landed shows a clean `0061` tip and looks safe.
- **Alembic revision ids are `VARCHAR(32)`** — keep them short.
- **Vendor-agnostic repo.** No customer names, internal hostnames, cloud project ids, or private-repo references in code, comments, docs, commit messages or PR text. The Keboola connector is an existing OSS connector and may be named as such.
- **No AI attribution** in commits or PR text.
- **CHANGELOG bullet** under `## [Unreleased]` — once, in Task 15, not per task.
- **Command UX standard** for new CLI commands: positional search term, `--limit`, `--json`; not-found errors hint the next step (`cli/query_hints.py`).
- **Do not run the full test suite locally.** Run the specific test files each task names. CI runs the full suite on the PR.

---

### Task 1: Vendored Ossie schema and validator

**Files:**
- Create: `src/semantic/__init__.py`
- Create: `src/semantic/schema/osi-schema.json` (downloaded, vendored)
- Create: `src/semantic/schema/VERSION` (single line, e.g. `0.2.0.dev0`)
- Create: `src/semantic/validation.py`
- Test: `tests/test_semantic_validation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `validate_document(text: str) -> ValidationResult` where
  `ValidationResult` is a dataclass with `ok: bool`, `spec_version: str`,
  `errors: list[str]`, `parsed: dict | None`.
  `SPEC_VERSION: str` module constant.

- [ ] **Step 1: Vendor the schema**

```bash
mkdir -p src/semantic/schema
curl -fsSL https://raw.githubusercontent.com/apache/ossie/main/core-spec/osi-schema.json \
  -o src/semantic/schema/osi-schema.json
python3 -c "import json;d=json.load(open('src/semantic/schema/osi-schema.json'));print(len(json.dumps(d)))"
```

Record the upstream commit SHA you fetched from in a comment at the top of
`src/semantic/validation.py`. Write the spec's own version string into
`src/semantic/schema/VERSION` (read it from the schema's `version`/`$id`, or
from `core-spec/spec.yaml` if the JSON schema carries no version).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_semantic_validation.py
import pytest

from src.semantic.validation import SPEC_VERSION, validate_document

VALID = """
version: "0.2.0.dev0"
semantic_model:
  - name: retail
    datasets:
      - name: orders
        source: "db.public.orders"
        fields:
          - name: order_date
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: "order_date"
            datatype: Date
"""


def test_valid_document_passes():
    result = validate_document(VALID)
    assert result.ok is True
    assert result.errors == []
    assert result.parsed["semantic_model"][0]["name"] == "retail"
    assert result.spec_version == SPEC_VERSION


def test_missing_required_field_fails_with_path():
    bad = VALID.replace("        source: \"db.public.orders\"\n", "")
    result = validate_document(bad)
    assert result.ok is False
    assert result.parsed is None
    assert any("source" in e for e in result.errors)


def test_malformed_yaml_is_an_error_not_an_exception():
    result = validate_document("semantic_model: [oops")
    assert result.ok is False
    assert any("YAML" in e or "yaml" in e for e in result.errors)
```

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/bin/pytest tests/test_semantic_validation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.semantic'`

- [ ] **Step 4: Implement the validator**

```python
# src/semantic/validation.py
"""Validation of Apache Ossie semantic-model documents.

The JSON schema in ``schema/osi-schema.json`` is vendored from the upstream
Apache Ossie repository (incubating) and pinned deliberately: the project
describes its pre-release schema as mutable, so an upgrade is a reviewed
change with its own test run, never an implicit dependency bump.

Vendored from apache/ossie @ <commit sha>
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import json
import yaml
from jsonschema import Draft202012Validator

_SCHEMA_DIR = Path(__file__).parent / "schema"
SPEC_VERSION: str = (_SCHEMA_DIR / "VERSION").read_text().strip()
_SCHEMA: Dict[str, Any] = json.loads((_SCHEMA_DIR / "osi-schema.json").read_text())
_VALIDATOR = Draft202012Validator(_SCHEMA)


@dataclass
class ValidationResult:
    ok: bool
    spec_version: str = SPEC_VERSION
    errors: List[str] = field(default_factory=list)
    parsed: Optional[Dict[str, Any]] = None


def validate_document(text: str) -> ValidationResult:
    """Parse and schema-check one Ossie document.

    Never raises on bad input — a malformed document is a result with
    ``ok=False``, because every caller stores the failure rather than
    aborting a whole sync over one bad file.
    """
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return ValidationResult(ok=False, errors=[f"YAML parse error: {exc}"])

    if not isinstance(parsed, dict):
        return ValidationResult(ok=False, errors=["Document root must be a mapping"])

    errors = [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in _VALIDATOR.iter_errors(parsed)
    ]
    if errors:
        return ValidationResult(ok=False, errors=sorted(errors))
    return ValidationResult(ok=True, parsed=parsed)
```

Add `jsonschema` to `pyproject.toml` dependencies if it is not already there
(`grep -n jsonschema pyproject.toml`).

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_semantic_validation.py -v`
Expected: PASS (3 tests)

If the upstream schema rejects the `VALID` fixture, do NOT loosen the
validator — fix the fixture against the real schema. The fixture is a claim
about the spec; the schema is the spec.

- [ ] **Step 5b: Validate against an upstream example**

Hand-written fixtures only prove the validator accepts what you imagined. Pull
one real example and assert it passes:

```bash
mkdir -p tests/fixtures/ossie
curl -fsSL https://raw.githubusercontent.com/apache/ossie/main/examples/tpcds_semantic_model.yaml \
  -o tests/fixtures/ossie/tpcds_semantic_model.yaml
```

```python
def test_upstream_example_validates():
    text = Path("tests/fixtures/ossie/tpcds_semantic_model.yaml").read_text()
    result = validate_document(text)
    assert result.ok, result.errors
```

If this fails, the vendored schema and the vendored example are from different
commits — re-fetch both from the same SHA before going further.

- [ ] **Step 6: Commit**

```bash
git add src/semantic tests/test_semantic_validation.py pyproject.toml
git commit -m "feat(semantic): vendored Ossie schema and document validator"
```

---

### Task 2: Schema — both migration ladders

**Files:**
- Modify: `src/db.py` (`SCHEMA_VERSION`, `_SYSTEM_SCHEMA` DDL, new `_v115_to_v116`, both wiring branches)
- Create: `migrations/versions/0063_sem_models_v116.py`
- Test: `tests/test_db_schema_version.py` (existing gate), `tests/test_semantic_schema.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces: tables `semantic_models`, `semantic_sources`,
  `data_package_semantic_models` on both engines.

- [ ] **Step 1: Confirm the ladder position**

```bash
grep -n "^SCHEMA_VERSION" src/db.py && ls migrations/versions | tail -3
```

Run it against `origin/main`, not against your branch:

```bash
git fetch origin main
git show origin/main:src/db.py | grep -m1 "^SCHEMA_VERSION"
git ls-tree --name-only origin/main migrations/versions/ | grep -v __init__ | tail -3
```

Expected as of this plan: `SCHEMA_VERSION = 115`, last revision
`0062_knowledge_domains_backfill`. If either has moved, renumber this task's
`_v115_to_v116` / `0063_*` and its `down_revision` to follow what is actually
there, and carry the new numbers through the rest of the plan.

Checking your own branch instead is not a shortcut — it is how the first
version of this plan ended up claiming `0062` was free. A branch that forked
before a revision landed shows a clean tip and looks safe.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_semantic_schema.py
def _columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return {r[1] for r in rows}


def test_semantic_tables_exist_on_fresh_install(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)

    assert _columns(conn, "semantic_models") >= {
        "id", "slug", "name", "description", "document", "document_json",
        "spec_version", "content_hash", "source", "source_ref",
        "status", "validation_errors", "validated_at",
        "created_at", "updated_at",
    }
    assert _columns(conn, "semantic_sources") >= {
        "id", "kind", "name", "adapter", "config", "enabled",
        "last_sync_at", "last_sync_status", "last_sync_error",
    }
    assert _columns(conn, "data_package_semantic_models") == {"package_id", "model_id"}
    conn.close()
```

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/bin/pytest tests/test_semantic_schema.py -v`
Expected: FAIL — `Catalog Error: Table with name semantic_models does not exist`

- [ ] **Step 4: Add the DDL to `_SYSTEM_SCHEMA`**

Place next to the other semantic tables (`metric_definitions`, `glossary_terms`):

```sql
CREATE TABLE IF NOT EXISTS semantic_models (
    id                VARCHAR PRIMARY KEY,
    slug              VARCHAR NOT NULL,
    name              VARCHAR NOT NULL,
    description       TEXT,
    -- The document exactly as the adapter produced it. Never re-serialized:
    -- round-tripping through a YAML dumper would silently reorder keys and
    -- drop comments, and this column is what `export` hands back out.
    document          TEXT NOT NULL,
    document_json     JSON,
    spec_version      VARCHAR NOT NULL,
    content_hash      VARCHAR NOT NULL,
    source            VARCHAR NOT NULL DEFAULT 'manual',
    source_ref        VARCHAR,
    status            VARCHAR NOT NULL DEFAULT 'valid',
    validation_errors JSON,
    validated_at      TIMESTAMP,
    created_at        TIMESTAMP DEFAULT current_timestamp,
    updated_at        TIMESTAMP DEFAULT current_timestamp
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_models_origin
    ON semantic_models (source, source_ref, slug);

CREATE TABLE IF NOT EXISTS semantic_sources (
    id               VARCHAR PRIMARY KEY,
    kind             VARCHAR NOT NULL,     -- 'git' | 'upload' | 'connection'
    name             VARCHAR NOT NULL,
    adapter          VARCHAR NOT NULL,     -- 'native' | 'keboola_metastore'
    config           JSON NOT NULL,
    enabled          BOOLEAN DEFAULT TRUE,
    last_sync_at     TIMESTAMP,
    last_sync_status VARCHAR,
    last_sync_error  TEXT,
    created_at       TIMESTAMP DEFAULT current_timestamp,
    updated_at       TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS data_package_semantic_models (
    package_id VARCHAR NOT NULL,
    model_id   VARCHAR NOT NULL,
    PRIMARY KEY (package_id, model_id)
);
```

- [ ] **Step 5: Add the migration step and wire it**

```python
def _v115_to_v116(conn: duckdb.DuckDBPyConnection) -> None:
    """v115→v116: semantic_models + semantic_sources + the data-package junction.

    Pure additive DDL — no backfill. Existing metric_definitions and
    glossary_terms rows keep their provenance and are NOT retro-attached to a
    model: there is no document they came from, and inventing one would make
    `export` emit a document the instance never received.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS semantic_models ( ... )""")   # same DDL as Step 4
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_models_origin ...""")
    conn.execute("""CREATE TABLE IF NOT EXISTS semantic_sources ( ... )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS data_package_semantic_models ( ... )""")
```

Wire it in BOTH branches, matching the existing style:

```python
            # v115→v116: semantic_models + semantic_sources + junction. No-op
            # on fresh installs — _SYSTEM_SCHEMA already declares them.
            _v115_to_v116(conn)
```

```python
            if current < 116:
                _v115_to_v116(conn)
```

Then bump `SCHEMA_VERSION = 116`.

- [ ] **Step 6: Write the Alembic revision**

```python
# migrations/versions/0063_sem_models_v116.py
"""semantic_models, semantic_sources, data_package_semantic_models

Mirrors DuckDB ``_v115_to_v116``. Pure additive DDL, no backfill.

Revision ID: 0063_sem_models_v116
Revises: 0062_knowledge_domains_backfill
Create Date: 2026-08-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0063_sem_models_v116"
down_revision: Union[str, None] = "0062_knowledge_domains_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "semantic_models",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("document_json", sa.JSON()),
        sa.Column("spec_version", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String()),
        sa.Column("status", sa.String(), nullable=False, server_default="valid"),
        sa.Column("validation_errors", sa.JSON()),
        sa.Column("validated_at", sa.TIMESTAMP()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "idx_semantic_models_origin",
        "semantic_models",
        ["source", "source_ref", "slug"],
        unique=True,
    )
    op.create_table(
        "semantic_sources",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("adapter", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true()),
        sa.Column("last_sync_at", sa.TIMESTAMP()),
        sa.Column("last_sync_status", sa.String()),
        sa.Column("last_sync_error", sa.Text()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_table(
        "data_package_semantic_models",
        sa.Column("package_id", sa.String(), primary_key=True),
        sa.Column("model_id", sa.String(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("data_package_semantic_models")
    op.drop_table("semantic_sources")
    op.drop_index("idx_semantic_models_origin", table_name="semantic_models")
    op.drop_table("semantic_models")
```

- [ ] **Step 7: Run both gates**

Run: `.venv/bin/pytest tests/test_semantic_schema.py tests/test_db_schema_version.py -v`
Expected: PASS

Run: `.venv/bin/pytest tests/db_pg/test_alembic_roundtrip.py tests/db_pg/test_alembic_skeleton.py -v`
Expected: PASS — this is what catches a ladder that reaches two different endpoints.

- [ ] **Step 8: Commit**

```bash
git add src/db.py migrations/versions/0063_sem_models_v116.py tests/test_semantic_schema.py
git commit -m "feat(db): semantic_models, semantic_sources and package junction (v116)"
```

---

### Task 3: `semantic_models` repository pair

**Files:**
- Create: `src/repositories/semantic_models.py`
- Create: `src/repositories/semantic_models_pg.py`
- Modify: `src/repositories/__init__.py` (dispatch entry, `__all__`, accessor)
- Test: `tests/db_pg/test_semantic_models_contract.py`

**Interfaces:**
- Consumes: `src.semantic.validation.ValidationResult` (Task 1) — only its
  fields, no import needed in the repo layer.
- Produces, on both classes:
  - `upsert(*, id, slug, name, description, document, document_json, spec_version, content_hash, source, source_ref, status, validation_errors, validated_at) -> dict`
  - `get(model_id: str) -> dict | None`
  - `get_by_slug(slug: str) -> dict | None`
  - `list_all(*, source: str | None = None, source_ref: str | None = None) -> list[dict]`
  - `delete(model_id: str) -> bool`
  - `delete_missing(*, source: str, source_ref: str | None, keep_slugs: list[str]) -> list[str]`
    — the prune primitive; returns deleted ids. Scoped to one origin by
    construction, so it can never touch another source's rows.
  - `link_package(package_id: str, model_id: str) -> None`
  - `unlink_package(package_id: str, model_id: str) -> None`
  - `list_for_package(package_id: str) -> list[dict]`

- [ ] **Step 1: Write the failing contract test**

```python
# tests/db_pg/test_semantic_models_contract.py
"""Cross-engine contract tests for the semantic_models repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.semantic_models import SemanticModelsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return SemanticModelsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.semantic_models_pg import SemanticModelsPgRepository

    return SemanticModelsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def _upsert(repo, *, id, slug, source="git", source_ref="repo-a"):
    return repo.upsert(
        id=id,
        slug=slug,
        name=slug.title(),
        description=None,
        document=f"version: '0.2.0.dev0'\nsemantic_model:\n  - name: {slug}\n",
        document_json={"semantic_model": [{"name": slug}]},
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source=source,
        source_ref=source_ref,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


def test_upsert_then_get(repo):
    _upsert(repo, id="m1", slug="retail")
    row = repo.get("m1")
    assert row["slug"] == "retail"
    assert row["spec_version"] == "0.2.0.dev0"
    assert row["document"].startswith("version:")
    assert row["document_json"]["semantic_model"][0]["name"] == "retail"


def test_upsert_is_idempotent_on_same_origin(repo):
    _upsert(repo, id="m1", slug="retail")
    _upsert(repo, id="m1", slug="retail")
    assert len(repo.list_all()) == 1


def test_get_by_slug(repo):
    _upsert(repo, id="m1", slug="retail")
    assert repo.get_by_slug("retail")["id"] == "m1"
    assert repo.get_by_slug("nope") is None


def test_list_filters_by_origin(repo):
    _upsert(repo, id="m1", slug="retail", source="git", source_ref="repo-a")
    _upsert(repo, id="m2", slug="finance", source="git", source_ref="repo-b")
    assert {r["id"] for r in repo.list_all(source="git", source_ref="repo-a")} == {"m1"}
    assert len(repo.list_all(source="git")) == 2


def test_delete_missing_is_scoped_to_one_origin(repo):
    _upsert(repo, id="m1", slug="retail", source="git", source_ref="repo-a")
    _upsert(repo, id="m2", slug="stale", source="git", source_ref="repo-a")
    _upsert(repo, id="m3", slug="other", source="git", source_ref="repo-b")

    deleted = repo.delete_missing(source="git", source_ref="repo-a", keep_slugs=["retail"])

    assert deleted == ["m2"]
    assert repo.get("m1") is not None
    assert repo.get("m3") is not None, "prune must never cross a source_ref boundary"


def test_delete_missing_with_empty_keep_list_deletes_that_origin_only(repo):
    _upsert(repo, id="m1", slug="retail", source="git", source_ref="repo-a")
    _upsert(repo, id="m3", slug="other", source="git", source_ref="repo-b")

    assert repo.delete_missing(source="git", source_ref="repo-a", keep_slugs=[]) == ["m1"]
    assert repo.get("m3") is not None


def test_package_links(repo):
    _upsert(repo, id="m1", slug="retail")
    repo.link_package("pkg1", "m1")
    assert [r["id"] for r in repo.list_for_package("pkg1")] == ["m1"]
    repo.link_package("pkg1", "m1")  # idempotent
    assert len(repo.list_for_package("pkg1")) == 1
    repo.unlink_package("pkg1", "m1")
    assert repo.list_for_package("pkg1") == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/pytest tests/db_pg/test_semantic_models_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: src.repositories.semantic_models`

- [ ] **Step 3: Implement the DuckDB repository**

Follow the shape of `src/repositories/glossary.py`: a `_COLS` constant, a
`_SELECT` joined from it, `_row_to_dict` / `_rows_to_dicts` helpers using
`self.conn.description`, and JSON columns decoded on read.

```python
# src/repositories/semantic_models.py
"""Repository for ``semantic_models`` + ``data_package_semantic_models`` (v116).

The canonical form of a semantic layer is the Ossie document in ``document``.
Every flat projection (metric_definitions, glossary_terms, column_metadata) is
derived from it and can be regenerated; this table is the owner.

``delete_missing`` is the prune primitive and is deliberately keyed on
(source, source_ref): a sync run can only ever delete rows it could have
written. Two sources syncing into one instance cannot collide.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import duckdb

_JSON_COLS = ("document_json", "validation_errors")


class SemanticModelsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _COLS = [
        "id", "slug", "name", "description", "document", "document_json",
        "spec_version", "content_hash", "source", "source_ref",
        "status", "validation_errors", "validated_at",
        "created_at", "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    def _decode(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        out = dict(zip(self._COLS, row))
        for col in _JSON_COLS:
            val = out.get(col)
            if isinstance(val, str):
                out[col] = json.loads(val) if val else None
        return out

    def upsert(self, *, id, slug, name, description, document, document_json,
               spec_version, content_hash, source, source_ref, status,
               validation_errors, validated_at) -> Dict[str, Any]:
        self.conn.execute(
            "DELETE FROM semantic_models WHERE source = ? AND source_ref IS NOT DISTINCT FROM ? AND slug = ?",
            [source, source_ref, slug],
        )
        self.conn.execute(
            f"INSERT INTO semantic_models ({self._SELECT}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp,current_timestamp)",
            [id, slug, name, description, document,
             json.dumps(document_json) if document_json is not None else None,
             spec_version, content_hash, source, source_ref, status,
             json.dumps(validation_errors) if validation_errors is not None else None,
             validated_at],
        )
        return self.get(id)

    def get(self, model_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM semantic_models WHERE id = ?", [model_id]
        ).fetchone()
        return self._decode(row)

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM semantic_models WHERE slug = ? ORDER BY updated_at DESC",
            [slug],
        ).fetchone()
        return self._decode(row)

    def list_all(self, *, source: Optional[str] = None,
                 source_ref: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = f"SELECT {self._SELECT} FROM semantic_models"
        clauses, params = [], []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if source_ref is not None:
            clauses.append("source_ref IS NOT DISTINCT FROM ?")
            params.append(source_ref)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name"
        return [self._decode(r) for r in self.conn.execute(sql, params).fetchall()]

    def delete(self, model_id: str) -> bool:
        existed = self.get(model_id) is not None
        self.conn.execute("DELETE FROM data_package_semantic_models WHERE model_id = ?", [model_id])
        self.conn.execute("DELETE FROM semantic_models WHERE id = ?", [model_id])
        return existed

    def delete_missing(self, *, source: str, source_ref: Optional[str],
                       keep_slugs: List[str]) -> List[str]:
        rows = self.conn.execute(
            "SELECT id FROM semantic_models "
            "WHERE source = ? AND source_ref IS NOT DISTINCT FROM ? "
            "  AND slug NOT IN (SELECT UNNEST(?::VARCHAR[])) ORDER BY id",
            [source, source_ref, keep_slugs],
        ).fetchall()
        ids = [r[0] for r in rows]
        for model_id in ids:
            self.delete(model_id)
        return ids

    def link_package(self, package_id: str, model_id: str) -> None:
        self.conn.execute(
            "DELETE FROM data_package_semantic_models WHERE package_id = ? AND model_id = ?",
            [package_id, model_id],
        )
        self.conn.execute(
            "INSERT INTO data_package_semantic_models (package_id, model_id) VALUES (?, ?)",
            [package_id, model_id],
        )

    def unlink_package(self, package_id: str, model_id: str) -> None:
        self.conn.execute(
            "DELETE FROM data_package_semantic_models WHERE package_id = ? AND model_id = ?",
            [package_id, model_id],
        )

    def list_for_package(self, package_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {', '.join('m.' + c for c in self._COLS)} FROM semantic_models m "
            "JOIN data_package_semantic_models j ON j.model_id = m.id "
            "WHERE j.package_id = ? ORDER BY m.name",
            [package_id],
        ).fetchall()
        return [self._decode(r) for r in rows]
```

Watch the empty-list case in `delete_missing`: `keep_slugs=[]` must delete
every row of that origin, not zero rows. The contract test above pins it
because an empty list is exactly what a source that went empty produces, and
getting it backwards is a silent no-prune.

- [ ] **Step 4: Implement the Postgres sibling**

`src/repositories/semantic_models_pg.py`, same method set, SQLAlchemy engine
instead of a DuckDB connection. Follow `src/repositories/glossary_pg.py` for
parameter binding and row mapping. Two engine differences to handle:

- `IS NOT DISTINCT FROM` works on both; keep it.
- The DuckDB `UNNEST(?::VARCHAR[])` has no Postgres equivalent in this form —
  use `slug <> ALL(:keep)` with a bound list, or an `IN` expansion.

- [ ] **Step 5: Register in the factory**

In `src/repositories/__init__.py`: add `"semantic_model"` to the dispatch
table next to `"glossary"`, add `semantic_model_repo` to `__all__`, and add

```python
def semantic_model_repo() -> Any:
    return _build("semantic_model")
```

- [ ] **Step 6: Run the contract test on both engines**

Run: `.venv/bin/pytest tests/db_pg/test_semantic_models_contract.py -v`
Expected: PASS — 7 tests × 2 backends = 14

Run: `.venv/bin/pytest tests/test_backend_split_guard.py -v`
Expected: PASS — catches a direct repo instantiation or a raw `get_system_db()` read.

- [ ] **Step 7: Commit**

```bash
git add src/repositories/semantic_models.py src/repositories/semantic_models_pg.py \
        src/repositories/__init__.py tests/db_pg/test_semantic_models_contract.py
git commit -m "feat(repositories): semantic_models on both backends"
```

---

### Task 4: `semantic_sources` repository pair

**Files:**
- Create: `src/repositories/semantic_sources.py`, `src/repositories/semantic_sources_pg.py`
- Modify: `src/repositories/__init__.py`
- Test: `tests/db_pg/test_semantic_sources_contract.py`

**Interfaces:**
- Produces: `create(*, id, kind, name, adapter, config, enabled=True) -> dict`,
  `get(source_id) -> dict | None`, `list_all(*, enabled_only: bool = False) -> list[dict]`,
  `update(source_id, **fields) -> dict | None`, `delete(source_id) -> bool`,
  `record_sync(source_id, *, status: str, error: str | None) -> None`,
  and the accessor `semantic_source_repo()`.

- [ ] **Step 1: Write the failing contract test**

Same fixture scaffolding as Task 3 (copy `_make_duckdb_repo` / `_make_pg_repo` /
`repo`, swapping the class names). Assertions:

```python
def test_create_and_get(repo):
    repo.create(id="s1", kind="git", name="Finance models",
                adapter="native",
                config={"repo_url": "https://example.com/x.git", "ref": "main",
                        "glob": "semantic/**/*.yaml"})
    row = repo.get("s1")
    assert row["kind"] == "git"
    assert row["config"]["glob"] == "semantic/**/*.yaml"
    assert row["enabled"] is True


def test_list_enabled_only(repo):
    repo.create(id="s1", kind="git", name="on", adapter="native", config={})
    repo.create(id="s2", kind="git", name="off", adapter="native", config={}, enabled=False)
    assert {r["id"] for r in repo.list_all(enabled_only=True)} == {"s1"}
    assert len(repo.list_all()) == 2


def test_record_sync_stores_outcome(repo):
    repo.create(id="s1", kind="git", name="x", adapter="native", config={})
    repo.record_sync("s1", status="error", error="clone failed: auth")
    row = repo.get("s1")
    assert row["last_sync_status"] == "error"
    assert "auth" in row["last_sync_error"]
    assert row["last_sync_at"] is not None


def test_record_sync_clears_previous_error_on_success(repo):
    repo.create(id="s1", kind="git", name="x", adapter="native", config={})
    repo.record_sync("s1", status="error", error="boom")
    repo.record_sync("s1", status="ok", error=None)
    assert repo.get("s1")["last_sync_error"] is None
```

That last test matters: a stale error left behind after a successful sync is
how an admin page lies about the current state.

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/pytest tests/db_pg/test_semantic_sources_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: src.repositories.semantic_sources`

- [ ] **Step 3: Implement the DuckDB class**

Same shape as Task 3: `_COLS` / `_SELECT`, `_decode` with `config` and any
JSON column decoded on read. `record_sync` writes all three columns in one
statement so a success cannot leave a stale error behind:

```python
    def record_sync(self, source_id: str, *, status: str, error: Optional[str]) -> None:
        self.conn.execute(
            "UPDATE semantic_sources SET last_sync_at = current_timestamp, "
            "last_sync_status = ?, last_sync_error = ?, updated_at = current_timestamp "
            "WHERE id = ?",
            [status, error, source_id],
        )
```

- [ ] **Step 4: Implement the Postgres sibling and register both in the factory**

`src/repositories/semantic_sources_pg.py` with the identical method set, plus
the `"semantic_source"` dispatch entry, the `__all__` entry, and

```python
def semantic_source_repo() -> Any:
    return _build("semantic_source")
```

- [ ] **Step 5: Run the contract test on both engines**

Run: `.venv/bin/pytest tests/db_pg/test_semantic_sources_contract.py -v`
Expected: PASS — 4 tests × 2 backends = 8

- [ ] **Step 6: Commit**

```bash
git add src/repositories/semantic_sources*.py src/repositories/__init__.py \
        tests/db_pg/test_semantic_sources_contract.py
git commit -m "feat(repositories): semantic_sources on both backends"
```

---

### Task 5: Adapter protocol and the native adapter

**Files:**
- Create: `src/semantic/adapters/__init__.py`
- Create: `src/semantic/adapters/native.py`
- Test: `tests/test_semantic_adapters.py`

**Interfaces:**
- Consumes: `validate_document` (Task 1).
- Produces:
  - `class SemanticAdapter(Protocol): def extract(self, config: dict) -> list[str]`
  - `get_adapter(name: str) -> SemanticAdapter` (raises `UnknownAdapter`)
  - `register_adapter(name: str, adapter: SemanticAdapter) -> None`
  - `NativeAdapter` — `config = {"documents": [<yaml text>, ...]}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_semantic_adapters.py
import pytest

from src.semantic.adapters import UnknownAdapter, get_adapter


def test_native_adapter_returns_documents_untouched():
    text = "version: '0.2.0.dev0'\nsemantic_model:\n  - name: retail\n"
    out = get_adapter("native").extract({"documents": [text]})
    assert out == [text], "the adapter must not re-serialize; byte-identical or bust"


def test_unknown_adapter_names_the_available_ones():
    with pytest.raises(UnknownAdapter) as exc:
        get_adapter("nope")
    assert "native" in str(exc.value)
```

The first assertion is the whole contract: an adapter that parses and re-dumps
would reorder keys and strip comments, and `export` would then hand back a
document the source never wrote.

- [ ] **Step 2: Run it, watch it fail**

Run: `.venv/bin/pytest tests/test_semantic_adapters.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# src/semantic/adapters/__init__.py
"""Adapter registry.

An adapter's entire job is to return Ossie documents. It never writes to
semantic_models, metric_definitions, glossary_terms or column_metadata —
validation and persistence happen once, centrally, in the importer. That is
what makes a new source format additive: one function, no new write path.
"""

from __future__ import annotations

from typing import Dict, List, Protocol, runtime_checkable


class UnknownAdapter(LookupError):
    pass


@runtime_checkable
class SemanticAdapter(Protocol):
    def extract(self, config: dict) -> List[str]:
        """Return Ossie documents as text, exactly as they should be stored."""


_REGISTRY: Dict[str, SemanticAdapter] = {}


def register_adapter(name: str, adapter: SemanticAdapter) -> None:
    _REGISTRY[name] = adapter


def get_adapter(name: str) -> SemanticAdapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownAdapter(
            f"unknown semantic adapter {name!r}; available: {', '.join(sorted(_REGISTRY))}"
        ) from None


from src.semantic.adapters.native import NativeAdapter  # noqa: E402

register_adapter("native", NativeAdapter())
```

```python
# src/semantic/adapters/native.py
from __future__ import annotations

from typing import List


class NativeAdapter:
    """The source already publishes Ossie documents — pass them through."""

    def extract(self, config: dict) -> List[str]:
        return list(config.get("documents") or [])
```

- [ ] **Step 4: Run, then commit**

```bash
.venv/bin/pytest tests/test_semantic_adapters.py -v
git add src/semantic/adapters tests/test_semantic_adapters.py
git commit -m "feat(semantic): adapter protocol and native adapter"
```

---

### Task 6: Dialect resolution

**Files:**
- Create: `src/semantic/dialect.py`
- Test: `tests/test_semantic_dialect.py`

**Interfaces:**
- Produces: `resolve_expression(expression: dict) -> tuple[str | None, str | None]`
  returning `(sql, reason_unusable)` — exactly one of the two is non-None.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_semantic_dialect.py
from src.semantic.dialect import resolve_expression


def _expr(*pairs):
    return {"dialects": [{"dialect": d, "expression": e} for d, e in pairs]}


def test_duckdb_dialect_wins_when_present():
    sql, reason = resolve_expression(_expr(("ANSI_SQL", "SUM(a)"), ("DUCKDB", "sum(a)")))
    assert (sql, reason) == ("sum(a)", None)


def test_ansi_sql_is_the_fallback():
    sql, reason = resolve_expression(_expr(("ANSI_SQL", "SUM(a)"), ("SNOWFLAKE", "SUM(a)")))
    assert (sql, reason) == ("SUM(a)", None)


def test_warehouse_only_expression_is_unusable_not_spliced():
    sql, reason = resolve_expression(_expr(("SNOWFLAKE", "TRY_CAST(a AS NUMBER)")))
    assert sql is None
    assert "SNOWFLAKE" in reason


def test_empty_expression_is_unusable():
    sql, reason = resolve_expression({"dialects": []})
    assert sql is None
    assert reason
```

The third test is the one that matters — it pins the behavior change. Today a
warehouse-dialect fragment is spliced into a DuckDB query and either fails or,
worse, means something subtly different.

- [ ] **Step 2: Run it, watch it fail, implement**

```python
# src/semantic/dialect.py
"""Pick the SQL a DuckDB-backed instance can actually run.

Preference order is DUCKDB, then ANSI_SQL. Anything else is reported as
unusable WITH ITS REASON rather than spliced into a query: a warehouse-specific
fragment that happens to parse is more dangerous than one that fails.
"""

from __future__ import annotations

from typing import Optional, Tuple

_PREFERRED = ("DUCKDB", "ANSI_SQL")


def resolve_expression(expression: dict) -> Tuple[Optional[str], Optional[str]]:
    dialects = (expression or {}).get("dialects") or []
    # An entry with no dialect NAME is dropped, not kept under a None key:
    # this function's whole contract is to return a reason rather than raise,
    # and a None key blows up the `sorted()` join below. Adapter-composed
    # documents really do produce these when the upstream model declares no
    # dialect at all.
    by_name = {
        d.get("dialect"): d.get("expression")
        for d in dialects
        if d.get("expression") and isinstance(d.get("dialect"), str)
    }

    for name in _PREFERRED:
        if by_name.get(name):
            return by_name[name], None

    if not by_name:
        return None, "no expression in any usable dialect"
    offered = ", ".join(sorted(by_name))
    return None, f"only warehouse-specific dialects offered ({offered}); no DUCKDB or ANSI_SQL"
```

Add a fifth test — the four in the plan all pass against a version that raises
on this input, which is exactly the kind of green that asserts nothing:

```python
def test_dialect_entry_without_a_name_is_ignored_not_a_crash():
    sql, reason = resolve_expression({"dialects": [{"dialect": None, "expression": "SUM(a)"}]})
    assert sql is None
    assert reason
```

- [ ] **Step 3: Run and commit**

```bash
.venv/bin/pytest tests/test_semantic_dialect.py -v
git add src/semantic/dialect.py tests/test_semantic_dialect.py
git commit -m "feat(semantic): dialect resolution with explicit unusable reason"
```

---

### Task 7: Projection with per-source prune

**Files:**
- Create: `src/semantic/projection.py`
- Test: `tests/test_semantic_projection.py`

**Interfaces:**
- Consumes: `resolve_expression` (Task 6), `metric_repo()`, `glossary_repo()`,
  `column_metadata_repo()` from `src.repositories`.
- Produces: `project_document(document_json: dict, *, source: str, source_ref: str | None) -> ProjectionReport`
  with `metrics_written: int`, `glossary_written: int`, `columns_written: int`,
  `skipped: list[dict]` (each `{kind, name, reason}`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_semantic_projection.py
import pytest

from src.semantic.projection import project_document

DOC = {
    "semantic_model": [
        {
            "name": "retail",
            "datasets": [
                {
                    "name": "orders",
                    "source": "db.public.orders",
                    "fields": [
                        {"name": "order_date", "datatype": "Date",
                         "description": "when the order was placed",
                         "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                                      "expression": "order_date"}]}},
                    ],
                }
            ],
            "metrics": [
                {"name": "revenue", "datatype": "Decimal",
                 "description": "total revenue",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                              "expression": "SUM(amount)"}]}},
                {"name": "wh_only",
                 "expression": {"dialects": [{"dialect": "SNOWFLAKE",
                                              "expression": "TRY_CAST(x AS NUMBER)"}]}},
            ],
        }
    ]
}


def test_projects_metrics_and_columns(system_db):
    report = project_document(DOC, source="git", source_ref="repo-a")
    assert report.metrics_written == 1
    assert report.columns_written == 1


def test_unusable_metric_is_reported_not_written(system_db):
    report = project_document(DOC, source="git", source_ref="repo-a")
    skipped = [s for s in report.skipped if s["name"] == "wh_only"]
    assert len(skipped) == 1
    assert "SNOWFLAKE" in skipped[0]["reason"]


def _stub_dataset(name="orders"):
    # The real schema sets `minItems: 1` on `datasets` and requires
    # ["name", "datasets"] on a model, so `"datasets": []` is NOT a valid
    # document even though project_document never validates. Keep fixtures
    # schema-legal or they become a trap the moment anything validates them.
    return {"name": name, "source": f"db.public.{name}", "fields": []}


def test_reprojection_prunes_only_this_origin(system_db):
    project_document(DOC, source="git", source_ref="repo-a")
    other = {"semantic_model": [{"name": "fin", "datasets": [_stub_dataset("costs")],
                                 "metrics": [
        {"name": "cost", "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "SUM(c)"}]}}]}]}
    project_document(other, source="git", source_ref="repo-b")

    shrunk = {"semantic_model": [{"name": "retail", "datasets": [_stub_dataset()],
                                  "metrics": []}]}
    project_document(shrunk, source="git", source_ref="repo-a")

    from src.repositories import metric_repo
    # NOTE: MetricRepository exposes `list(category=None)`, NOT `list_all()`.
    # The semantic_models repo this plan adds does use `list_all` — don't let
    # the two naming conventions blur.
    remaining = {m["name"] for m in metric_repo().list()}
    assert "revenue" not in remaining, "repo-a's dropped metric should be pruned"
    assert "cost" in remaining, "prune must not cross a source_ref boundary"
```

Reuse the existing `system_db` fixture from `tests/conftest.py`; check its
exact name first with `grep -n "def system_db" tests/conftest.py` and adapt if
it differs.

- [ ] **Step 2: Run it, watch it fail, implement**

Projection rules, all of them explicit:

- Each `metrics[]` entry → one `metric_definitions` row. `sql` comes from
  `resolve_expression`; an unusable expression is skipped and reported, never
  written with empty SQL.
- Each `datasets[].fields[]` entry → one `column_metadata` row keyed on the
  dataset's resolved `table_id` and the field name, `description` from the
  field, `source` from the model's origin.
- Model-level and dataset-level `ai_context.synonyms` → `synonyms` on the
  metrics of that dataset (preserving today's behavior).
- Glossary: only if the document carries glossary entries in
  `custom_extensions`; core Ossie has no glossary object, so an empty
  projection here is correct, not a bug. Note the schema's shape —
  `custom_extensions` entries require `vendor_name` plus a `data` field that
  is a **JSON-encoded string**, not a nested mapping. Read it with
  `json.loads(entry["data"])`, and write it with `json.dumps(...)`; an inline
  YAML mapping there fails validation.
- Prune: after writing, delete rows with this `(source, source_ref)` whose ids
  are not in the just-written set — never a global delete.

- [ ] **Step 3: Run and commit**

```bash
.venv/bin/pytest tests/test_semantic_projection.py -v
git add src/semantic/projection.py tests/test_semantic_projection.py
git commit -m "feat(semantic): project Ossie documents into flat tables with scoped prune"
```

---

### Task 8: Import pipeline

**Files:**
- Create: `src/semantic/importer.py`
- Test: `tests/test_semantic_importer.py`

**Interfaces:**
- Consumes: `validate_document` (1), `project_document` (7),
  `semantic_model_repo()` (3).
- Produces: `import_documents(source: dict, documents: list[str]) -> ImportReport`
  with `models_written: int`, `models_unchanged: int`,
  `models_pruned: list[str]`, `invalid: list[dict]`,
  `projection: ProjectionReport | None`.

This task takes documents as an argument rather than fetching them. Task 9 adds
the fetching half (`import_source`). Splitting it this way keeps every test here
free of clones and HTTP, and it is why the pipeline can be exercised
exhaustively before any transport exists.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_semantic_importer.py
import pytest

from src.repositories import semantic_model_repo
from src.semantic.importer import import_documents

SOURCE = {"id": "s1", "kind": "upload", "adapter": "native",
          "source": "git", "source_ref": "repo-a"}


def _doc(slug, metric="revenue"):
    return (
        "version: '0.2.0.dev0'\n"
        "semantic_model:\n"
        f"  - name: {slug}\n"
        "    datasets:\n"
        "      - name: orders\n"
        "        source: db.public.orders\n"
        "    metrics:\n"
        f"      - name: {metric}\n"
        "        expression:\n"
        "          dialects:\n"
        "            - dialect: ANSI_SQL\n"
        "              expression: SUM(amount)\n"
    )


def test_unchanged_document_is_a_no_op_write(system_db):
    """Re-importing identical content must not bump updated_at."""
    import_documents(SOURCE, [_doc("retail")])
    first = semantic_model_repo().get_by_slug("retail")["updated_at"]

    report = import_documents(SOURCE, [_doc("retail")])

    assert report.models_unchanged == 1
    assert report.models_written == 0
    assert semantic_model_repo().get_by_slug("retail")["updated_at"] == first


def test_invalid_document_is_stored_with_its_errors_and_does_not_abort_the_run(system_db):
    """One bad file must not cost the sync its good files."""
    report = import_documents(SOURCE, [_doc("retail"), "semantic_model: [oops"])

    assert report.models_written == 1
    assert len(report.invalid) == 1
    assert semantic_model_repo().get_by_slug("retail") is not None


def test_document_dropped_upstream_is_pruned(system_db):
    import_documents(SOURCE, [_doc("retail"), _doc("finance")])
    dropped = semantic_model_repo().get_by_slug("finance")["id"]

    report = import_documents(SOURCE, [_doc("retail")])

    assert report.models_pruned == [dropped]
    assert semantic_model_repo().get_by_slug("finance") is None
    assert semantic_model_repo().get_by_slug("retail") is not None
```

A malformed document has no slug to key on, so it cannot be stored under one.
Store it keyed on a stable digest of its text with `status='invalid'`, and keep
it out of `keep_slugs` — otherwise the prune pass would delete the last good
version of a model the moment someone pushes a typo.

- [ ] **Step 2: Run it, watch it fail, implement**

Pipeline order, and the reasoning behind each step:

1. `validate_document` each incoming document.
2. `content_hash = sha256(document.encode())`; equal hash → count as unchanged
   and skip the write entirely. This is what keeps `updated_at` honest and makes
   a 6-hourly sync cheap.
3. Upsert valid documents; upsert invalid ones with `status='invalid'` and their
   errors, so an admin surface can show what broke instead of the row silently
   vanishing.
4. `delete_missing(source=…, source_ref=…, keep_slugs=<slugs of valid docs seen>)`.
5. Project ONCE per import call, over all valid documents together — **not once
   per document.**

That last point is not a style preference. `project_document` prunes its own
output scoped to `(source, source_ref)` on every call, so projecting documents
one at a time under a shared origin makes each call delete the previous one's
rows. Two documents from the same git repo would leave only the last one's
metrics behind, and no test that checks only counts or model rows would notice.
Proven against the real implementation: projecting `retail` then `finance` under
`source_ref="repo-a"` leaves `{'cost'}` — `revenue` is gone.

Steps 3–5 must also be one unit of work: a prune that lands while a projection
failure aborts the run leaves the instance with rows deleted and nothing
written back.

Pin it with a test that reads projected CONTENT, not counts:

```python
def test_two_documents_in_one_import_keep_both_their_metrics(system_db):
    from src.repositories import metric_repo

    import_documents(SOURCE, [_doc("retail", "revenue"), _doc("finance", "cost")])

    names = {m["name"] for m in metric_repo().list()}
    assert names >= {"revenue", "cost"}
```

- [ ] **Step 3: Run and commit**

```bash
.venv/bin/pytest tests/test_semantic_importer.py -v
git add src/semantic/importer.py tests/test_semantic_importer.py
git commit -m "feat(semantic): import pipeline with content-hash no-op and scoped prune"
```

---

### Task 9: Git and upload transports

**Files:**
- Create: `src/semantic/transports.py`
- Modify: `src/semantic/adapters/native.py` (accept documents loaded by a transport)
- Test: `tests/test_semantic_transports.py`

**Interfaces:**
- Consumes: `get_adapter` (5), `import_documents` (8), `semantic_source_repo()` (4).
- Produces:
  - `load_documents(source: dict) -> list[str]` — fetch the raw payload for
    `source["kind"]` (`git` clones and globs, `upload` reads
    `config["documents"]`, `connection` hands the connection config through),
    then run it through `get_adapter(source["adapter"])`.
  - `import_source(source_id: str) -> ImportReport` — look the source up,
    `load_documents`, `import_documents`, then `record_sync` with the outcome.
    A transport or adapter that raises is recorded as
    `record_sync(status="error", error=…)` and re-raised; nothing is imported
    from a failed fetch, so a temporarily unreachable source can never look
    like a source that went empty. **That distinction is the whole reason
    fetching lives outside `import_documents`** — an empty list means "prune
    everything", and a failed clone must never produce one.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_semantic_transports.py
import pytest

import src.semantic.transports as transports
from src.semantic.transports import import_source, load_documents

DOC = "version: '0.2.0.dev0'\nsemantic_model:\n  - name: retail\n"

GIT_SOURCE = {
    "id": "s1", "kind": "git", "adapter": "native",
    "config": {"repo_url": "https://example.com/x.git", "ref": "main",
               "glob": "semantic/**/*.yaml"},
}


@pytest.fixture
def clone_dir(tmp_path, monkeypatch):
    """A fake clone: two matching documents, one file the glob must ignore."""
    root = tmp_path / "clone"
    (root / "semantic" / "nested").mkdir(parents=True)
    (root / "semantic" / "a.yaml").write_text(DOC)
    (root / "semantic" / "nested" / "b.yaml").write_text(DOC)
    (root / "README.md").write_text("not a model")
    monkeypatch.setattr(transports, "_clone", lambda **kw: root)
    return root


def test_git_transport_globs_matching_files(clone_dir):
    docs = load_documents(GIT_SOURCE)
    assert len(docs) == 2
    assert all("semantic_model" in d for d in docs)


def test_git_transport_rejects_paths_escaping_the_clone(clone_dir):
    """A symlink out of the clone must not be readable through the glob."""
    escape = clone_dir / "semantic" / "escape.yaml"
    escape.symlink_to("/etc/passwd")

    docs = load_documents(GIT_SOURCE)

    assert len(docs) == 2
    assert not any("root:" in d for d in docs)


def test_failed_clone_records_the_error_and_imports_nothing(system_db, monkeypatch):
    """An unreachable source must never look like a source that went empty."""
    from src.repositories import semantic_source_repo

    semantic_source_repo().create(id="s1", kind="git", name="x",
                                  adapter="native", config=GIT_SOURCE["config"])

    def _boom(**kw):
        raise RuntimeError("clone failed: host unreachable")

    monkeypatch.setattr(transports, "_clone", _boom)

    with pytest.raises(RuntimeError):
        import_source("s1")

    row = semantic_source_repo().get("s1")
    assert row["last_sync_status"] == "error"
    assert "unreachable" in row["last_sync_error"]
```

That last test is the one that protects real data: without it, a network blip
turns into `documents == []`, which the prune pass reads as "upstream deleted
everything".

The second test is not hypothetical: this repo's security playbook requires
realpath-containment for any filesystem path built from untrusted names, and a
cloned repository is untrusted input.

- [ ] **Step 2: Run it, watch it fail, implement**

Reuse the existing clone machinery in `src/marketplace.py` (`sync_one`,
~line 670) rather than shelling out to `git` again — read it first and follow
its credential handling; never put a token on argv.

Containment: resolve each globbed path with `Path.resolve()` and drop anything
whose resolved form is not under the resolved clone root.

- [ ] **Step 3: Run and commit**

```bash
.venv/bin/pytest tests/test_semantic_transports.py -v
git add src/semantic/transports.py tests/test_semantic_transports.py
git commit -m "feat(semantic): git and upload transports with path containment"
```

---

### Task 10: Admin REST API, export endpoint, RBAC

**Files:**
- Create: `app/api/semantic_models.py`
- Modify: `app/main.py` (import ~line 405, `include_router` ~line 2430)
- Modify: `app/resource_types.py` (`ResourceType.SEMANTIC_MODEL` + spec)
- Modify: `tests/test_documentation_api_triple_surface.py` (`_COHORT` entry per new endpoint)
- Test: `tests/test_semantic_models_api.py`

**Interfaces:**
- Consumes: `semantic_model_repo()`, `semantic_source_repo()`, `import_source`.
- Produces:
  - `GET/POST/PUT/DELETE /api/admin/semantic-models` — `Depends(require_admin)`
  - `GET/POST/PUT/DELETE /api/admin/semantic-sources` + `POST …/{id}/sync`
  - `GET /api/semantic-models/{slug}.yaml` — resource-gated export

- [ ] **Step 1: Write the failing test**

```python
def test_admin_endpoints_reject_non_admin(client, user_token):
    for method, path in [("get", "/api/admin/semantic-models"),
                         ("post", "/api/admin/semantic-sources")]:
        r = getattr(client, method)(path, headers={"Authorization": f"Bearer {user_token}"})
        assert r.status_code == 403


def test_export_returns_the_stored_document_byte_for_byte(client, admin_token):
    doc = "version: '0.2.0.dev0'\nsemantic_model:\n  - name: retail\n# trailing comment\n"
    client.post("/api/admin/semantic-models",
                json={"document": doc},
                headers={"Authorization": f"Bearer {admin_token}"})
    r = client.get("/api/semantic-models/retail.yaml",
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.text == doc, "export must not re-serialize; comments and key order survive"


def test_posting_an_invalid_document_returns_422_with_the_schema_errors(client, admin_token):
    r = client.post("/api/admin/semantic-models", json={"document": "semantic_model: [oops"},
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"]


def test_a_source_owned_model_cannot_be_edited_through_the_api(client, admin_token, git_backed_model):
    """The source owns imported material — even an admin edits it at the source."""
    r = client.put(f"/api/admin/semantic-models/{git_backed_model['id']}",
                   json={"name": "renamed"},
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["code"] == "source_owned"
    assert "git" in body["message"], "the error must name where to go and edit it"


def test_an_uploaded_model_remains_editable(client, admin_token, uploaded_model):
    r = client.put(f"/api/admin/semantic-models/{uploaded_model['id']}",
                   json={"name": "renamed"},
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
```

Those two tests are the enforcement point for the spec's ownership rule. There
is no UI in this plan, so the API is where "imported rows are read-only" either
holds or does not — and a 409 that names the source is what stops someone
editing a value the next sync would silently revert.

- [ ] **Step 2: Run it, watch it fail, implement**

Router shape follows `app/api/metrics.py`: `router = APIRouter(tags=["semantic-models"])`,
`Depends(require_admin)` on every admin route, Pydantic request models.

Register the resource type in `app/resource_types.py` next to `DATA_PACKAGE`:

```python
    ResourceType.SEMANTIC_MODEL: ResourceTypeSpec(
        key=ResourceType.SEMANTIC_MODEL,
        display_name="Semantic models",
        description="A semantic model — datasets, fields, relationships and metrics.",
        id_format="<model_id>",
        list_blocks=_semantic_model_blocks,
    ),
```

Export is gated on the linked data package's grant, not on admin — that is the
point of the junction.

- [ ] **Step 3: Run, then run the API snapshot gate**

```bash
.venv/bin/pytest tests/test_semantic_models_api.py -v
make update-openapi-snapshot && git diff --stat
```

A new endpoint changes the OpenAPI snapshot; regenerate it in this task or CI
fails on a diff you did not intend.

- [ ] **Step 4: Commit**

```bash
git add app/api/semantic_models.py app/main.py app/resource_types.py \
        tests/test_semantic_models_api.py tests/snapshots
git commit -m "feat(api): semantic-model and semantic-source admin endpoints, export"
```

---

### Task 11: CLI command groups

**Files:**
- Create: `cli/commands/admin_semantic_model.py`, `cli/commands/admin_semantic_source.py`
- Modify: `cli/commands/admin.py` (two `add_typer` lines next to `admin_metrics_app`)
- Test: `tests/test_cli_semantic_model.py`

**Interfaces:**
- Produces: `agnes admin semantic-model list|show|import|export|validate`,
  `agnes admin semantic-source add|list|sync`.

- [ ] **Step 1: Write the failing test**

```python
def test_list_json_shape(runner, monkeypatch):
    result = runner.invoke(app, ["admin", "semantic-model", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)


def test_show_missing_model_hints_the_next_step(runner, monkeypatch):
    result = runner.invoke(app, ["admin", "semantic-model", "show", "nope"])
    assert result.exit_code == 1
    assert "agnes admin semantic-model list" in result.stdout


def test_validate_reads_a_local_file_without_touching_the_server(runner, tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("semantic_model: [oops")
    result = runner.invoke(app, ["admin", "semantic-model", "validate", str(p)])
    assert result.exit_code == 1
    assert "YAML" in result.stdout
```

`validate` running offline is deliberate: an author fixing a document should
not need a reachable server or an admin token.

- [ ] **Step 2: Run it, watch it fail, implement**

Follow `cli/commands/admin_metrics.py`. Flag vocabulary is fixed by the
command-UX standard: positional term, `--limit`, `--json`. Not-found messages
go through `cli/query_hints.py`.

- [ ] **Step 3: Run, then the coverage gate**

```bash
.venv/bin/pytest tests/test_cli_semantic_model.py -v
.venv/bin/pytest tests/test_documentation_api_triple_surface.py -v
```

The triple-surface guard holds a `_COHORT` dict keyed by REST path →
`(cli_cmd, mcp_tool)`. A new endpoint needs an entry there or an `_EXEMPT`
one with a reason — which is why the REST, CLI and MCP work in Tasks 10–12
lands as ONE unit: the entry cannot be written until all three names exist.

- [ ] **Step 4: Commit**

```bash
git add cli/commands/admin_semantic_model.py cli/commands/admin_semantic_source.py \
        cli/commands/admin.py tests/test_cli_semantic_model.py
git commit -m "feat(cli): agnes admin semantic-model and semantic-source"
```

---

### Task 12: MCP read tools

**Files:**
- Modify: `app/api/mcp/foundation_tools.py` (inside `register_foundation_tools`, ~line 198)
- Test: `tests/test_mcp_tool_parity.py`, `tests/test_mcp_http.py` (both existing)

**Interfaces:**
- Produces: `semantic_model_search(query: str, k: int = 10) -> dict`,
  `semantic_model_get(slug: str) -> dict`.

- [ ] **Step 1: Add the tools next to `glossary_search` (~line 381)**

Match the surrounding style exactly — `async def`, a docstring that reads as
the tool description, a plain dict return.

- [ ] **Step 2: Update the pinned tool set**

`tests/test_mcp_http.py` pins the EXACT set of exposed tools. Adding a tool
without updating it fails CI, and the failure names the diff. Run it locally —
this is the one gate people forget:

```bash
.venv/bin/pytest tests/test_mcp_http.py tests/test_mcp_tool_parity.py -v
```

- [ ] **Step 3: Commit**

```bash
git add app/api/mcp/foundation_tools.py tests/test_mcp_http.py
git commit -m "feat(mcp): semantic-model search and get"
```

---

### Task 13: Keboola metastore adapter (slice 2)

**Files:**
- Create: `connectors/keboola/semantic_ossie.py`
- Modify: `connectors/keboola/semantic_layer.py:1072` (`sync_semantic_layer` delegates)
- Modify: `src/semantic/adapters/__init__.py` (register)
- Test: `tests/test_keboola_ossie_adapter.py`
- Fixture: `tests/fixtures/metastore_six_types.json`

**Interfaces:**
- Consumes: the six object types the existing client already fetches.
- Produces: `KeboolaMetastoreAdapter.extract(config) -> list[str]`, registered
  as `"keboola_metastore"`.

- [ ] **Step 1: Capture a fixture from the existing code path**

Read `sync_semantic_layer` (line 1072) and record what the client returns for
all six types. Save it as a JSON fixture — do not hand-write it, and scrub any
project identifiers before committing.

- [ ] **Step 2: Write the failing mapping test, one assertion per today's loss**

```python
def test_all_models_are_emitted_not_just_the_first(fixture):
    docs = KeboolaMetastoreAdapter().extract(cfg)
    assert len(docs) == 2, "today's importer takes models[0] and warns about the rest"


def test_per_column_fields_survive(fixture):
    doc = yaml.safe_load(KeboolaMetastoreAdapter().extract(cfg)[0])
    fields = doc["semantic_model"][0]["datasets"][0]["fields"]
    assert {f["name"] for f in fields} >= {"order_id", "order_date", "amount"}
    assert any(f.get("description") for f in fields)


def test_declared_dialect_becomes_a_dialect_tagged_expression(fixture):
    doc = yaml.safe_load(KeboolaMetastoreAdapter().extract(cfg)[0])
    metric = doc["semantic_model"][0]["metrics"][0]
    assert {d["dialect"] for d in metric["expression"]["dialects"]} == {"SNOWFLAKE"}


def test_keywords_survive_in_ai_context(fixture):
    doc = yaml.safe_load(KeboolaMetastoreAdapter().extract(cfg)[0])
    ds = doc["semantic_model"][0]["datasets"][0]
    assert ds["ai_context"]["synonyms"]
    ext = {e["vendor_name"]: json.loads(e["data"]) for e in ds.get("custom_extensions", [])}
    assert ext["AGNES"]["keywords"]


def test_relationships_survive_beyond_the_single_supported_case(fixture):
    doc = yaml.safe_load(KeboolaMetastoreAdapter().extract(cfg)[0])
    assert len(doc["semantic_model"][0]["relationships"]) == 3


def test_constraints_ride_custom_extensions(fixture):
    doc = yaml.safe_load(KeboolaMetastoreAdapter().extract(cfg)[0])
    ext = {e["vendor_name"]: json.loads(e["data"])
           for e in doc["semantic_model"][0].get("custom_extensions", [])}
    assert ext["AGNES"]["constraints"]
```

- [ ] **Step 3: Run, watch every one fail, implement the mapping**

Mapping table:

| Metastore | Ossie |
|---|---|
| `semantic-model` (each, not just the first) | one `semantic_model[]` entry |
| `semantic-model.sql_dialect` | the `dialect` on every expression from that model |
| `semantic-dataset` | `datasets[]` — `source`, `primary_key` |
| `semantic-dataset.fields[]` | `datasets[].fields[]` — name, datatype, description, `dimension.is_time` for timestamp roles |
| `semantic-metric` | `metrics[]` with a dialect-tagged expression |
| `semantic-relationship` (all) | `relationships[]` |
| `ai.synonyms` / `hints` / `warnings` | `ai_context.synonyms` / `instructions` |
| `ai.keywords`, `semantic-constraint` | `custom_extensions` under the Agnes vendor name |

- [ ] **Step 4: Delegate the existing sync**

`sync_semantic_layer` keeps its signature and its schedule; internally it now
builds documents through the adapter and hands them to `import_source`.

- [ ] **Step 5: Run and commit**

```bash
.venv/bin/pytest tests/test_keboola_ossie_adapter.py -v
git add connectors/keboola/semantic_ossie.py connectors/keboola/semantic_layer.py \
        src/semantic/adapters/__init__.py tests/test_keboola_ossie_adapter.py tests/fixtures
git commit -m "feat(connectors): compose an Ossie document from metastore objects"
```

---

### Task 14: Golden regression (slice 2) — RE-SCOPED DURING THE BUILD

**This task as written below no longer applies, and was not executed.** It
assumed the metastore adapter would feed `project_document`, making the flat
tables a projection of the composed document — so a golden regression was
needed to prove the projected rows equalled today's.

Implementation found that routing the adapter through projection collides with
the legacy flat importer, which writes `metric_definitions` rows for the same
metrics under its own `source`. The two writers collide on metric *name*: in one
order the legacy importer's name-ownership check silently drops its own row, in
the other the table gains a duplicate, since only `id` is unique. Reproduced
with a test.

The adapter therefore stores documents only, and **the flat tables keep their
existing writer, untouched**. With nothing about that path changed, there is no
projection regression to guard: the evidence is the legacy writer's own
pre-existing suite passing unchanged, plus an added test proving a broken Ossie
composition cannot break the flat sync.

The golden regression belongs to the **cutover** — projecting the flat tables
from stored documents and retiring the legacy writer in the same change. That is
a separate effort with its own plan; the design doc records the constraint it
has to satisfy.

The original task text follows for whoever picks the cutover up.



**Files:**
- Test: `tests/test_semantic_golden_regression.py`

- [ ] **Step 1: Record today's output BEFORE the new path is wired in**

```bash
git stash list   # must be empty — never stash in a worktree in this repo
git log --oneline -1
```

Check out the pre-Task-13 commit in a scratch clone (NOT this worktree — a
`git checkout` here would collide with the parallel sessions), run the old
importer against the fixture, and dump `metric_definitions` + `glossary_terms`
to `tests/fixtures/semantic_projection_golden.json`.

- [ ] **Step 2: Write the regression test**

```python
def test_projection_matches_pre_ossie_output(system_db, fixture, golden):
    """Everything that worked before must produce the identical row set."""
    import_source("keboola-1")
    from src.repositories import metric_repo, glossary_repo
    # Two repository-API traps, both of which produce a PASSING but meaningless
    # comparison if taken from memory:
    #   * MetricRepository exposes `list(category=None)`, not `list_all()`.
    #   * GlossaryRepository.list() defaults to limit=100 — pass an explicit
    #     high limit, or a golden diff silently stops at the 100th term.
    got = {"metrics": sorted(metric_repo().list(), key=lambda m: m["id"]),
           "glossary": sorted(glossary_repo().list(limit=100_000), key=lambda g: g["id"])}
    assert len(got["glossary"]) < 100_000, "raise the limit; the cap was hit"
    assert _normalize(got) == _normalize(golden)
```

`_normalize` drops timestamps and any id that is generated per-run. Everything
else must match exactly — this test is the licence to change the import path
at all.

- [ ] **Step 3: Run and commit**

```bash
.venv/bin/pytest tests/test_semantic_golden_regression.py -v
git add tests/test_semantic_golden_regression.py tests/fixtures/semantic_projection_golden.json
git commit -m "test: golden regression for metastore projection"
```

---

### Task 15: CHANGELOG and docs

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]`)
- Create: `docs/semantic-layer.md`
- Modify: `docs/README.md` (index entry), `CLAUDE.md` (one paragraph under the architecture section)

- [ ] **Step 1: Add the CHANGELOG bullets**

```markdown
### Added
- Semantic models are stored as canonical Apache Ossie documents, with git,
  upload and connection sources feeding a shared adapter seam. Documents are
  validated against a pinned schema, kept whole, and projected into metric,
  glossary and column tables with prune scoped per source. `agnes admin
  semantic-model` and `agnes admin semantic-source` manage them; models export
  as Ossie YAML.

### Fixed
- A metric whose expression exists only in a warehouse-specific SQL dialect is
  now reported as unusable locally instead of being spliced into a DuckDB query.
- The metastore import no longer discards per-column fields, keyword metadata,
  relationships beyond one narrow case, the declared SQL dialect, or every
  model after the first.
```

- [ ] **Step 2: Write `docs/semantic-layer.md`**

Cover: what a semantic model is here, the document-is-canonical rule, how to
register a git source, the adapter contract for a new format, the ownership
rule (imported rows are read-only; edit at the source), and how export works.
Use placeholder hosts (`example.com`) throughout.

- [ ] **Step 3: Verify and commit**

```bash
python3 scripts/verify_syncmap.py
git add CHANGELOG.md docs/semantic-layer.md docs/README.md CLAUDE.md
git commit -m "docs: semantic-layer contract reference and changelog"
```

---

## Out of scope for this plan

- **Slice 3** (adopting an upstream converter package, e.g. `apache-ossie-dbt`)
  gets its own plan once the seam is merged.
- The UI editor, negative-signal surfacing, a pre-execution query validator, and
  write-back to a source belong to the parallel effort and build on this seam.
