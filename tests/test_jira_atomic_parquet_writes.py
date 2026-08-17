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
recorded two incidents the extracted helper now carries for every writer: a shared temp
name raced two writers (#1274), and `tempfile.mkstemp` + `os.replace` republished the
parquet 0600 (#203).

Layered like `test_jira_webhook_transform_paths.py`: behavioural tests driving the
writers, plus a source-level sweep so a *future* writer cannot reintroduce a direct
write.
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

CONNECTOR_ROOT = Path(__file__).resolve().parent.parent / "connectors" / "jira"
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
# Source-level sweep: no future writer may publish directly.
# --------------------------------------------------------------------------------


class _PublishSweep(ast.NodeVisitor):
    """Collect every direct parquet publish, with the function that encloses it.

    One pass with an explicit stack — the enclosing name is correct by
    construction for nested defs, which a two-pass `id()`-keyed map got wrong.

    Matches all three spellings a writer reaches for, not just the one that
    caused this bug: `pq.write_table(...)`, `df.to_parquet(...)`, and a DuckDB
    `COPY … TO '<path>' (FORMAT PARQUET)`. The narrow version would have waved
    through a future writer that used the shortest path.
    """

    _CALL_NAMES = {"write_table", "to_parquet"}

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: list[str] = ["<module>"]
        self.found: list[tuple[str, int, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in self._CALL_NAMES:
            self.found.append((self.rel, node.lineno, self.stack[-1]))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            sql = " ".join(node.value.upper().split())
            if "COPY" in sql and "FORMAT PARQUET" in sql:
                self.found.append((self.rel, node.lineno, self.stack[-1]))
        self.generic_visit(node)


def _publish_sites() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for py in sorted(CONNECTOR_ROOT.rglob("*.py")):
        if "tests" in py.parts:
            continue
        sweep = _PublishSweep(str(py.relative_to(CONNECTOR_ROOT)))
        sweep.visit(ast.parse(py.read_text(), filename=str(py)))
        found.extend(sweep.found)
    return found


def test_the_only_parquet_publish_in_the_connector_is_the_atomic_helper() -> None:
    """A direct write onto a published path is the bug this file exists for.
    Funnelling every writer through one helper is what makes that
    unrepresentable, so a new call site has to justify itself here.

    Scope note: this sweeps `connectors/jira` only. The same invariant holds for
    every connector — several publish parquet onto live paths, and the ones that
    do use temp+replace use a shared (non-per-process) temp name and no chmod,
    i.e. exactly the #1274 and #203 defects the helper encodes. Widening this
    guard belongs with the move that makes the helper shared.
    """
    sites = _publish_sites()
    assert sites, "sweep found no writes at all — it has stopped working"
    offenders = [s for s in sites if s[2] != "write_parquet_atomic"]
    assert offenders == [], (
        f"parquet must be published via `write_parquet_atomic` (temp + os.replace); direct writes found: {offenders}"
    )
