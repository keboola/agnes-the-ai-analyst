"""
Every caller of `transform_single_issue` must tell it where the JSON was written.

Regression guard. `trigger_incremental_transform` used to call it with no paths, so the
transform resolved its own default — `$DATA_DIR/extracts/<source>/raw`, derived from
DATA_DIR and therefore unrelated to JIRA_DATA_DIR, which is what the writers use. Wherever
the two did not coincide (including the default configuration) every webhook logged "Issue
JSON not found" and still answered Jira 200, so edits to issues already in the parquet
stopped landing while new issues kept appearing via the consistency check's key-based
backfill — a failure with no error surface anywhere.

Two layers here, because the behavioural test only covers the call site it drives:
  * the webhook path, asserted against `JiraService().data_dir` itself rather than a
    literal, so reader and writer cannot drift apart again;
  * a source-level sweep over the whole connector, so a *future* call site (or the SLA
    poller, which had the identical bug) cannot reintroduce it unnoticed.

`output_dir` is deliberately left unset by callers: its default is the
extract.duckdb-contract location the orchestrator serves, whereas `JIRA_PARQUET_DIR` — the
obvious thing to forward — means the legacy Data Broker root across this connector.
"""

import ast
from pathlib import Path

import pytest

from connectors.jira import incremental_transform as jira_incremental
from connectors.jira.service import Config, JiraService, trigger_incremental_transform

RAW_DIR = Path("/srv/jira/raw")
CONNECTOR_ROOT = Path(__file__).resolve().parent.parent / "connectors" / "jira"


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture the kwargs forwarded to `transform_single_issue`.

    Returns False so the caller short-circuits before the jobs-queue enqueue — this is
    about path plumbing, not the rebuild that follows. `trigger_incremental_transform`
    imports the symbol inside the function body, so patching the module attribute is
    what the call actually resolves.
    """
    seen: dict = {}

    def _fake_transform(**kwargs):
        seen.update(kwargs)
        return False

    monkeypatch.setattr(jira_incremental, "transform_single_issue", _fake_transform)
    return seen


def test_raw_dir_is_where_the_service_saves(captured: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """The read side and the write side must resolve to one directory."""
    monkeypatch.setattr(Config, "JIRA_DATA_DIR", RAW_DIR)

    trigger_incremental_transform("PROJ-1234")

    assert captured["raw_dir"] == RAW_DIR
    assert captured["raw_dir"] == JiraService().data_dir


def test_output_dir_is_left_to_the_transform_default(captured: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not passing it is the point — see the module docstring."""
    monkeypatch.setattr(Config, "JIRA_DATA_DIR", RAW_DIR)

    trigger_incremental_transform("PROJ-1234")

    assert "output_dir" not in captured


def test_deletion_carries_the_same_anchoring(captured: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deletions rewrite the same parquet and need the same directory."""
    monkeypatch.setattr(Config, "JIRA_DATA_DIR", RAW_DIR)

    trigger_incremental_transform("PROJ-1234", deleted=True)

    assert captured["deleted"] is True
    assert captured["raw_dir"] == RAW_DIR


def _transform_call_sites() -> list[tuple[str, int, set[str]]]:
    """(relative path, line, keyword names) for every `transform_single_issue(...)` call.

    Read off the source rather than by importing, so a call site inside a `__main__`
    block or a script that needs env at import time is still covered.
    """
    sites: list[tuple[str, int, set[str]]] = []
    for path in sorted(CONNECTOR_ROOT.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "transform_single_issue":
                continue
            keywords = {kw.arg for kw in node.keywords if kw.arg}
            sites.append((str(path.relative_to(CONNECTOR_ROOT)), node.lineno, keywords))
    return sites


def test_the_sweep_actually_finds_call_sites() -> None:
    """A guard that silently matches nothing asserts nothing."""
    assert len(_transform_call_sites()) >= 2


def test_every_caller_passes_raw_dir() -> None:
    """No call site may fall back to the DATA_DIR-derived default."""
    offenders = [f"{path}:{line}" for path, line, keywords in _transform_call_sites() if "raw_dir" not in keywords]

    assert not offenders, (
        "transform_single_issue called without raw_dir at: "
        + ", ".join(offenders)
        + " — it would resolve $DATA_DIR/extracts/<source>/raw instead of the "
        "directory the caller wrote the JSON to."
    )
