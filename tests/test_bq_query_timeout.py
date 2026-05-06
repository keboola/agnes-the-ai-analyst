"""Unit tests for apply_bq_session_settings.

Covers the data_source.bigquery.query_timeout_ms knob added so that
agnes query --remote no longer trips the DuckDB BigQuery extension's
built-in 90 s wait timeout when the underlying BQ job takes longer.
"""

from unittest.mock import patch

from connectors.bigquery.access import apply_bq_session_settings


class _RecordingConn:
    """Minimal DuckDB-conn stand-in that records execute() calls.

    apply_bq_session_settings only calls .execute(); we don't need a
    real DuckDB to verify the SET command shape.
    """

    def __init__(self, raise_on=None):
        self.calls: list[str] = []
        self.raise_on = raise_on

    def execute(self, sql: str):
        self.calls.append(sql)
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError(f"simulated failure on: {sql}")


def _patched_get_value(value):
    """Helper: build a patch target that returns *value* for the
    data_source.bigquery.query_timeout_ms key and propagates the
    `default=` kwarg for any other lookup so we don't accidentally
    break tests that read other keys via the same module."""
    def fake(*keys, default=None):
        if keys == ("data_source", "bigquery", "query_timeout_ms"):
            return value
        return default
    return patch("app.instance_config.get_value", side_effect=fake)


def test_default_when_config_missing():
    """When get_value returns the default (None passed through, default arg
    used), apply_bq_session_settings should fall back to the bumped
    600 000 ms default and emit the SET."""
    conn = _RecordingConn()
    # Simulate get_value returning the default we passed (600_000) by
    # echoing the default kwarg.
    def fake(*keys, default=None):
        return default
    with patch("app.instance_config.get_value", side_effect=fake):
        apply_bq_session_settings(conn)
    assert conn.calls == ["SET bq_query_timeout_ms = 600000"]


def test_explicit_value():
    conn = _RecordingConn()
    with _patched_get_value(900_000):
        apply_bq_session_settings(conn)
    assert conn.calls == ["SET bq_query_timeout_ms = 900000"]


def test_zero_sentinel_leaves_extension_default():
    """0 means 'use the DuckDB BQ extension's built-in default' — no SET
    must be emitted so a non-zero default doesn't override an operator's
    explicit opt-out."""
    conn = _RecordingConn()
    with _patched_get_value(0):
        apply_bq_session_settings(conn)
    assert conn.calls == []


def test_negative_value_treated_as_zero():
    """Negative is nonsensical for a timeout; treat as 'extension default'
    rather than emitting a negative SET that the extension might reject
    or interpret unexpectedly."""
    conn = _RecordingConn()
    with _patched_get_value(-1):
        apply_bq_session_settings(conn)
    assert conn.calls == []


def test_non_numeric_silently_skipped():
    """A string-typed YAML value (e.g. operator typo) shouldn't crash
    the BQ session — fall through to the extension default."""
    conn = _RecordingConn()
    with _patched_get_value("notanumber"):
        apply_bq_session_settings(conn)
    assert conn.calls == []


def test_string_numeric_is_coerced():
    """YAML loaders sometimes deliver int-like values as strings; accept
    those rather than failing."""
    conn = _RecordingConn()
    with _patched_get_value("750000"):
        apply_bq_session_settings(conn)
    assert conn.calls == ["SET bq_query_timeout_ms = 750000"]


def test_set_failure_does_not_propagate():
    """Older DuckDB BQ extension versions may not recognise the setting.
    The function must fail-soft so a session that was otherwise healthy
    keeps working — just with the extension's built-in default timeout."""
    conn = _RecordingConn(raise_on="SET bq_query_timeout_ms")
    with _patched_get_value(600_000):
        # Must not raise.
        apply_bq_session_settings(conn)
    # The SET was attempted (recorded before the exception).
    assert conn.calls == ["SET bq_query_timeout_ms = 600000"]


def test_no_app_config_module_silently_skipped():
    """Unit-test contexts that don't bring up the app config layer must
    still be able to construct BQ sessions for narrow tests; an
    ImportError on app.instance_config means we can't read the knob,
    so we leave the extension default in place."""
    conn = _RecordingConn()
    with patch.dict(
        "sys.modules", {"app.instance_config": None},
    ):
        apply_bq_session_settings(conn)
    assert conn.calls == []
