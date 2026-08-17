"""Every Jira partition write publishes via temp + ``os.replace``, and an
unreadable partition is never silently treated as empty.

`pq.write_table()` wrote straight onto the live `month=YYYY-MM/data.parquet`, so a
crash mid-write (deploy, OOM, restart) left a parquet with no valid footer. That did
not stay a read error — `load_parquet_month` answered `None`, `upsert_dataframe` read
that as "empty", and the month was republished holding only the record being upserted.
A 400-issue month became a 1-issue month, independently for all six tables, behind
nothing louder than a WARNING, and nothing restored it: the SLA poller revisits only
tickets whose `status_category != 'Done'`.

Two halves, and both are needed. Atomic writes remove the likeliest *producer* of an
unreadable partition; `_read_or_raise` removes the *amplifier*, so the remaining
producers (disk error, truncated restore, non-atomic `os.replace` on NFS/overlay) cost
one erroring transform instead of a month of history.

`connectors/jira/organizations.py` already published atomically, and its comments
recorded two incidents the mechanism now carries for every writer, everywhere (moved
to `src/parquet_publish.py` in #1359, shared by the ten other publish sites that same
issue found — the Keboola/BigQuery/MCP connectors and `src/ingest/tabular.py`): a
shared temp name raced two writers (#1274), and `tempfile.mkstemp` + `os.replace`
republished the parquet 0600 (#203).

Layered like `test_jira_webhook_transform_paths.py`: behavioural tests driving the
Jira writers, plus a source-level sweep — widened by #1359 from `connectors/jira` to
every connector plus `src/ingest/tabular.py` (the module docstring on
`_publish_sites` below states the exact scope and why) — so a *future* writer, in any
of them, cannot reintroduce a direct write.
"""

import ast
import logging
import os
from pathlib import Path

import pandas as pd
import pytest

from connectors.jira import incremental_transform as jira_incremental
from connectors.jira import transform as jira_transform
from tests.test_jira_hive_parquet import _make_raw_issue, _write_raw_issues

REPO_ROOT = Path(__file__).resolve().parent.parent
CONNECTOR_ROOT = REPO_ROOT / "connectors" / "jira"
SCHEMA = {"issue_key": "string"}
MONTH = "2025-06"
# What a killed write leaves behind: the magic bytes, no footer.
FOOTERLESS = b"PAR1" + b"\x00" * 64


def _write_hive(table_dir: Path, *keys: str) -> None:
    rows = pd.DataFrame([{"issue_key": k} for k in keys])
    jira_transform.write_hive_parquet(jira_transform.apply_schema(rows, SCHEMA), table_dir, MONTH)


def _write_month(table_dir: Path, *keys: str) -> None:
    jira_incremental.save_parquet_month(pd.DataFrame([{"issue_key": k} for k in keys]), SCHEMA, table_dir, MONTH)


# Both writers publish through `write_parquet_atomic`; every guarantee below has to
# hold for each, so they share one parametrization rather than four hand-rolled
# if/else branches.
writers = pytest.mark.parametrize("write", [_write_hive, _write_month], ids=["hive", "month"])


def _keys(path: Path) -> list[str]:
    return sorted(pd.read_parquet(path)["issue_key"].tolist())


def _boom(_table, where, **_kwargs):
    """Model a write that DIES MIDWAY, not one that never starts.

    A no-op stub would leave the destination untouched and pass even against the
    unfixed writers, which is the whole bug. So emit a footerless prefix at the
    path the writer chose — exactly what a SIGKILL'd `pq.write_table` leaves —
    and only then raise. If that path is the live file, the month is now
    unreadable; if it is a temp, nothing published is harmed.
    """
    Path(where).write_bytes(FOOTERLESS)
    raise OSError("disk full mid-write")


# --------------------------------------------------------------------------------
# A failed write must not damage what is already published.
# --------------------------------------------------------------------------------


