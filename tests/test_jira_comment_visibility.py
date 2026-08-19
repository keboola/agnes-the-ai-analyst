"""Tests for the ``comments.public_visibility`` column.

Jira Service Management separates customer-facing replies from internal notes.
The platform API exposes that state as ``jsdPublic`` on every comment — present
on the comments embedded in a plain ``GET /issue/{key}``, so no ``expand`` and
no second request are needed. JSM's own storage, the ``sd.public.comment``
entity property, is deliberately not consulted: it needs ``expand=properties``,
and any payload carrying it carries ``jsdPublic`` too, so a property branch
would be unreachable by construction.

The column is three-valued on purpose. A missing flag is written as NULL and
counted, never defaulted: a boolean that is confidently wrong is worse than one
that admits the gap, because nothing downstream can distinguish a defaulted
``true`` from an observed one. The same strictness applies to typing: only a
JSON boolean is trusted — ``bool("false")`` is ``True``, so a mistyped flag
resolves to NULL rather than risking an inverted value.
"""

import logging

import pandas as pd
import pyarrow as pa

from connectors.jira.transform import (
    COMMENTS_SCHEMA,
    apply_schema,
    get_pyarrow_schema,
    transform_comments,
)


def _issue(*comments: dict) -> dict:
    return {"key": "SUPPORT-1", "fields": {"comment": {"comments": list(comments), "total": len(comments)}}}


def _comment(comment_id: str = "1", **extra) -> dict:
    base = {
        "id": comment_id,
        "author": {"emailAddress": "agent@example.com", "displayName": "Agent"},
        "updateAuthor": {"emailAddress": "agent@example.com", "displayName": "Agent"},
        "body": {"type": "doc", "content": [{"type": "text", "text": "hello"}]},
        "created": "2026-01-01T10:00:00.000+0000",
        "updated": "2026-01-01T10:00:00.000+0000",
    }
    base.update(extra)
    return base


def _visibility(comment: dict):
    records = transform_comments(_issue(comment), preserve_on_incomplete=False)
    assert records is not None and len(records) == 1
    return records[0]["public_visibility"]


class TestPublicVisibilityExtraction:
    """The states a comment can arrive in, and what each must produce."""

    def test_jsd_public_true_is_customer_facing(self):
        assert _visibility(_comment(jsdPublic=True)) is True

    def test_jsd_public_false_is_internal(self):
        assert _visibility(_comment(jsdPublic=False)) is False

    def test_absent_flag_is_null_never_defaulted(self):
        assert _visibility(_comment()) is None

    def test_explicit_null_flag_is_null(self):
        assert _visibility(_comment(jsdPublic=None)) is None

    def test_string_false_is_null_never_inverted(self):
        """``bool("false")`` is ``True`` — the exact miscoercion the sibling
        dataset shipped for ``value.internal``. A mistyped flag must resolve to
        NULL, never to an inverted boolean."""
        assert _visibility(_comment(jsdPublic="false")) is None

    def test_string_true_is_null_not_trusted(self):
        assert _visibility(_comment(jsdPublic="true")) is None

    def test_numeric_flag_is_null_not_trusted(self):
        assert _visibility(_comment(jsdPublic=1)) is None
        assert _visibility(_comment(jsdPublic=0)) is None

    def test_unresolved_comments_are_counted_in_a_warning(self, caplog):
        issue = _issue(_comment("1", jsdPublic=True), _comment("2"), _comment("3"))
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(issue, preserve_on_incomplete=False)
        assert "2 of 3 comments" in caplog.text
        assert "public_visibility resolved as NULL" in caplog.text

    def test_mistyped_flags_are_counted_in_the_warning(self, caplog):
        issue = _issue(_comment("1", jsdPublic=True), _comment("2", jsdPublic="false"))
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(issue, preserve_on_incomplete=False)
        assert "1 of 2 comments" in caplog.text

    def test_no_warning_when_every_comment_resolves(self, caplog):
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(_issue(_comment("1", jsdPublic=True)), preserve_on_incomplete=False)
        assert "public_visibility" not in caplog.text

    def test_warning_suppressed_on_throwaway_passes(self, caplog):
        """``transform_issues``' grouping pass discards its payloads and rebuilds
        them under the month lock; it passes ``warn_unresolved=False`` so the
        same gap is not logged twice per issue per cycle."""
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(_issue(_comment("1")), preserve_on_incomplete=False, warn_unresolved=False)
        assert "public_visibility" not in caplog.text


