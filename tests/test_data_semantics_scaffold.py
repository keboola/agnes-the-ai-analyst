"""Unit tests for the workspace data-semantics pack scaffolder (Gap 1 / #469).

The engine is a pure transformation over plain Python data (the CLI assembles
that data from the repositories), so these tests need no database — they feed
dicts shaped like ``metric_definitions`` / ``table_registry`` rows and assert
on the emitted YAML/markdown and the ``sync``-block merge contract.
"""

from __future__ import annotations

import yaml

from src.data_semantics_scaffold import (
    ScaffoldError,
    comparable_view,
    humanize,
    scaffold_pack,
)

FIXED_TS = "2026-06-01T00:00:00Z"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _table(tid="e1_events", name="E1 — Events", **kw):
    base = {"id": tid, "name": name, "columns": [], "bq_cache": None}
    base.update(kw)
    return base


def _metric(name="clicks", **kw):
    base = {"name": name}
    base.update(kw)
    return base


def _pkg(slug="engagement", name="Engagement", description="UI events.",
         tables=None, metrics=None):
    return {
        "slug": slug, "name": name, "description": description,
        "tables": list(tables or []), "metrics": list(metrics or []),
    }


def _inputs(*packages):
    return {"packages": list(packages)}


def _reparse(files):
    """Build a parsed ``existing`` map from rendered files, as the CLI does."""
    out = {}
    for rel, text in files.items():
        out[rel] = yaml.safe_load(text) if rel.endswith((".yml", ".yaml")) else text
    return out


def _full_metric():
    return _metric(
        "clicks", display_name="Clicks", category="engagement",
        description="Total clicks.", type="count", unit="count", grain="event",
        tables=["e1_events"], synonyms=["clicks", "click count"],
        filters=["e1.event_date BETWEEN <s> AND <e>"], notes=["Use COUNT(*)."],
        dimensions=["event_date", "widget_id"],
        sql="SELECT COUNT(*)\nFROM e1\nWHERE event_type='click'",
        validation='{"method": "reconcile", "status": "ok"}',
    )


def _full_table():
    return _table(
        "e1_events", "E1 — Events", bq_fqn="proj.analytics.E1_events",
        grain="1 row per UI event", partition_col="event_date",
        gotchas=["Always filter event_date."],
        columns=[
            {"column_name": "event_id", "basetype": "STRING", "description": "Unique key"},
            {"column_name": "event_date", "basetype": "DATE", "description": "Partition key"},
        ],
        bq_cache={"partition_by": "event_date", "clustered_by": ["country_code", "platform"]},
    )


def _generate(*, tables=None, metrics=None, existing=None, ts=FIXED_TS):
    pkg = _pkg(tables=tables, metrics=metrics)
    return scaffold_pack(_inputs(pkg), existing=existing or {}, generated_at=ts)


# --------------------------------------------------------------------------- #
# humanize
# --------------------------------------------------------------------------- #


def test_humanize():
    assert humanize("new_customers") == "New Customers"
    assert humanize("pipeline-value") == "Pipeline Value"
    assert humanize("MRR") == "MRR"  # internal caps preserved


# --------------------------------------------------------------------------- #
# Fresh derivation
# --------------------------------------------------------------------------- #


def test_fresh_metric_derivation(tmp_path=None):
    files, report = _generate(metrics=[_full_metric()])
    rel = "engagement/metrics/clicks.yml"
    assert rel in files
    doc = yaml.safe_load(files[rel])
    assert isinstance(doc, list) and len(doc) == 1  # list form
    m = doc[0]
    assert m["name"] == "clicks"
    assert m["data_product"] == "Engagement"
    assert m["display_name"] == "Clicks"
    assert m["type"] == "count" and m["unit"] == "count" and m["grain"] == "event"
    assert m["coordinate"] == ["e1_events"]
    assert m["coordinate_fqn"] == ["engagement.e1_events"]
    assert m["required_filters"] == ["e1.event_date BETWEEN <s> AND <e>"]
    assert m["validation"] == {"method": "reconcile", "status": "ok"}  # JSON parsed
    assert m["sync"]["method"] == "generated"
    assert m["sync"]["source"] == "metric_definitions"


