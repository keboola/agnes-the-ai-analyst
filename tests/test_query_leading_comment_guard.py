"""Leading SQL comments must not trip the /api/query SELECT/WITH guard.

`agnes query --remote` posts to /api/query, whose `_assert_select_only`
rejected any SQL beginning with a `--` line comment or a `/* */` block comment
even though it was a valid SELECT/WITH after the comment. The local DuckDB
path (`agnes query`) tolerates leading comments, so the two execution paths
disagreed — a registered server-only metric whose stored SQL starts with a
`-- header` comment could not be run verbatim via the only tool that can run
it. `run_remote_select_to_arrow` shares `_assert_select_only`, so this guard
covers the snapshot `--from-query` path too.
"""
import pytest
from fastapi import HTTPException

from app.api.query import _assert_select_only


class TestAssertSelectOnlyLeadingComments:
    @pytest.mark.parametrize(
        "sql_lower",
        [
            "-- leading comment\nselect 1 as x",
            "-- first\n-- second\nselect id from orders",
            "/* block comment */ select 1 as x",
            "/* multi\nline */\nwith t as (select 1) select * from t",
        ],
    )
    def test_leading_comment_allowed(self, sql_lower):
        # Caller passes `.strip().lower()`-ed SQL (per the function contract).
        _assert_select_only(sql_lower)

    def test_blocked_keyword_in_leading_comment_still_blocked(self):
        # Blocklist scans the full SQL (comments included) — a comment can
        # never be used to smuggle a blocked keyword past the guard.
        with pytest.raises(HTTPException):
            _assert_select_only("-- drop this table first\nselect 1")

    def test_comment_only_still_rejected(self):
        # A comment with no actual statement is not a SELECT/WITH.
        with pytest.raises(HTTPException):
            _assert_select_only("-- just a comment, no query")

    def test_block_comment_false_close_rejects_trailing_non_select(self):
        # `/*/ … */` — the block-comment end marker must not be found
        # overlapping the `/*` opener, else the guard sees the comment's fake
        # `SELECT` while DuckDB executes the trailing non-SELECT statement after
        # the true close (Devin Review on PR #743).
        with pytest.raises(HTTPException):
            _assert_select_only("/*/ select 1 */ set memory_limit='1gb'")

    def test_block_comment_with_slash_body_before_real_select_allowed(self):
        # `/*/ header */` is a valid block comment; a real SELECT after it must
        # still pass (the overlapping-close bug used to reject this too).
        _assert_select_only("/*/ header */ select 1")