class TestBoolSchemaSupport:
    """``bool`` was not a supported dtype before this column existed.

    ``get_pyarrow_schema`` fell through to ``pa.string()`` and ``apply_schema``
    raised ``ArrowTypeError`` on a real boolean, so both needed a branch.
    """

    def test_pyarrow_schema_emits_bool(self):
        assert get_pyarrow_schema(COMMENTS_SCHEMA).field("public_visibility").type == pa.bool_()

    def test_comments_schema_declares_the_column_as_bool(self):
        assert COMMENTS_SCHEMA["public_visibility"] == "bool"

    def test_mixed_true_false_null_round_trips(self):
        df = pd.DataFrame(
            [
                {"comment_id": "1", "public_visibility": True},
                {"comment_id": "2", "public_visibility": False},
                {"comment_id": "3", "public_visibility": None},
            ]
        )
        table = apply_schema(df, COMMENTS_SCHEMA)
        assert table.schema.field("public_visibility").type == pa.bool_()
        assert table.column("public_visibility").to_pylist() == [True, False, None]

    def test_all_null_column_stays_bool_typed(self):
        """An issue whose comments carry no signal must not degrade the column
        to string — the monthly parquets are read with ``union_by_name``, and a
        type that varies per month breaks the union."""
        table = apply_schema(pd.DataFrame([{"comment_id": "1"}]), COMMENTS_SCHEMA)
        assert table.schema.field("public_visibility").type == pa.bool_()
        assert table.column("public_visibility").to_pylist() == [None]

    def test_nan_from_a_concat_with_older_rows_becomes_null(self):
        """The incremental path concatenates new rows onto parquet rows written
        before this column existed; the missing side arrives as NaN."""
        old = pd.DataFrame([{"comment_id": "1"}])
        new = pd.DataFrame([{"comment_id": "2", "public_visibility": False}])
        table = apply_schema(pd.concat([old, new], ignore_index=True), COMMENTS_SCHEMA)
        assert table.column("public_visibility").to_pylist() == [None, False]


class TestBothTransformPathsCarryTheColumn:
    """Both production callers pass ``preserve_on_incomplete=False``, so the
    column flows from the one place it is populated."""

    def test_full_rebuild_path(self):
        records = transform_comments(_issue(_comment(jsdPublic=False)), preserve_on_incomplete=False)
        assert records[0]["public_visibility"] is False

    def test_incremental_payload_path(self, tmp_path):
        import json

        from connectors.jira.incremental_transform import _build_issue_payload

        issue = _issue(_comment(jsdPublic=False))
        issue["fields"]["created"] = "2026-01-01T09:00:00.000+0000"
        (tmp_path / "issues").mkdir()
        (tmp_path / "issues" / "SUPPORT-1.json").write_text(json.dumps(issue))

        payload = _build_issue_payload("SUPPORT-1", tmp_path, tmp_path / "attachments")

        assert payload is not None
        assert payload.comments[0]["public_visibility"] is False

    def test_preserve_contract_is_untouched(self):
        """A ``_comments_incomplete`` issue must still preserve, not rewrite."""
        issue = _issue(_comment(jsdPublic=True))
        issue["_comments_incomplete"] = True
        assert transform_comments(issue) is None


class TestWebhookFallbackDoesNotNullStoredVisibility:
    """The one path in this connector that transforms a payload other than a GET
    refetch: `process_webhook_event`'s fetch-failure fallback. Its write is an
    issue-scoped delete-then-insert, so a comment arriving without a boolean
    `jsdPublic` does not just fail to add information — it replaces an
    already-observed `public_visibility` with NULL.

    `jsdPublic` was measured present on 100% of comments across five fetch
    shapes, but webhook bodies were not one of them, and webhook serialization
    is precisely what the known Atlassian reports concern. So the completeness
    guard answers False when the flag is missing, and the existing
    `_comments_incomplete` marker preserves the stored rows.
    """

    @staticmethod
    def _payload(comments):
        return {"fields": {"comment": {"comments": comments, "total": len(comments)}}}

    def test_a_complete_thread_with_the_flag_is_still_accepted(self):
        from connectors.jira.service import _embedded_comments_are_complete

        payload = self._payload([{"id": "1", "jsdPublic": True}, {"id": "2", "jsdPublic": False}])
        assert _embedded_comments_are_complete(payload) is True

    def test_a_comment_missing_the_flag_makes_the_thread_incomplete(self):
        from connectors.jira.service import _embedded_comments_are_complete

        payload = self._payload([{"id": "1", "jsdPublic": True}, {"id": "2"}])
        assert _embedded_comments_are_complete(payload) is False, (
            "accepting this payload would overwrite an observed public_visibility with NULL"
        )

    def test_a_string_typed_flag_also_makes_the_thread_incomplete(self):
        """`_comment_public_visibility` resolves a non-boolean to NULL, so a
        string-typed flag reaches the store as NULL exactly like an absent one.
        The guard has to reject the same shapes the transform refuses to trust,
        or the two disagree about what counts as observed."""
        from connectors.jira.service import _embedded_comments_are_complete

        assert _embedded_comments_are_complete(self._payload([{"id": "1", "jsdPublic": "false"}])) is False
        assert _embedded_comments_are_complete(self._payload([{"id": "1", "jsdPublic": None}])) is False

    def test_an_empty_thread_is_still_complete(self):
        """No comments, nothing to lose — and `total == 0` is a real state that
        must not be turned into a permanent incomplete marker."""
        from connectors.jira.service import _embedded_comments_are_complete

        assert _embedded_comments_are_complete(self._payload([])) is True

    def test_a_short_thread_is_incomplete_regardless_of_the_flag(self):
        """The original length check still stands on its own."""
        from connectors.jira.service import _embedded_comments_are_complete

        payload = {"fields": {"comment": {"comments": [{"id": "1", "jsdPublic": True}], "total": 5}}}
        assert _embedded_comments_are_complete(payload) is False
