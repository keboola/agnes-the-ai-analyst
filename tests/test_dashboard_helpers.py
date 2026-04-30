"""Unit tests for the dashboard / catalog display helpers in
``app.web.router``: ``_format_bytes``, ``_format_relative_time``, and
``_build_metrics_data``. These wire the dashboard stat row, the
"Last sync" account row, and the Business-Metrics card."""

from datetime import datetime, timezone, timedelta

import pytest

from app.web.router import (
    _format_bytes,
    _format_relative_time,
    _build_metrics_data,
)


# ── _format_bytes ────────────────────────────────────────────────────────


def test_format_bytes_zero_renders_as_mb():
    assert _format_bytes(0) == "0 MB"
    assert _format_bytes(None) == "0 MB"


def test_format_bytes_kb_range():
    assert _format_bytes(1500) == "1.5 KB"


def test_format_bytes_mb_range():
    assert _format_bytes(2_500_000) == "2.5 MB"
    # ≥ 10 of unit drops the decimal (compactness).
    assert _format_bytes(42_000_000) == "42 MB"


def test_format_bytes_gb_range():
    assert _format_bytes(15_000_000_000) == "15 GB"


def test_format_bytes_under_kb():
    assert _format_bytes(512) == "512 B"


# ── _format_relative_time ────────────────────────────────────────────────


def test_relative_time_none_returns_none():
    assert _format_relative_time(None) is None
    assert _format_relative_time("") is None


def test_relative_time_just_now():
    assert _format_relative_time(datetime.now(timezone.utc)) == "just now"


def test_relative_time_minutes():
    ts = datetime.now(timezone.utc) - timedelta(minutes=3)
    assert _format_relative_time(ts) == "3 minutes ago"


def test_relative_time_singular_minute():
    ts = datetime.now(timezone.utc) - timedelta(minutes=1, seconds=5)
    assert _format_relative_time(ts) == "1 minute ago"


def test_relative_time_hours_and_days():
    ts = datetime.now(timezone.utc) - timedelta(hours=2)
    assert _format_relative_time(ts) == "2 hours ago"
    ts = datetime.now(timezone.utc) - timedelta(days=3)
    assert _format_relative_time(ts) == "3 days ago"


def test_relative_time_falls_back_to_absolute_after_a_week():
    ts = datetime.now(timezone.utc) - timedelta(days=14)
    out = _format_relative_time(ts)
    assert out is not None
    # Absolute fallback uses ``YYYY-MM-DD HH:MM UTC`` format.
    assert "UTC" in out


def test_relative_time_naive_datetime_treated_as_utc():
    """Naive datetimes match how DuckDB returns TIMESTAMP rows under our
    default config; the helper must not raise ``can't subtract`` errors."""
    ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    out = _format_relative_time(ts)
    assert out == "5 minutes ago"


def test_relative_time_iso_string_input():
    iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert _format_relative_time(iso) == "10 minutes ago"


def test_relative_time_future_timestamp_renders_just_now():
    """Clock skew between the writer and the web pod can produce a sync
    timestamp slightly in the future. Without clamping, the helper would
    floor-divide the negative delta into "0 minutes ago" or worse — pin
    that future timestamps render as ``just now`` instead."""
    ts = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert _format_relative_time(ts) == "just now"


# ── _build_metrics_data ──────────────────────────────────────────────────


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    from src.db import close_system_db
    close_system_db()
    yield tmp_path
    close_system_db()


def test_build_metrics_data_empty_when_no_definitions(fresh_db):
    from src.db import get_system_db
    conn = get_system_db()
    try:
        assert _build_metrics_data(conn) == []
    finally:
        conn.close()


def test_build_metrics_data_groups_by_category(fresh_db):
    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository
    conn = get_system_db()
    try:
        repo = MetricRepository(conn)
        repo.create(
            id="finance/mrr", name="mrr", display_name="MRR",
            category="finance", description="Monthly recurring revenue",
            type="ratio", unit="USD", grain="monthly", sql="SELECT 1",
        )
        repo.create(
            id="finance/arr", name="arr", display_name="ARR",
            category="finance", description="Annual recurring revenue",
            type="ratio", unit="USD", grain="yearly", sql="SELECT 1",
        )
        repo.create(
            id="sales/win-rate", name="win-rate", display_name="Win Rate",
            category="sales", description="Wins / opportunities",
            type="ratio", unit="%", grain="weekly", sql="SELECT 1",
        )

        out = _build_metrics_data(conn)
    finally:
        conn.close()

    # Two categories alphabetized: finance first, then sales.
    assert [c["label"] for c in out] == ["Finance", "Sales"]
    # Each category carries its CSS class for the .category-tag pill.
    assert out[0]["css"] == "finance"
    assert out[1]["css"] == "sales"
    # Metrics carry the (path, display_name, description, grain) the
    # template iterator expects.
    finance_paths = [m["path"] for m in out[0]["metrics"]]
    assert set(finance_paths) == {"finance/mrr", "finance/arr"}
    finance_mrr = next(m for m in out[0]["metrics"] if m["path"] == "finance/mrr")
    assert finance_mrr["display_name"] == "MRR"
    assert finance_mrr["grain"] == "monthly"


def test_build_metrics_data_unknown_category_renders_without_css(fresh_db):
    """Categories not in the well-known map carry an empty ``css`` so
    the .category-tag pill renders without a color accent (rather than
    breaking with a bad CSS class)."""
    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository
    conn = get_system_db()
    try:
        MetricRepository(conn).create(
            id="ops/uptime", name="uptime", display_name="Uptime",
            category="ops", description="Service uptime",
            type="ratio", unit="%", grain="daily", sql="SELECT 1",
        )
        out = _build_metrics_data(conn)
    finally:
        conn.close()

    assert len(out) == 1
    assert out[0]["label"] == "Ops"
    assert out[0]["css"] == ""