@writers
def test_a_write_that_dies_midway_leaves_the_published_month_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write
) -> None:
    table_dir = tmp_path / "issues"
    write(table_dir, "SUPPORT-1", "SUPPORT-2")
    dest = table_dir / f"month={MONTH}" / "data.parquet"
    assert _keys(dest) == ["SUPPORT-1", "SUPPORT-2"]

    # Patched on `transform`: both writers publish through `write_parquet_atomic`,
    # which lives there.
    monkeypatch.setattr(jira_transform.pq, "write_table", _boom)
    with pytest.raises(OSError):
        write(table_dir, "SUPPORT-3")

    assert _keys(dest) == ["SUPPORT-1", "SUPPORT-2"]


@writers
def test_failed_write_leaves_no_temp_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write) -> None:
    table_dir = tmp_path / "issues"
    write(table_dir, "SUPPORT-1")

    monkeypatch.setattr(jira_transform.pq, "write_table", _boom)
    with pytest.raises(OSError):
        write(table_dir, "SUPPORT-1")

    leftovers = [p.name for p in (table_dir / f"month={MONTH}").iterdir() if p.name != "data.parquet"]
    assert leftovers == [], f"temp file left behind: {leftovers}"


# --------------------------------------------------------------------------------
# The temp must be invisible to every reader, and the published mode must not
# depend on the umask (incident #203).
# --------------------------------------------------------------------------------


def test_temp_is_per_process_and_never_matches_the_parquet_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []
    real = jira_transform.pq.write_table

    def _record(table, where, **kw):
        seen.append(Path(where))
        return real(table, where, **kw)

    monkeypatch.setattr(jira_transform.pq, "write_table", _record)
    table_dir = tmp_path / "issues"
    _write_hive(table_dir, "SUPPORT-1")

    assert seen, "write_table was never called"
    tmp_target = seen[0]
    assert tmp_target.name != "data.parquet", "wrote straight onto the live path"
    assert str(os.getpid()) in tmp_target.name, "temp name must be per-process (#1274)"
    # Every reader globs `*.parquet` — extract views, `_hash_table_parts`,
    # `find_open_issues`. A temp that matched would be served mid-write.
    assert not list((table_dir / f"month={MONTH}").glob("*.parquet.*"))
    assert [p.name for p in (table_dir / f"month={MONTH}").glob("*.parquet")] == ["data.parquet"]


@writers
def test_published_mode_is_0644_even_under_a_restrictive_umask(tmp_path: Path, write) -> None:
    """`pq.write_table` creates as 0666 & umask and `os.replace` preserves the
    mode, so a 0077 umask (seen in container/systemd units) would publish 0600
    and the server process could no longer read its own parquet — incident #203."""
    table_dir = tmp_path / "issues"
    previous = os.umask(0o077)
    try:
        write(table_dir, "SUPPORT-1")
    finally:
        os.umask(previous)

    dest = table_dir / f"month={MONTH}" / "data.parquet"
    assert oct(dest.stat().st_mode & 0o777) == oct(0o644)


# --------------------------------------------------------------------------------
# An unreadable partition must stop the write, not redefine it as empty.
# --------------------------------------------------------------------------------


def test_a_corrupt_partition_raises_instead_of_reading_as_empty(tmp_path: Path) -> None:
    out = tmp_path / "issues"
    _write_month(out, "SUPPORT-1", "SUPPORT-2")
    dest = out / f"month={MONTH}" / "data.parquet"

    dest.write_bytes(FOOTERLESS)
    with pytest.raises(jira_incremental.UnreadablePartitionError):
        jira_incremental.load_parquet_month(out, MONTH)


def test_a_missing_partition_is_still_legitimately_empty(tmp_path: Path) -> None:
    """The distinction the old code failed to make: absent means empty, present
    but unreadable does not."""
    assert jira_incremental.load_parquet_month(tmp_path / "issues", MONTH) is None


