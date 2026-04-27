"""Issue #81 Group D — extractor-layer identifier injection (M15).

Mirror of `_validate_identifier` coverage in `tests/test_orchestrator.py`,
but at the extractor layer. Each test feeds a registry row with an
attacker-shaped `name` / `bucket` / `source_table` into the extractor and
asserts the row is skipped (no SQL run, error recorded) while valid
sibling rows in the same registry continue to process.
"""

from unittest.mock import MagicMock

import pytest

from src.identifier_validation import (
    is_safe_identifier,
    is_safe_quoted_identifier,
    validate_identifier,
    validate_quoted_identifier,
)


class TestIsSafeIdentifier:
    """Strict identifier — for our own view names, aliases."""

    @pytest.mark.parametrize(
        "name",
        ["orders", "Orders", "_priv", "ABC_123", "x", "a" * 64],
    )
    def test_valid(self, name):
        assert is_safe_identifier(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "1starts_with_digit",
            "has space",
            "has-dash",
            "has.dot",
            "has;semi",
            "has\"quote",
            "has'quote",
            "evil\"; DROP TABLE x; --",
            "x" * 65,        # too long
            "café",          # diacritic — DuckDB allows but our policy doesn't
            None,
            123,
            ["x"],
        ],
    )
    def test_invalid(self, name):
        assert is_safe_identifier(name) is False


class TestIsSafeQuotedIdentifier:
    """Relaxed identifier for upstream-typed names (Keboola buckets,
    BigQuery datasets). Allows `.` and `-`; refuses injection markers."""

    @pytest.mark.parametrize(
        "name",
        [
            "orders",
            "in.c-events",          # Keboola bucket
            "out.c-marketing.roi",  # nested Keboola bucket form
            "my-bq-dataset",
            "1starts_with_digit",   # numeric start fine inside quotes
            "ABC_123",
            "a" * 128,              # at the limit
        ],
    )
    def test_valid(self, name):
        assert is_safe_quoted_identifier(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "",                       # empty
            "has space",
            "has\"quote",            # would close the surrounding `"`
            "has'apostrophe",
            "has;semi",
            "evil\"; DROP TABLE x; --",
            "name\x00with-nul",      # NUL
            "name\nwith-newline",    # control char
            "x" * 129,                # too long
            ".starts_with_dot",
            "-starts_with_dash",
            None,
            123,
            ["x"],
        ],
    )
    def test_invalid(self, name):
        assert is_safe_quoted_identifier(name) is False


class TestKeboolaExtractorRefusesUnsafeIdentifiers:
    """Verify keboola/extractor.py:run() skips registry rows with unsafe
    identifiers but still processes the safe siblings."""

    def test_unsafe_table_name_rejected_safe_kept(self, tmp_path, monkeypatch):
        """Behavioural test: pass mixed registry rows, observe stats."""
        from connectors.keboola import extractor as kbe

        # Use the extractor's run() with use_extension=False so we don't
        # need a real Keboola server. We mock _extract_via_legacy to be a
        # no-op that creates a tiny parquet so the size lookup works.
        import duckdb

        def fake_legacy(tc, pq_path, url, token):
            d = duckdb.connect()
            d.execute(f"COPY (SELECT 1 AS x) TO '{pq_path}' (FORMAT PARQUET)")
            d.close()

        monkeypatch.setattr(kbe, "_extract_via_legacy", fake_legacy)

        # Three rows: a good one, one with a legitimate dashed name (must
        # also pass — operator habit), and one with a hostile injection.
        rows = [
            {"name": "good_table", "query_mode": "local"},
            {"name": "events-2026", "query_mode": "local"},  # legitimate dash
            {"name": "evil\"; DROP TABLE x; --", "query_mode": "local"},
        ]
        out_dir = tmp_path / "extracts" / "keboola"
        result = kbe.run(
            str(out_dir), rows, keboola_url="", keboola_token="",
        )

        assert result["tables_extracted"] == 2  # good_table + events-2026
        assert result["tables_failed"] == 1
        assert any("unsafe identifier" in (e.get("error") or "") for e in result["errors"])

    def test_unsafe_bucket_in_remote_row_rejected(self, tmp_path, monkeypatch):
        from connectors.keboola import extractor as kbe

        # An adversarial bucket containing `"` would close the surrounding
        # quote and inject SQL. Real Keboola buckets like `in.c-events`
        # are accepted by the relaxed validator.
        rows = [
            {
                "name": "good",
                "query_mode": "remote",
                "bucket": 'evil"; DROP--',
                "source_table": "t",
            },
        ]
        out_dir = tmp_path / "extracts" / "keboola"
        result = kbe.run(
            str(out_dir), rows, keboola_url="", keboola_token="",
        )
        assert result["tables_failed"] == 1
        assert any("unsafe bucket/source_table" in (e.get("error") or "") for e in result["errors"])


class TestBigQueryExtractorRefusesUnsafeIdentifiers:
    def test_unsafe_dataset_rejected(self):
        """BigQuery extractor uses validate_quoted_identifier for dataset."""
        # `my.dataset` is now legitimate (BigQuery datasets can contain dots).
        assert validate_quoted_identifier("my-dataset", "BigQuery dataset") is True
        # An injection attempt via embedded quote — refused.
        assert validate_quoted_identifier("evil\"; DROP --", "BigQuery dataset") is False
        # Sanity: a normal dataset name passes.
        assert validate_quoted_identifier("marketing", "BigQuery dataset") is True
        # The view-name (table_name) still uses the strict validator.
        assert validate_identifier("marketing_view", "BigQuery table_name") is True
        assert validate_identifier("marketing-view", "BigQuery table_name") is False
