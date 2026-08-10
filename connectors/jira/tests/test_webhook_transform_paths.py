"""
The webhook's incremental transform must read from the directory `save_issue` writes to.

Regression guard. `trigger_incremental_transform` used to call `transform_single_issue`
with no paths at all, so the transform resolved its own default — `$DATA_DIR/extracts/
<source>/raw`, derived from DATA_DIR and therefore unrelated to JIRA_DATA_DIR. Wherever
the two did not coincide (including the default configuration, where JIRA_DATA_DIR is
unset and the writer lands on the legacy raw path) every webhook logged "Issue JSON not
found" and still answered Jira 200, so edits to issues already in the parquet stopped
landing while new issues kept appearing via the consistency check's backfill — a
failure mode with no error surface anywhere.

The assertion that matters is not "some path is passed" but "the path passed is the one
the writer uses", which is what these tests pin.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from connectors.jira import incremental_transform as jira_incremental
from connectors.jira.service import Config, JiraService, trigger_incremental_transform

RAW_DIR = Path("/srv/jira/raw")
PARQUET_DIR = Path("/srv/jira/parquet")


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture the kwargs forwarded to `transform_single_issue`.

    Returns False so the caller short-circuits before the jobs-queue enqueue —
    this test is about path plumbing, not about the rebuild that follows it.
    `trigger_incremental_transform` imports the symbol inside the function body,
    so patching the module attribute is what the call actually resolves.
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
    # Stated against the writer itself, so the two cannot drift apart again.
    assert captured["raw_dir"] == JiraService().data_dir


def test_output_dir_is_forwarded_when_configured(captured: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Config, "JIRA_PARQUET_DIR", PARQUET_DIR)

    trigger_incremental_transform("PROJ-1234")

    assert captured["output_dir"] == PARQUET_DIR


def test_unset_output_dir_defers_to_the_transform_default(captured: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset must stay None.

    `incremental_transform` then applies the extract.duckdb-contract location. Passing a
    default from here would have to pick one, and the only other candidate in the
    connector is the legacy Data Broker path — which would move webhook writes off the
    served layout on every deployment that leaves the variable unset.
    """
    monkeypatch.setattr(Config, "JIRA_PARQUET_DIR", None)

    trigger_incremental_transform("PROJ-1234")

    assert captured["output_dir"] is None


def test_deletion_carries_the_same_paths(captured: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deletions rewrite the same parquet and need the same anchoring."""
    monkeypatch.setattr(Config, "JIRA_DATA_DIR", RAW_DIR)
    monkeypatch.setattr(Config, "JIRA_PARQUET_DIR", PARQUET_DIR)

    trigger_incremental_transform("PROJ-1234", deleted=True)

    assert captured["deleted"] is True
    assert captured["raw_dir"] == RAW_DIR
    assert captured["output_dir"] == PARQUET_DIR
