"""The SLA poller applies a month's tickets in ONE read-modify-write per table.

`transform_single_issue` was built for the webhook path, where one event is one
issue and rewriting that issue's whole month partition is the correct granularity.
`poll_sla` reused it inside a bulk loop over every open ticket, so a month holding
N polled tickets was rewritten N times per cycle — six whole partitions each pass,
producing the same bytes the last pass produced.

Batching keeps the writes and drops the repetition: group the run's tickets by
month, then apply all of a month's updates against one load and one save per table.

Two invariants this file pins, both load-bearing and neither obvious:

  * **Equivalence.** Batched output must equal what the per-issue path produces for
    the same inputs, including the two tri-state rules that are decided PER ISSUE
    against shared state — a `_comments_incomplete` marker preserves that issue's
    stored thread, and an absent `_remote_links` overlay preserves its stored links.
    Flattening those to a per-batch decision would silently wipe threads.
  * **Lock discipline.** `file_lock.py` documents the nesting as
    `issue_json_lock` (outer) -> `parquet_month_lock` (inner). The batch path holds
    the month lock across many issues, so it must never reach for an issue lock
    inside it — that inverts the order against the webhook path and deadlocks. It
    must also read each issue's JSON *inside* the month lock: a webhook that lands
    between an outside-the-lock read and the write would be clobbered.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from connectors.jira import incremental_transform as jira_incremental

MONTH = "2025-06"


def _raw_issue(key: str, *, created: str = "2025-06-15T10:00:00.000+0000", comments: int = 1) -> dict:
    return {
        "key": key,
        "id": key.split("-")[-1],
        "fields": {
            "summary": f"summary for {key}",
            "created": created,
            "updated": created,
            "status": {"name": "New", "statusCategory": {"name": "To Do"}},
            "issuetype": {"name": "Task"},
            "project": {"key": "PROJ", "name": "Project"},
            "comment": {
                "total": comments,
                "maxResults": comments,
                "startAt": 0,
                "comments": [
                    {
                        "id": f"{i}",
                        "body": f"comment {i} on {key}",
                        "created": created,
                        "updated": created,
                        "author": {"displayName": "A", "emailAddress": "a@example.com"},
                    }
                    for i in range(comments)
                ],
            },
        },
    }


@pytest.fixture()
def tree(tmp_path: Path):
    raw = tmp_path / "raw"
    (raw / "issues").mkdir(parents=True)
    out = tmp_path / "data"
    out.mkdir()
    return raw, out


def _write_raw(raw: Path, issue: dict) -> None:
    (raw / "issues" / f"{issue['key']}.json").write_text(json.dumps(issue))


def _rows(out: Path, table: str, month: str = MONTH) -> pd.DataFrame:
    path = out / table / f"month={month}" / "data.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


# --------------------------------------------------------------------------------
# Equivalence with the per-issue path.
# --------------------------------------------------------------------------------


def test_batch_matches_per_issue_output_for_the_same_inputs(tree, tmp_path: Path) -> None:
    raw, out_batch = tree
    out_single = tmp_path / "data_single"
    out_single.mkdir()
    keys = ["PROJ-101", "PROJ-102", "PROJ-103"]
    for k in keys:
        _write_raw(raw, _raw_issue(k, comments=2))

    for k in keys:
        assert jira_incremental.transform_single_issue(k, raw_dir=raw, output_dir=out_single)
    jira_incremental.transform_issues(keys, raw_dir=raw, output_dir=out_batch)

    for table in ("issues", "comments", "attachments", "changelog", "issuelinks", "remote_links"):
        a, b = _rows(out_single, table), _rows(out_batch, table)
        key = list(a.columns)[:2]
        pd.testing.assert_frame_equal(
            a.sort_values(by=key).reset_index(drop=True),
            b.sort_values(by=key).reset_index(drop=True),
            check_like=True,
            obj=table,
        )


def test_batch_writes_each_table_once_regardless_of_ticket_count(tree, monkeypatch) -> None:
    """The whole point: N tickets in a month cost one write per table, not N."""
    raw, out = tree
    keys = [f"PROJ-2{i:02d}" for i in range(12)]
    for k in keys:
        _write_raw(raw, _raw_issue(k))

    saves: list[str] = []
    real = jira_incremental.save_parquet_month

    def _count(df, schema, output_dir, month_key):
        saves.append(Path(output_dir).name)
        return real(df, schema, output_dir, month_key)

    monkeypatch.setattr(jira_incremental, "save_parquet_month", _count)
    jira_incremental.transform_issues(keys, raw_dir=raw, output_dir=out)

    assert len(saves) == len(set(saves)), f"a table was written more than once: {saves}"
    assert len(saves) <= 6, f"expected at most one write per table, got {saves}"
    # and the data really is all there
    assert sorted(_rows(out, "issues")["issue_key"].tolist()) == sorted(keys)


def test_every_issue_in_the_batch_survives_in_every_table(tree) -> None:
    raw, out = tree
    keys = ["PROJ-301", "PROJ-302", "PROJ-303", "PROJ-304"]
    for k in keys:
        _write_raw(raw, _raw_issue(k, comments=3))
    jira_incremental.transform_issues(keys, raw_dir=raw, output_dir=out)

    assert sorted(_rows(out, "issues")["issue_key"].tolist()) == sorted(keys)
    comments = _rows(out, "comments")
    assert sorted(comments["issue_key"].unique().tolist()) == sorted(keys)
    assert len(comments) == 4 * 3


def test_batch_upserts_rather_than_appends_on_a_second_pass(tree) -> None:
    """Re-running the same batch must not duplicate rows — the poller does this
    every cycle."""
    raw, out = tree
    keys = ["PROJ-401", "PROJ-402"]
    for k in keys:
        _write_raw(raw, _raw_issue(k, comments=2))

    jira_incremental.transform_issues(keys, raw_dir=raw, output_dir=out)
    first = len(_rows(out, "comments"))
    jira_incremental.transform_issues(keys, raw_dir=raw, output_dir=out)

    assert len(_rows(out, "comments")) == first
    assert len(_rows(out, "issues")) == 2


def test_a_batch_leaves_other_issues_in_the_month_untouched(tree) -> None:
    """Only the batch's keys may be replaced; a month holds tickets the poll did
    not visit (every resolved issue), and they must survive."""
    raw, out = tree
    for k in ["PROJ-501", "PROJ-502", "PROJ-503"]:
        _write_raw(raw, _raw_issue(k, comments=1))
        jira_incremental.transform_single_issue(k, raw_dir=raw, output_dir=out)

    jira_incremental.transform_issues(["PROJ-501"], raw_dir=raw, output_dir=out)

    assert sorted(_rows(out, "issues")["issue_key"].tolist()) == ["PROJ-501", "PROJ-502", "PROJ-503"]
    assert sorted(_rows(out, "comments")["issue_key"].unique().tolist()) == [
        "PROJ-501",
        "PROJ-502",
        "PROJ-503",
    ]


# --------------------------------------------------------------------------------
# The per-issue tri-states must not be flattened by batching.
# --------------------------------------------------------------------------------


def test_an_incomplete_comment_marker_preserves_only_that_issues_thread(tree) -> None:
    """`_comments_incomplete` is decided PER ISSUE. In a batch where one issue
    carries the marker and another does not, the marked issue keeps its stored
    thread while the other still updates."""
    raw, out = tree
    good, marked = "PROJ-601", "PROJ-602"
    for k in (good, marked):
        _write_raw(raw, _raw_issue(k, comments=3))
    jira_incremental.transform_issues([good, marked], raw_dir=raw, output_dir=out)
    assert len(_rows(out, "comments")) == 6

    # Now the marked issue comes back truncated + flagged, the other grows.
    truncated = _raw_issue(marked, comments=1)
    truncated["_comments_incomplete"] = True
    _write_raw(raw, truncated)
    _write_raw(raw, _raw_issue(good, comments=5))

    jira_incremental.transform_issues([good, marked], raw_dir=raw, output_dir=out)

    comments = _rows(out, "comments")
    assert len(comments[comments["issue_key"] == marked]) == 3, "stored thread was overwritten"
    assert len(comments[comments["issue_key"] == good]) == 5, "unmarked issue did not update"


def test_an_absent_remote_links_overlay_preserves_only_that_issues_rows(tree) -> None:
    raw, out = tree
    keys = ["PROJ-701", "PROJ-702"]
    for k in keys:
        issue = _raw_issue(k)
        issue["_remote_links"] = [{"id": "1", "object": {"url": "https://example.com/a", "title": "a"}}]
        _write_raw(raw, issue)
    jira_incremental.transform_issues(keys, raw_dir=raw, output_dir=out)
    assert len(_rows(out, "remote_links")) == 2

    # One issue comes back with the overlay missing (fetch failure) — its rows stand.
    _write_raw(raw, _raw_issue("PROJ-701"))
    jira_incremental.transform_issues(keys, raw_dir=raw, output_dir=out)

    links = _rows(out, "remote_links")
    assert "PROJ-701" in links["issue_key"].tolist(), "preserved rows were wiped"


# --------------------------------------------------------------------------------
# Lock discipline.
# --------------------------------------------------------------------------------


def test_the_batch_module_never_reaches_for_an_issue_lock() -> None:
    """`file_lock.py` documents `issue_json_lock` (outer) -> `parquet_month_lock`
    (inner). The batch holds the month lock across many issues, so taking an issue
    lock inside it would invert the order and deadlock against a webhook holding
    that issue lock while waiting on the month.

    Asserted against the SOURCE rather than by patching: an earlier version of this
    test monkeypatched `file_lock.issue_json_lock` and could never fail, because
    `incremental_transform` does not import or reference it under any name — which
    is precisely the property worth pinning.
    """
    source = Path(jira_incremental.__file__).read_text()
    assert "issue_json_lock" not in source, (
        "incremental_transform must not take an issue lock — it runs inside the "
        "month lock, and that ordering is the deadlock"
    )


def test_batch_reads_each_issue_json_inside_the_month_lock(tree, monkeypatch) -> None:
    """A JSON read outside the lock lets a webhook land between the read and the
    write, and the batch would publish stale rows over it. Reading inside means the
    webhook either already landed (we see it) or is still blocked on the lock and
    applies afterwards."""
    raw, out = tree
    keys = ["PROJ-901", "PROJ-902"]
    for k in keys:
        _write_raw(raw, _raw_issue(k))

    events: list[str] = []
    real_month = jira_incremental.parquet_month_lock
    real_open = open

    import builtins
    import contextlib

    @contextlib.contextmanager
    def _month(output_dir, month_key):
        events.append("lock:enter")
        with real_month(output_dir, month_key):
            yield
        events.append("lock:exit")

    def _open(file, *a, **kw):
        if str(file).endswith(".json") and "issues" in str(file):
            events.append(f"read:{Path(file).stem}")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(jira_incremental, "parquet_month_lock", _month)
    monkeypatch.setattr(builtins, "open", _open)
    jira_incremental.transform_issues(keys, raw_dir=raw, output_dir=out)
    monkeypatch.undo()

    enter, exit_ = events.index("lock:enter"), events.index("lock:exit")
    for key in ("PROJ-901", "PROJ-902"):
        reads = [i for i, e in enumerate(events) if e == f"read:{key}"]
        assert reads, f"{key} was never read: {events}"
        # An earlier, cheap read outside the lock is fine and expected — it only
        # groups by month and its payload is thrown away. What must hold is that
        # the read whose payload is actually WRITTEN happened inside the lock.
        assert enter < reads[-1] < exit_, f"authoritative read outside the lock: {events}"


# --------------------------------------------------------------------------------
# Odd inputs.
# --------------------------------------------------------------------------------


def test_issues_are_filed_in_their_own_month_not_the_batch_head(tree) -> None:
    """The month is derived per issue from its own `created_at`, so a mixed batch
    fans out to the right partitions instead of being filed under one."""
    raw, out = tree
    _write_raw(raw, _raw_issue("PROJ-1001", created="2024-02-03T10:00:00.000+0000"))
    _write_raw(raw, _raw_issue("PROJ-1002"))

    jira_incremental.transform_issues(["PROJ-1001", "PROJ-1002"], raw_dir=raw, output_dir=out)

    assert _rows(out, "issues")["issue_key"].tolist() == ["PROJ-1002"]
    assert _rows(out, "issues", "2024-02")["issue_key"].tolist() == ["PROJ-1001"]


def test_a_missing_json_does_not_sink_the_rest_of_the_batch(tree) -> None:
    raw, out = tree
    _write_raw(raw, _raw_issue("PROJ-1101"))
    jira_incremental.transform_issues(["PROJ-1101", "PROJ-9999"], raw_dir=raw, output_dir=out)
    assert _rows(out, "issues")["issue_key"].tolist() == ["PROJ-1101"]


def test_an_empty_batch_writes_nothing(tree) -> None:
    raw, out = tree
    jira_incremental.transform_issues([], raw_dir=raw, output_dir=out)
    assert not (out / "issues").exists()


def test_a_repeated_key_does_not_duplicate_rows(tree) -> None:
    """`transform_single_issue` was idempotent under repetition and this must be
    too. `_apply_payloads` extends its record list once per payload, so an
    un-deduplicated key is deleted once and re-inserted twice. Repeats are real:
    the same issue_key sits in two partitions when `created_at` is missing, and a
    half-finished flat->hive migration lets the glob see one month twice."""
    raw, out = tree
    _write_raw(raw, _raw_issue("PROJ-1201", comments=2))

    jira_incremental.transform_issues(["PROJ-1201", "PROJ-1201"], raw_dir=raw, output_dir=out)

    assert _rows(out, "issues")["issue_key"].tolist() == ["PROJ-1201"]
    assert len(_rows(out, "comments")) == 2


def test_one_unreadable_month_does_not_sink_the_others(tree) -> None:
    """A corrupt partition raises `UnreadablePartitionError` (from the atomic-write
    change). Without per-month isolation that propagates out of the whole run:
    every later month goes unwritten AND the caller's coalesced jira-refresh
    enqueue is skipped, so the months that DID write are never announced."""
    raw, out = tree
    _write_raw(raw, _raw_issue("PROJ-1301", created="2024-02-03T10:00:00.000+0000"))
    _write_raw(raw, _raw_issue("PROJ-1302"))
    jira_incremental.transform_issues(["PROJ-1301", "PROJ-1302"], raw_dir=raw, output_dir=out)

    # Corrupt the EARLIER month, so a sorted() walk hits it first.
    corrupt = out / "issues" / "month=2024-02" / "data.parquet"
    corrupt.write_bytes(b"PAR1" + b"\x00" * 64)

    _write_raw(raw, _raw_issue("PROJ-1302", comments=4))
    applied = jira_incremental.transform_issues(
        ["PROJ-1301", "PROJ-1302"], raw_dir=raw, output_dir=out
    )

    # The healthy month still landed, and the run returned rather than raising.
    assert "PROJ-1302" in applied
    assert len(_rows(out, "comments")) == 4
    # The corrupt partition was left for an operator, not overwritten.
    assert corrupt.read_bytes().startswith(b"PAR1\x00")


# --------------------------------------------------------------------------------
# The poller actually uses it: one transform pass per month, not per ticket.
# --------------------------------------------------------------------------------


def test_the_poller_writes_parquet_end_to_end_with_nothing_stubbed_but_the_api(
    tmp_path: Path, monkeypatch
) -> None:
    """The regression guard this file was missing.

    An earlier version stubbed the month grouping in every poller test, so it never
    exercised the one path that mattered — and shipped a lookup that returned `{}`
    on every real tree, leaving the poller writing NO parquet at all while every
    test stayed green. Here only the Jira HTTP call is faked: the poll reads a real
    issues parquet, refreshes real JSON, and must leave real rows on disk.
    """
    from connectors.jira.scripts import poll_sla

    raw = tmp_path / "raw"
    (raw / "issues").mkdir(parents=True)
    served = tmp_path / "extracts" / "jira" / "data"
    served.mkdir(parents=True)

    # Two open tickets in different months, present as raw JSON and in the parquet
    # the poller reads to decide what is open.
    tickets = {"PROJ-11": "2025-06-15T10:00:00.000+0000", "PROJ-12": "2024-02-15T10:00:00.000+0000"}
    for key, created in tickets.items():
        _write_raw(raw, _raw_issue(key, created=created))
    for key, created in tickets.items():
        jira_incremental.save_parquet_month(
            pd.DataFrame([{"issue_key": key, "status_category": "To Do"}]),
            {"issue_key": "string", "status_category": "string"},
            served / "issues",
            created[:7],
        )

    monkeypatch.setenv("JIRA_PARQUET_DIR", str(served))
    monkeypatch.setattr(jira_incremental, "DEFAULT_OUTPUT_DIR", served)
    monkeypatch.setattr(poll_sla, "configured_field_ids", lambda: ["customfield_1"])
    monkeypatch.setattr(
        poll_sla,
        "load_config",
        lambda: {
            "data_dir": raw,
            "base_url": "https://example.invalid",
            "email": "poller@example.com",
            "api_token": "token",
        },
    )
    monkeypatch.setattr(
        poll_sla, "fetch_sla_and_status", lambda *_a, **_kw: {"customfield_1": {"elapsed": 1}}
    )
    monkeypatch.setattr(poll_sla.time, "sleep", lambda _s: None)

    saves: list[tuple[str, str]] = []
    real_save = jira_incremental.save_parquet_month

    def _count(df, schema, output_dir, month_key):
        saves.append((Path(output_dir).name, month_key))
        return real_save(df, schema, output_dir, month_key)

    monkeypatch.setattr(jira_incremental, "save_parquet_month", _count)

    stats = poll_sla.run()

    assert stats["updated"] == 2, stats
    assert saves, "the poll wrote NO parquet — the batch path never fired"
    # One write per (table, month), never one per ticket.
    assert len(saves) == len(set(saves)), f"a table+month was written twice: {saves}"
    assert {m for _t, m in saves} == {"2025-06", "2024-02"}
    # And the rows really landed, each in its own month.
    assert _rows(served, "issues", "2025-06")["issue_key"].tolist() == ["PROJ-11"]
    assert _rows(served, "issues", "2024-02")["issue_key"].tolist() == ["PROJ-12"]


def test_the_poller_defers_every_write_until_all_json_is_refreshed(
    tmp_path: Path, monkeypatch
) -> None:
    """Phase 1 must release each issue lock before phase 2 takes any month lock,
    or the batch inverts `file_lock.py`'s issue(outer) -> month(inner) nesting and
    can deadlock against a concurrent webhook."""
    from connectors.jira.scripts import poll_sla

    events: list[str] = []

    def _fake_update(issue_key, raw_dir, base_url, auth):
        events.append(f"json:{issue_key}")
        return "updated"

    handed: list[list[str]] = []

    def _fake_transform(issue_keys, **kw):
        events.append("write")
        handed.append(list(issue_keys))
        return list(issue_keys)

    # PROJ-2 is skipped by the API gate; only refreshed tickets may be handed on.
    def _update(issue_key, raw_dir, base_url, auth):
        if issue_key == "PROJ-2":
            return "skipped"
        return _fake_update(issue_key, raw_dir, base_url, auth)

    monkeypatch.setattr(poll_sla, "find_open_issues", lambda _d: ["PROJ-1", "PROJ-2", "PROJ-3"])
    monkeypatch.setattr(poll_sla, "update_issue_sla", _update)
    monkeypatch.setattr(poll_sla, "transform_issues", _fake_transform)
    monkeypatch.setattr(poll_sla.time, "sleep", lambda _s: None)
    monkeypatch.setattr(poll_sla, "configured_field_ids", lambda: ["customfield_1"])
    monkeypatch.setattr(
        poll_sla,
        "load_config",
        lambda: {
            "data_dir": tmp_path,
            "base_url": "https://example.invalid",
            "email": "poller@example.com",
            "api_token": "token",
        },
    )

    poll_sla.run()

    assert events == ["json:PROJ-1", "json:PROJ-3", "write"], events
    assert handed == [["PROJ-1", "PROJ-3"]], "skipped tickets must not reach the write"