def test_a_corrupt_partition_is_never_overwritten_with_a_single_row(tmp_path: Path) -> None:
    """The whole point, end to end: the exact sequence that used to collapse a
    month to one row now refuses, and the (bad) bytes are left for an operator
    rather than replaced by a plausible-looking one-row parquet."""
    out = tmp_path / "issues"
    _write_month(out, "SUPPORT-1", "SUPPORT-2", "SUPPORT-3")
    dest = out / f"month={MONTH}" / "data.parquet"
    dest.write_bytes(FOOTERLESS)

    with pytest.raises(jira_incremental.UnreadablePartitionError):
        existing = jira_incremental.load_parquet_month(out, MONTH)
        jira_incremental.upsert_dataframe(existing, [{"issue_key": "SUPPORT-9"}], "issue_key", "SUPPORT-9")

    assert dest.read_bytes() == FOOTERLESS, "the corrupt partition was overwritten anyway"


def test_the_webhook_wrapper_reports_false_and_leaves_the_corrupt_bytes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The same guarantee one layer up, through the wrapper the webhook actually
    calls: `transform_single_issue` against a corrupt month answers False (its
    blanket `except Exception` absorbs `UnreadablePartitionError`), never
    publishes the issues row, and leaves the bad bytes for an operator. Pinned
    so a future "resilience" change that catches the error and treats the month
    as empty cannot quietly recreate the one-row-month collapse on the webhook
    path. The OTHER tables may legitimately advance before the failure —
    `_TABLES` writes `issues` last precisely so the ticket stays open and the
    whole month is retried next cycle.
    """
    raw_dir = tmp_path / "raw"
    out = tmp_path / "parquet"
    _write_raw_issues(raw_dir, [_make_raw_issue("PROJ-1", f"{MONTH}-15T00:00:00.000+0000")])
    dest = out / "issues" / f"month={MONTH}" / "data.parquet"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FOOTERLESS)

    with caplog.at_level(logging.ERROR):
        ok = jira_incremental.transform_single_issue(
            issue_key="PROJ-1", raw_dir=raw_dir, output_dir=out, attachments_dir=tmp_path / "att"
        )

    assert ok is False
    # Pin the cause: the refusal tripped the wrapper, not some fixture accident
    # failing an earlier transform step behind the same blanket except.
    assert "could not be read" in caplog.text
    assert dest.read_bytes() == FOOTERLESS, "the corrupt partition was overwritten anyway"


# --------------------------------------------------------------------------------
# Source-level sweep: no future writer, in any connector, may publish directly.
# --------------------------------------------------------------------------------
#
# The guard's actual contract: every `pq.write_table` / `<df>.to_parquet` / DuckDB
# `COPY … FORMAT PARQUET` that publishes a file into a connector's extract-layout
# directory (`/data/extracts/<source>/data/*.parquet` or the Jira hive-partitioned
# equivalent — the tree `src/orchestrator.py`'s hasher and master-view glob treat as
# authoritative, and `agnes pull` distributes) must go through
# `src.parquet_publish` (#1359). It is NOT "every parquet write anywhere in the
# repo" — the CLI's analyst-local snapshot cache (`cli/commands/snapshot.py`), an
# in-memory HTTP export buffer (`app/api/admin_usage.py`), and dev tooling
# (`connectors/*/scripts/`) all write parquet too, but none of them is an
# extract-layout publish that multiple unrelated readers depend on, so they're out
# of this sweep's root entirely rather than allowlisted — allowlisting would suggest
# they're offenders that happen to be excused, when they're simply not the thing
# this invariant is about.

# Every connector, not just Jira — `connectors/*/tests/` and `connectors/*/scripts/`
# excluded: the former is test fixtures, the latter is operator/benchmark tooling
# (e.g. `connectors/jira/scripts/bloom_benchmark.py` writes its own comparison
# parquet on a synthetic corpus, never touching a real extract tree) rather than
# code any sync path executes. `src/ingest/tabular.py` is the one `src/`-rooted
# publish site #1359 named; `src.parquet_publish` itself is excluded by construction
# (it's under `src/`, not `connectors/`, and isn't the tabular-ingest file).
SWEEP_ROOTS = [REPO_ROOT / "connectors"]
EXTRA_SWEEP_FILES = [REPO_ROOT / "src" / "ingest" / "tabular.py"]
_EXCLUDED_PATH_PARTS = {"tests", "scripts"}

# Names that prove a function participates in the shared publish protocol
# (`src/parquet_publish.py`). Deliberately looser than "the write is lexically
# inside a `with atomic_publish(...):` block": a few call sites (the BigQuery and
# Keboola `materialize_query` functions) span retries and several branches too
# spread out to nest inside one `with`, so they use the two-step
# `atomic_publish_temp_path` + `atomic_publish_finalize` form instead. Requiring
# only "the enclosing function references one of these names somewhere" still makes
# the actual bug class — a function that stages nothing and writes straight onto
# the served path — unrepresentable, without forcing every call site into one
# lexical shape.
_PUBLISH_PRIMITIVE_NAMES = {"atomic_publish", "atomic_publish_temp_path", "atomic_publish_finalize"}

# Sites deliberately left unconverted — shrink-only, never grown without a reason
# attached. Keyed by (path relative to repo root, enclosing function name).
_ALLOWED_OFFENDERS: dict[tuple[str, str], str] = {
    ("connectors/databricks/extractor.py", "_write_batches_to_parquet"): (
        "Not a publish: this helper streams record batches into a path its CALLER "
        "hands it, and that caller (`materialize_query`) obtains the path from "
        "`atomic_publish_temp_path` and commits it with `atomic_publish_finalize`. "
        "The sweep keys on the enclosing function, and this one is module-level "
        "rather than nested inside its caller (unlike BigQuery's `_copy_attempt` "
        "closure, which the reference-walk does reach), so it cannot see the "
        "protocol its argument already came from. Allowlisted as a limitation of "
        "the detection, not as an unconverted site — verify by reading "
        "`materialize_query`, which is where the publish actually happens."
    ),
}


class _PublishSweep(ast.NodeVisitor):
    """Collect every direct parquet publish, with the function that encloses it.

    One pass with an explicit stack — the enclosing name is correct by
    construction for nested defs, which a two-pass `id()`-keyed map got wrong.

    Matches all three spellings a writer reaches for, not just the one that
    caused this bug: `pq.write_table(...)`, `df.to_parquet(...)`, and a DuckDB
    `COPY … TO '<path>' (FORMAT PARQUET)`. The narrow version would have waved
    through a future writer that used the shortest path.

    The COPY spelling is matched on `JoinedStr` (an f-string), not only bare
    `Constant` — every real COPY call site in this codebase interpolates the
    identifier/path between "COPY" and "FORMAT PARQUET"
    (``f"COPY (...) TO '{path}' (FORMAT PARQUET)"``), which the parser splits
    into several `Constant` fragments around each `{...}`. A per-fragment-only
    check never sees "COPY" and "FORMAT PARQUET" in the SAME fragment and so
    never matches any of them — a real gap this sweep had until widening it
    surfaced it. Reconstructing the joined static text (ignoring the
    interpolated parts) fixes that without losing the ability to catch a
    plain non-f-string literal too.

    Docstrings are excluded from the Constant/JoinedStr scan (`docstring_ids`,
    computed once per file by `_docstring_const_ids`) — otherwise prose that
    happens to mention both "COPY" and "FORMAT PARQUET" false-positives. Real
    example hit while widening this sweep:
    `connectors/bigquery/extractor.py::materialize_query`'s own docstring
    reads "``COPY (...) TO 'path' (FORMAT PARQUET)``." as documentation, not
    code.
    """

    _CALL_NAMES = {"write_table", "to_parquet"}

    def __init__(self, rel: str, docstring_ids: set[int]) -> None:
        self.rel = rel
        self._docstring_ids = docstring_ids
        self.stack: list[str] = ["<module>"]
        # Parallel stack: does the CURRENT innermost enclosing function's body
        # reference a publish-protocol name anywhere (not necessarily wrapping
        # the write)? Module scope is never "safe" — no real site writes there,
        # and treating "referenced anywhere in the whole file" as sufficient
        # would let an unrelated new bad site ride along on a distant good one.
        #
        # A nested function INHERITS its enclosing function's safety (see
        # visit_FunctionDef) rather than only checking its own body — a real
        # site needs this: `connectors/bigquery/extractor.py::materialize_query`
        # nests a nine-line `_copy_attempt()` closure that builds the actual COPY
        # string, while the temp-path and commit calls live in the OUTER
        # function (retried by calling `_copy_attempt()` a second time on a
        # fresh session). Without inheritance this sweep would demand the commit
        # calls move into the inner closure for no reason but satisfying a lint.
        self.safe_stack: list[bool] = [False]
        self.found: list[tuple[str, int, str]] = []

    def _references_publish_primitive(self, node: ast.AST) -> bool:
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Name) and func.id in _PUBLISH_PRIMITIVE_NAMES:
                return True
            if isinstance(func, ast.Attribute) and func.attr in _PUBLISH_PRIMITIVE_NAMES:
                return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        inherited = self.safe_stack[-1]
        self.safe_stack.append(inherited or self._references_publish_primitive(node))
        self.generic_visit(node)
        self.safe_stack.pop()
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _record(self, node: ast.AST) -> None:
        if not self.safe_stack[-1]:
            self.found.append((self.rel, node.lineno, self.stack[-1]))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in self._CALL_NAMES:
            self._record(node)
        self.generic_visit(node)

    def _check_sql_text(self, node: ast.AST, text: str) -> None:
        sql = " ".join(text.upper().split())
        if "COPY" in sql and "FORMAT PARQUET" in sql:
            self._record(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if id(node) not in self._docstring_ids and isinstance(node.value, str):
            self._check_sql_text(node, node.value)
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        # f-strings can never be a docstring (Python only recognizes a bare
        # `Constant` there), so no exclusion check needed on this branch.
        text = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        self._check_sql_text(node, text)
        self.generic_visit(node)


def _docstring_const_ids(tree: ast.AST) -> set[int]:
    """`id()`s of `Constant` nodes that ARE a Module/Class/Function's own
    docstring. A separate pass so `_PublishSweep` doesn't have to special-case
    "is this string literally the first statement of an enclosing def/class/
    module" mid-traversal."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _iter_swept_files():
    for root in SWEEP_ROOTS:
        for py in sorted(root.rglob("*.py")):
            if _EXCLUDED_PATH_PARTS & set(py.relative_to(root).parts):
                continue
            yield py
    yield from EXTRA_SWEEP_FILES


