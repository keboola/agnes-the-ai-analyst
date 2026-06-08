# Playbook: repository / method with DuckDB↔Postgres parity

Both backends are first-class. A one-sided change is a BLOCKING parity gap caught
by guards (below). Reach repos via the `*_repo()` factory, never instantiate a
repo class directly.

## Files (a new repo touches all four)

1. `src/repositories/<name>.py` — DuckDB impl: `class <Name>Repository` taking a
   `duckdb.DuckDBPyConnection` in `__init__`; positional `?` bindings; returns
   plain `dict`/`list[dict]`/`None`. Shape: `src/repositories/sync_state.py:10`.
2. `src/repositories/<name>_pg.py` — Postgres impl: `class <Name>PgRepository`
   taking a SQLAlchemy `Engine`; `sa.text(...)` with `:named` binds; reads under
   `with self._engine.connect()`, writes under `with self._engine.begin()`. Shape:
   `src/repositories/sync_state_pg.py:17`.
3. `src/repositories/__init__.py` — THREE edits:
   - add `"<name>_repo"` to `__all__`;
   - add a `_REGISTRY` entry: `"<name>": {DUCKDB: ("src.repositories.<name>", "<Name>Repository"), PG: ("src.repositories.<name>_pg", "<Name>PgRepository")}`;
   - add the factory fn `def <name>_repo() -> Any: return _build("<name>")`.
4. `tests/db_pg/test_<name>_contract.py` — parametrize `["duckdb", "pg"]` through
   the same assertions. Model it on `tests/db_pg/test_mcp_sources_contract.py`.

## Method-mirroring rule

Every public method on the DuckDB class must exist on the PG class with identical
parameter names (PG may add defaulted params, never drop). This is an AST check —
no DB needed.

## Guards that fail if you skip a step

| Skipped | Failing test |
|---|---|
| `_REGISTRY` entry / asymmetric backends | `tests/test_repository_registry.py::test_registry_backends_are_symmetric` |
| `__all__` / factory fn | `tests/test_repository_registry.py::test_every_public_factory_has_a_registry_entry` |
| PG missing a public method | `tests/db_pg/test_repo_method_parity.py` |
| Direct `XRepository(conn)` instead of factory | `tests/test_backend_split_guard.py` |
| `get_system_db()` in a handler | `tests/test_backend_split_guard.py` |
| Semantic drift (e.g. JSON dict vs str) | your `tests/db_pg/test_<name>_contract.py` |

## Steps

1. TDD: write `tests/db_pg/test_<name>_contract.py` first (it fails — no repo).
2. Write the DuckDB repo, then the PG repo (mirror signatures).
3. Make the three `__init__.py` edits.
4. Green the contract test + registry/parity guards.

## Anchors

- factory `_build` / `_REGISTRY` / `_ARG_PROVIDERS`: `src/repositories/__init__.py`
- paired example: `src/repositories/sync_state.py` + `src/repositories/sync_state_pg.py`
- contract example: `tests/db_pg/test_mcp_sources_contract.py`