def test_fresh_table_derivation():
    files, _ = _generate(tables=[_full_table()])
    rel = "engagement/tables/e1_events.yml"
    doc = yaml.safe_load(files[rel])
    assert doc["id"] == "e1_events"
    assert doc["fqn"] == "proj.analytics.E1_events"
    assert doc["partition_by"] == "event_date"        # from bq_cache
    assert doc["clustered_by"] == ["country_code", "platform"]
    assert [c["name"] for c in doc["columns"]] == ["event_id", "event_date"]
    assert doc["columns"][0] == {"name": "event_id", "type": "STRING", "note": "Unique key"}
    assert doc["gotchas"] == ["Always filter event_date."]
    assert doc["sync"]["method"] == "generated"


def test_sync_block_records_owned_fields():
    files, _ = _generate(metrics=[_full_metric()])
    m = yaml.safe_load(files["engagement/metrics/clicks.yml"])[0]
    gf = m["sync"]["generated_fields"]
    for owned in ("name", "type", "grain", "description", "sql", "synonyms"):
        assert owned in gf
    assert m["sync"]["generator"] == "agnes-data-semantics-scaffold"


def test_fqn_constructed_from_bucket_when_no_bq_fqn():
    t = _table("orders", "Orders", bucket="sales", source_table="orders_raw")
    files, _ = _generate(tables=[t])
    doc = yaml.safe_load(files["engagement/tables/orders.yml"])
    assert doc["fqn"] == "sales.orders_raw"


def test_metric_without_tables_emits_without_coordinate():
    files, _ = _generate(metrics=[_metric("ratio", type="ratio", grain="day")])
    m = yaml.safe_load(files["engagement/metrics/ratio.yml"])[0]
    assert m["name"] == "ratio"
    assert "coordinate" not in m


def test_metric_coordinate_unions_tables_and_table_name():
    # A metric referencing tables[] AND a distinct table_name must list both —
    # consistent with how the CLI assigns it to a package (Devin #472 finding).
    m = _metric("ctr", tables=["e1_events"], table_name="s1_sessions",
                type="ratio", grain="event")
    doc = yaml.safe_load(_generate(metrics=[m])[0]["engagement/metrics/ctr.yml"])[0]
    assert doc["coordinate"] == ["e1_events", "s1_sessions"]
    assert doc["coordinate_fqn"] == ["engagement.e1_events", "engagement.s1_sessions"]
    # de-dup: a table_name already present in tables is not repeated.
    m2 = _metric("dup", tables=["e1_events"], table_name="e1_events",
                 type="count", grain="event")
    doc2 = yaml.safe_load(_generate(metrics=[m2])[0]["engagement/metrics/dup.yml"])[0]
    assert doc2["coordinate"] == ["e1_events"]


# --------------------------------------------------------------------------- #
# _brief.md / _overview.md
# --------------------------------------------------------------------------- #


def test_brief_and_overview_seeded_when_absent():
    files, report = _generate(tables=[_full_table()], metrics=[_full_metric()])
    assert "engagement/_brief.md" in files
    assert "engagement/_overview.md" in files
    brief = files["engagement/_brief.md"]
    assert "Engagement" in brief
    assert "`e1_events`" in brief    # derived tables table
    assert "`clicks`" in brief       # derived metrics table
    assert len(report.briefs_seeded) == 2


def test_brief_not_overwritten_when_present():
    files1, _ = _generate(tables=[_full_table()])
    existing = _reparse(files1)
    existing["engagement/_brief.md"] = "# Hand-written brief\nDo not touch.\n"
    files2, report = scaffold_pack(
        _inputs(_pkg(tables=[_full_table()])), existing=existing, generated_at=FIXED_TS
    )
    # seed-if-absent: an existing brief is never re-emitted.
    assert "engagement/_brief.md" not in files2
    assert "engagement/_overview.md" not in files2  # also already present


# --------------------------------------------------------------------------- #
# Merge contract
# --------------------------------------------------------------------------- #


def test_hand_authored_item_is_frozen():
    files1, _ = _generate(metrics=[_full_metric()])
    existing = _reparse(files1)
    item = existing["engagement/metrics/clicks.yml"][0]
    item["sync"]["method"] = "hand-authored"
    item["display_name"] = "Human Clicks"
    item["type"] = "frozen"
    # Source changes, but the human-owned item must not move.
    changed = _full_metric()
    changed["display_name"] = "New Auto Name"
    files2, report = scaffold_pack(
        _inputs(_pkg(metrics=[changed])), existing=existing, generated_at=FIXED_TS
    )
    out = yaml.safe_load(files2["engagement/metrics/clicks.yml"])[0]
    assert out["display_name"] == "Human Clicks"
    assert out["type"] == "frozen"
    assert ("engagement/metrics/clicks.yml", "*", "kept-human") in report.actions