def _publish_sites() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for py in _iter_swept_files():
        tree = ast.parse(py.read_text(), filename=str(py))
        rel = str(py.relative_to(REPO_ROOT))
        sweep = _PublishSweep(rel, _docstring_const_ids(tree))
        sweep.visit(tree)
        found.extend(sweep.found)
    return found


def test_the_only_parquet_publish_in_a_connector_is_the_shared_atomic_helper() -> None:
    """A direct write onto a published path is the bug this file exists for.
    Funnelling every writer through `src.parquet_publish` is what makes that
    unrepresentable, so a new call site has to justify itself here.

    Widened by #1359 from `connectors/jira` only to every connector plus
    `src/ingest/tabular.py` — see the module comment above `SWEEP_ROOTS` for
    the exact scope and why, and `_ALLOWED_OFFENDERS` for what's deliberately
    still red and why.
    """
    sites = _publish_sites()
    assert sites, "sweep found no writes at all — it has stopped working"
    offenders = [s for s in sites if (s[0], s[2]) not in _ALLOWED_OFFENDERS]
    assert offenders == [], (
        f"parquet must be published via src.parquet_publish (temp + chmod + os.replace); "
        f"direct writes found: {offenders}"
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    """Shrink-only: an allowlisted (path, function) that the sweep no longer
    finds as an offender means the underlying code changed (converted, moved,
    or deleted) — the entry must be removed in the same change, not left to
    silently stop meaning anything."""
    sites = {(s[0], s[2]) for s in _publish_sites()}
    stale = [key for key in _ALLOWED_OFFENDERS if key not in sites]
    assert stale == [], f"allowlist entries no longer found by the sweep (remove them): {stale}"
