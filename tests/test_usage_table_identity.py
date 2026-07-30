"""Telemetry table identity: recorded SQL path → registry id.

`app/api/query.py` records the FULL path a query named; the aggregation folds
paths that a registry row owns onto that row's id and leaves the rest verbatim.
These are the pure-Python helpers both usage repos share, so the DuckDB and PG
dashboards cannot disagree about what a table is called or whether it is
registered.

The bug class being fenced off (Devin Review on #1121, thread
"Fully-qualified paths collapse to the bare table name"): the pre-0.77.30 write
path kept only the last segment, so `proj_a.ds1.orders` and `proj_b.ds2.orders`
aggregated into one `orders` row that read as registered if *any* registry id
happened to be `orders`, while a registry-gated `bq."dataset"."table"` reduced
to a `table` that matched nothing and read as unregistered.
"""

from src.repositories.usage import (
    _merge_frequency,
    _merge_top_tables,
    _registry_identity_keys,
    _resolve_table_identity,
)


def _group(path, queries=1, failed=0, scan_bytes=0, remote=0, local=0):
    return (path, queries, failed, scan_bytes, remote, local)


class TestRegistryIdentityKeys:
    def test_bare_id_and_display_name_both_resolve(self):
        keys = _registry_identity_keys(
            [
                {
                    "id": "bq.finance.ue",
                    "name": "ue",
                    "source_type": "bigquery",
                    "bucket": None,
                    "source_table": None,
                    "bq_fqn": None,
                },
            ]
        )
        assert keys["bq.finance.ue"] == "bq.finance.ue"
        # id != name is a real registration shape, and the SQL carries `name`.
        assert keys["ue"] == "bq.finance.ue"

    def test_bq_catalog_path_and_fqn_resolve(self):
        keys = _registry_identity_keys(
            [
                {
                    "id": "ledger",
                    "name": "ledger",
                    "source_type": "bigquery",
                    "bucket": "finance",
                    "source_table": "gl_entries",
                    "bq_fqn": "my-project-123.finance.gl_entries",
                },
            ]
        )
        assert keys["bq.finance.gl_entries"] == "ledger"
        assert keys["finance.gl_entries"] == "ledger"
        assert keys["my-project-123.finance.gl_entries"] == "ledger"

    def test_keboola_rows_use_the_kbc_alias(self):
        keys = _registry_identity_keys(
            [
                {
                    "id": "orders",
                    "name": "orders",
                    "source_type": "keboola",
                    "bucket": "in.c-main",
                    "source_table": "orders",
                    "bq_fqn": None,
                },
            ]
        )
        assert keys["kbc.in.c-main.orders"] == "orders"
        assert "bq.in.c-main.orders" not in keys

    def test_keys_are_lowercased(self):
        keys = _registry_identity_keys(
            [
                {
                    "id": "Orders",
                    "name": "Orders",
                    "source_type": "bigquery",
                    "bucket": "Finance",
                    "source_table": "GL",
                    "bq_fqn": None,
                },
            ]
        )
        assert keys["orders"] == "Orders"
        assert keys["bq.finance.gl"] == "Orders"

    def test_id_wins_over_another_rows_display_name(self):
        """A name collision must never steal a row's own id."""
        keys = _registry_identity_keys(
            [
                {
                    "id": "shadow",
                    "name": "orders",
                    "source_type": "bigquery",
                    "bucket": None,
                    "source_table": None,
                    "bq_fqn": None,
                },
                {
                    "id": "orders",
                    "name": "orders_v2",
                    "source_type": "bigquery",
                    "bucket": None,
                    "source_table": None,
                    "bq_fqn": None,
                },
            ]
        )
        assert keys["orders"] == "orders"

    def test_shared_path_resolves_to_the_older_row(self):
        """Same tie-break as the RBAC gate (find_by_bq_path), so the dashboard
        attributes a shared path to the row the gate charged. Rows arrive
        oldest-first."""
        keys = _registry_identity_keys(
            [
                {
                    "id": "ledger_old",
                    "name": "ledger_old",
                    "source_type": "bigquery",
                    "bucket": "finance",
                    "source_table": "gl",
                    "bq_fqn": None,
                },
                {
                    "id": "ledger_new",
                    "name": "ledger_new",
                    "source_type": "bigquery",
                    "bucket": "finance",
                    "source_table": "gl",
                    "bq_fqn": None,
                },
            ]
        )
        assert keys["bq.finance.gl"] == "ledger_old"

    def test_rows_without_an_id_are_skipped(self):
        assert _registry_identity_keys([{"id": None, "name": "x"}]) == {}

    def test_accepts_row_tuples(self):
        keys = _registry_identity_keys(
            [
                ("ledger", "ledger", "bigquery", "finance", "gl", None),
            ]
        )
        assert keys["bq.finance.gl"] == "ledger"


