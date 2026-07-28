"""DuckDB ↔ Postgres repository METHOD-PARITY contract test.

Catches the drift class behind PRs #499, #513 and the latent gaps found in
their wake: a public method (or a keyword argument) exists on a DuckDB repo
but is missing from its ``_pg`` sibling. On a DuckDB-backed dev box every
call works; the gap only bites once a Postgres-backed instance is live —
``AttributeError`` (method absent) or ``TypeError`` (kwarg absent), surfaced
as a production 500 / crashed CLI command.

``test_schema_parity.py`` already guards *column* drift; this guards *API*
drift. Together with the per-cluster ``test_*_contract.py`` behavioural
tests, they enforce the CLAUDE.md "dual-backend discipline" rule
mechanically instead of by code review.

The check is static (AST) — no DB needed, so it runs everywhere regardless
of whether the PG test backend is installed.

How it works: for every ``src/repositories/<name>.py`` that has a
``<name>_pg.py`` sibling, every PUBLIC method on the DuckDB repo class(es)
must also exist on the PG side, and the DuckDB method's parameter names must
be a subset of the PG method's parameters (PG may add params with defaults;
it may not drop one the DuckDB caller relies on).

Intentional, documented asymmetries live in ``ALLOWED_METHOD_GAPS`` /
``ALLOWED_PARAM_GAPS`` — add to them only with a one-line justification.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[2] / "src" / "repositories"

# Files that are not DuckDB repos with a PG sibling (skip outright).
_NON_REPO = {"__init__.py", "factory.py", "audit_protocol.py"}

# ---------------------------------------------------------------------------
# Documented, intentional divergences. Each entry needs a reason.
# ---------------------------------------------------------------------------

# (repo_stem, method_name) entirely absent from the PG repo on purpose.
ALLOWED_METHOD_GAPS: dict[tuple[str, str], str] = {}

# (repo_stem, method_name) -> {param names allowed to be missing on PG}.
ALLOWED_PARAM_GAPS: dict[tuple[str, str], set[str]] = {
    # #499: DuckDB `knowledge.create(added_by=...)` attributes a
    # knowledge_item_domains join-table row. The PG repo stores `domain`
    # inline (no join table in this path) and no caller passes added_by to
    # create() — wiring it would be dead code. Documented inline in
    # knowledge_pg.py.
    ("knowledge", "create"): {"added_by"},
}


def _public_methods(path: Path) -> dict[str, set[str]]:
    """Return ``{method_name: {param_names}}`` across every class in *path*.

    Union across classes (a repo file may carry small helper classes); for a
    duplicated name the last definition wins, which is fine for an existence
    + param-subset check.
    """
    tree = ast.parse(path.read_text())
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and not item.name.startswith("_"):
                args = [a.arg for a in item.args.args if a.arg != "self"]
                kwonly = [a.arg for a in item.args.kwonlyargs]
                out[item.name] = set(args) | set(kwonly)
    return out


def _repo_pairs() -> list[tuple[str, Path, Path]]:
    pairs = []
    for duck in sorted(REPO_DIR.glob("*.py")):
        if duck.name in _NON_REPO or duck.name.endswith("_pg.py"):
            continue
        pg = REPO_DIR / f"{duck.stem}_pg.py"
        if pg.exists():
            pairs.append((duck.stem, duck, pg))
    return pairs


@pytest.mark.parametrize("stem,duck,pg", _repo_pairs(), ids=[p[0] for p in _repo_pairs()])
def test_pg_repo_has_every_duckdb_method(stem, duck, pg):
    """Every public DuckDB repo method must exist on the PG sibling with a
    compatible signature (PG params ⊇ DuckDB params)."""
    dm = _public_methods(duck)
    pm = _public_methods(pg)

    missing_methods = []
    param_drift = []
    for name, dparams in dm.items():
        if name not in pm:
            if ALLOWED_METHOD_GAPS.get((stem, name)):
                continue
            missing_methods.append(name)
            continue
        missing_params = dparams - pm[name]
        allowed = ALLOWED_PARAM_GAPS.get((stem, name), set())
        missing_params -= allowed
        if missing_params:
            param_drift.append(f"{name}() missing PG kwargs: {sorted(missing_params)}")

    problems = []
    if missing_methods:
        problems.append(
            f"{stem}_pg.py is missing method(s) present on DuckDB: "
            f"{sorted(missing_methods)} — port them (a Postgres-backed instance "
            f"will AttributeError when a caller reaches them)."
        )
    if param_drift:
        problems.append(
            f"{stem}_pg.py signature drift: {param_drift} — a caller passing "
            f"those kwargs works on DuckDB but TypeErrors on Postgres."
        )
    assert not problems, "\n".join(problems)