def test_human_edit_of_draft_field_wins():
    files1, _ = _generate(metrics=[_full_metric()])
    existing = _reparse(files1)
    existing["engagement/metrics/clicks.yml"][0]["description"] = "HUMAN polished."
    files2, report = scaffold_pack(
        _inputs(_pkg(metrics=[_full_metric()])), existing=existing, generated_at=FIXED_TS
    )
    out = yaml.safe_load(files2["engagement/metrics/clicks.yml"])[0]
    assert out["description"] == "HUMAN polished."
    assert ("engagement/metrics/clicks.yml", "description", "kept-edited") in report.actions


def test_untouched_field_regenerates_on_source_change():
    files1, _ = _generate(metrics=[_full_metric()])
    existing = _reparse(files1)
    changed = _full_metric()
    changed["description"] = "A brand new description."
    files2, report = scaffold_pack(
        _inputs(_pkg(metrics=[changed])), existing=existing, generated_at=FIXED_TS
    )
    out = yaml.safe_load(files2["engagement/metrics/clicks.yml"])[0]
    assert out["description"] == "A brand new description."
    assert ("engagement/metrics/clicks.yml", "description", "regenerated") in report.actions


def test_generated_field_removed_when_source_disappears():
    files1, _ = _generate(metrics=[_full_metric()])
    existing = _reparse(files1)
    stripped = _full_metric()
    stripped.pop("notes")  # source no longer provides notes
    files2, report = scaffold_pack(
        _inputs(_pkg(metrics=[stripped])), existing=existing, generated_at=FIXED_TS
    )
    out = yaml.safe_load(files2["engagement/metrics/clicks.yml"])[0]
    assert "notes" not in out
    assert ("engagement/metrics/clicks.yml", "notes", "removed") in report.actions


def test_human_added_field_on_generated_item_is_preserved():
    files1, _ = _generate(tables=[_full_table()])
    existing = _reparse(files1)
    # A field the generator never derives (KEEP).
    existing["engagement/tables/e1_events.yml"]["approx_rows_per_day"] = "~5M"
    files2, _ = scaffold_pack(
        _inputs(_pkg(tables=[_full_table()])), existing=existing, generated_at=FIXED_TS
    )
    out = yaml.safe_load(files2["engagement/tables/e1_events.yml"])
    assert out["approx_rows_per_day"] == "~5M"


def test_idempotent_second_run():
    files1, _ = _generate(tables=[_full_table()], metrics=[_full_metric()])
    existing = _reparse(files1)
    _, report = scaffold_pack(
        _inputs(_pkg(tables=[_full_table()], metrics=[_full_metric()])),
        existing=existing, generated_at="2099-01-01T00:00:00Z",
    )
    counts = report.status_counts()
    assert counts.get("generated", 0) == 0
    assert counts.get("regenerated", 0) == 0
    assert not report.wrote_changes()


# --------------------------------------------------------------------------- #
# --check
# --------------------------------------------------------------------------- #


def test_comparable_view_ignores_timestamp_but_sees_real_changes():
    a, _ = _generate(metrics=[_full_metric()], ts="2026-01-01T00:00:00Z")
    b, _ = _generate(metrics=[_full_metric()], ts="2099-09-09T00:00:00Z")
    assert comparable_view(a) == comparable_view(b)  # only last_synced differs
    changed = _full_metric()
    changed["description"] = "Different."
    c, _ = _generate(metrics=[changed], ts="2026-01-01T00:00:00Z")
    assert comparable_view(a) != comparable_view(c)


# --------------------------------------------------------------------------- #
# Odd inputs
# --------------------------------------------------------------------------- #


def test_package_without_slug_is_skipped_with_warning():
    bad = _pkg(slug="", tables=[_full_table()])
    files, report = scaffold_pack(_inputs(bad), existing={}, generated_at=FIXED_TS)
    assert files == {}
    assert any("slug" in w for w in report.warnings)


def test_bad_inputs_raise():
    try:
        scaffold_pack({"packages": "nope"}, existing={}, generated_at=FIXED_TS)
    except ScaffoldError:
        pass
    else:
        raise AssertionError("expected ScaffoldError for non-list packages")


def test_rendered_yaml_round_trips():
    files, _ = _generate(tables=[_full_table()], metrics=[_full_metric()])
    for rel, text in files.items():
        if rel.endswith(".yml"):
            assert yaml.safe_load(text) is not None
