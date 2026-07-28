# Playbook: schema migration (both ladders)

Two ladders must reach the SAME endpoint: the DuckDB ladder in `src/db.py` and the
Alembic ladder in `migrations/versions/`. The Postgres model in `src/db_pg.py`
(`Base.metadata`) must also match, or autogenerate-drift fails.

## DuckDB side (`src/db.py`)

1. Bump `SCHEMA_VERSION` (currently 72 at `src/db.py:50`) → next integer.
2. Write `def _v72_to_v73(conn): ...` ending with
   `conn.execute("UPDATE schema_version SET version = 73")`. Use idempotent
   `CREATE ... IF NOT EXISTS`. Worked example: `_v71_to_v72` at `src/db.py:4905`.
3. Wire it into `_ensure_schema` in BOTH places:
   - the `current == 0` fresh-install block (after the prior `_vNN` call);
   - the upgrade block: `if current < 73: _v72_to_v73(conn)` after the `< 72` guard.

## Alembic side (`migrations/versions/`)

Create `migrations/versions/00NN_<desc>_v73.py` (naming: `NNNN_<desc>_v<duckdb_version>.py`):

```python
revision = "00NN_<desc>_v73"
down_revision = "<previous revision id>"   # chain to the current head

def upgrade() -> None: ...   # op.create_table / op.add_column
def downgrade() -> None: ...  # exact inverse
```

Then update `src/db_pg.py` `Base.metadata` (the SQLAlchemy models) to match the
new structural change.

## Integration gates

- `tests/test_db_schema_version.py` — drives old DuckDB files up the ladder and
  asserts they reach `SCHEMA_VERSION`. Fails if a `_vN_to_v(N+1)` fn or its
  dispatch guard is missing.
- `tests/db_pg/test_alembic_roundtrip.py` — upgrade/downgrade roundtrips +
  `test_no_model_migration_drift` (autogenerate diff vs `Base.metadata` must be
  empty → this is why you update `src/db_pg.py`).

## Steps

1. TDD: add the schema-version test expectation / a test for the new table.
2. DuckDB: bump version, write `_vN_to_v(N+1)`, wire both dispatch sites.
3. Alembic: new revision (up + down), chained to head.
4. Update `src/db_pg.py` `Base.metadata`.
5. Green `test_db_schema_version.py` + `test_alembic_roundtrip.py`.

## Anchors

- `SCHEMA_VERSION`: `src/db.py:50`; recent migration fn: `src/db.py:4905`
- recent revision: `migrations/versions/0019_system_secrets_v72.py`
- gates: `tests/test_db_schema_version.py`, `tests/db_pg/test_alembic_roundtrip.py`
