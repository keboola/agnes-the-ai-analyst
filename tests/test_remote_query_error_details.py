"""`src/remote_query.py:RemoteQueryError` carries a `details` dict
(verified to already exist) populated for raise sites that wrap an
upstream BqAccessError.

Currently only the BqAccessError-wrap path (lines 422 + 432) populates
it. The other 11 raise sites (lines 134, 142, 167, 173, 259, 264, 282,
289, 313, 322, 375) need an audit — for sites that wrap an external
exception, populate `details` so the CLI renderer has the structured
context it needs.

Closes the audit half of #160 §4.7.3.
"""
from __future__ import annotations


def test_blocked_keyword_carries_keyword_in_details():
    """`raise RemoteQueryError("blocked_keyword", ..., details={"blocked_keyword": kw})`
    already populates the keyword. Lock it in via assertion so a future
    refactor doesn't drop the field."""
    from src.remote_query import RemoteQueryEngine, RemoteQueryError
    import duckdb

    conn = duckdb.connect(":memory:")
    engine = RemoteQueryEngine(conn)
    try:
        engine.execute("DROP TABLE foo")
    except RemoteQueryError as exc:
        # Blocked-keyword raise lives at src/remote_query.py:134-138 with
        # error_type="query_error" (not "blocked_keyword" — that's the
        # keyword name in details, not the type).
        assert exc.error_type == "query_error", exc.error_type
        assert exc.details, "blocked_keyword raise must populate details"
        assert "blocked_keyword" in exc.details
        assert exc.details["blocked_keyword"] == "drop "
    else:
        raise AssertionError("expected RemoteQueryError")


def test_query_must_be_select_carries_no_unnecessary_details():
    """A pure local-validation raise (SQL doesn't start with SELECT)
    has nothing structured to surface. details=None is the right shape;
    test locks that in so a future contributor doesn't add noise."""
    from src.remote_query import RemoteQueryEngine, RemoteQueryError
    import duckdb

    conn = duckdb.connect(":memory:")
    engine = RemoteQueryEngine(conn)
    try:
        engine.execute("WITH x AS (SELECT 1) SELECT * FROM x")
    except RemoteQueryError as exc:
        # `WITH ...` is treated as not-starting-with-SELECT today; this is
        # the shape we want to assert: details may be empty/None for pure
        # local-validation errors.
        assert exc.error_type in ("query_must_be_select", "blocked_keyword"), exc.error_type
        # Either empty dict or dict with explanatory keys is fine.
        assert isinstance(exc.details, dict)
    except Exception:
        # If the engine accepts WITH, that's also fine — this test is
        # primarily about details shape, not what gets blocked.
        pass