class TestResolveTableIdentity:
    def test_unresolved_path_keeps_every_segment(self):
        """Falling back to the tail segment is exactly the guess that produced
        the false `registered=true`."""
        ident, registered = _resolve_table_identity("proj_b.ds2.orders", {"orders": "orders"})
        assert ident == "proj_b.ds2.orders"
        assert registered is False

    def test_resolved_path_reports_the_registry_id(self):
        ident, registered = _resolve_table_identity("bq.finance.gl", {"bq.finance.gl": "ledger"})
        assert (ident, registered) == ("ledger", True)

    def test_case_and_padding_insensitive(self):
        assert _resolve_table_identity("  BQ.Finance.GL ", {"bq.finance.gl": "ledger"}) == (
            "ledger",
            True,
        )


class TestMergeTopTables:
    def test_same_name_different_projects_stay_two_rows(self):
        rows = _merge_top_tables(
            [_group("proj_a.ds1.orders", queries=3), _group("proj_b.ds2.orders", queries=2)],
            {"orders": "orders"},
            limit=10,
        )
        assert [r["table_id"] for r in rows] == ["proj_a.ds1.orders", "proj_b.ds2.orders"]
        assert all(r["registered"] is False for r in rows)

    def test_two_spellings_of_one_table_merge_into_one_row(self):
        """A bare-name local query and a gated `bq.*` remote query hit the same
        physical table — one row, summed counters, ranked on the total."""
        keys = {"ledger": "ledger", "bq.finance.gl": "ledger"}
        rows = _merge_top_tables(
            [
                _group("ledger", queries=2, local=2, scan_bytes=10),
                _group("bq.finance.gl", queries=3, remote=3, scan_bytes=90),
                _group("other", queries=4, local=4),
            ],
            keys,
            limit=10,
        )
        by_id = {r["table_id"]: r for r in rows}
        assert by_id["ledger"]["queries"] == 5
        assert by_id["ledger"]["local"] == 2
        assert by_id["ledger"]["remote"] == 3
        assert by_id["ledger"]["scan_bytes"] == 100
        assert by_id["ledger"]["registered"] is True
        # Merged volume (5) outranks the single-spelling table (4).
        assert rows[0]["table_id"] == "ledger"

    def test_ranking_is_by_successful_queries_then_deterministic(self):
        rows = _merge_top_tables(
            [
                _group("ghost", queries=9, failed=9),
                _group("real", queries=2),
                _group("also_real", queries=2),
            ],
            {},
            limit=10,
        )
        assert [r["table_id"] for r in rows] == ["also_real", "real", "ghost"]

    def test_limit_applies_after_the_fold(self):
        rows = _merge_top_tables(
            [_group("ledger", queries=1), _group("bq.finance.gl", queries=1), _group("z", queries=5)],
            {"ledger": "ledger", "bq.finance.gl": "ledger"},
            limit=1,
        )
        assert [r["table_id"] for r in rows] == ["z"]

    def test_limit_zero_returns_everything(self):
        rows = _merge_top_tables([_group("a"), _group("b")], {}, limit=0)
        assert len(rows) == 2


class TestMergeFrequency:
    def test_spellings_merge_per_day_and_days_stay_newest_first(self):
        rows = _merge_frequency(
            [
                ("2026-07-30", "ledger", 0, 1),
                ("2026-07-30", "bq.finance.gl", 2, 0),
                ("2026-07-29", "ledger", 0, 5),
            ],
            {"ledger": "ledger", "bq.finance.gl": "ledger"},
        )
        assert [(r["day"], r["table_id"], r["remote"], r["local"]) for r in rows] == [
            ("2026-07-30", "ledger", 2, 1),
            ("2026-07-29", "ledger", 0, 5),
        ]

    def test_busiest_table_leads_its_day_after_merging(self):
        rows = _merge_frequency(
            [
                ("2026-07-30", "quiet", 0, 4),
                ("2026-07-30", "ledger", 0, 3),
                ("2026-07-30", "bq.finance.gl", 0, 3),
            ],
            {"ledger": "ledger", "bq.finance.gl": "ledger"},
        )
        assert [r["table_id"] for r in rows] == ["ledger", "quiet"]

    def test_date_objects_are_isoformatted(self):
        from datetime import date

        rows = _merge_frequency([(date(2026, 7, 30), "orders", 1, 0)], {})
        assert rows[0]["day"] == "2026-07-30"
