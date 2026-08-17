"""Tests for the ``comments.public_visibility`` column.

Jira Service Management separates customer-facing replies from internal notes.
The platform API exposes that state as ``jsdPublic`` on every comment — present
on the comments embedded in a plain ``GET /issue/{key}``, so no ``expand`` and
no second request are needed. JSM's own storage, the ``sd.public.comment``
entity property, is the fallback when a payload predates or omits the flag.

The column is three-valued on purpose. A missing flag is written as NULL and
counted, never defaulted: a boolean that is confidently wrong is worse than one
that admits the gap, because nothing downstream can distinguish a defaulted
``true`` from an observed one.
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


def _property(internal) -> list[dict]:
    return [{"key": "sd.public.comment", "value": {"internal": internal}}]


def _visibility(comment: dict):
    records = transform_comments(_issue(comment), preserve_on_incomplete=False)
    assert records is not None and len(records) == 1
    return records[0]["public_visibility"]


class TestPublicVisibilityExtraction:
    """The five signal combinations a comment can arrive in."""

    def test_jsd_public_true_is_customer_facing(self):
        assert _visibility(_comment(jsdPublic=True)) is True

    def test_jsd_public_false_is_internal(self):
        assert _visibility(_comment(jsdPublic=False)) is False

    def test_falls_back_to_boolean_property(self):
        """No jsdPublic: derive from sd.public.comment. internal=True -> not public."""
        assert _visibility(_comment(properties=_property(True))) is False
        assert _visibility(_comment(properties=_property(False))) is True

    def test_string_typed_property_value_is_coerced_by_content_not_truthiness(self):
        """The regression this column exists to avoid.

        The same Jira instance stores ``internal`` as both a JSON boolean and
        the STRING ``"false"``. Every non-empty string is truthy, so a naive
        ``bool(raw)`` marks a PUBLIC comment internal. Measured on a live
        instance: 5 of 120 property-bearing comments in a 30-issue sample.
        """
        assert _visibility(_comment(properties=_property("false"))) is True
        assert _visibility(_comment(properties=_property("False"))) is True
        assert _visibility(_comment(properties=_property("true"))) is False

    def test_neither_signal_is_null_never_defaulted(self):
        assert _visibility(_comment()) is None

    def test_jsd_public_wins_over_property(self):
        """The flag is the platform's projection of the property; prefer it."""
        assert _visibility(_comment(jsdPublic=False, properties=_property(False))) is False

    def test_unrelated_and_malformed_properties_are_ignored(self):
        assert _visibility(_comment(properties=[{"key": "other", "value": {"internal": True}}])) is None
        assert _visibility(_comment(properties=[{"key": "sd.public.comment", "value": "nope"}])) is None
        assert _visibility(_comment(properties=[{"key": "sd.public.comment", "value": {}}])) is None
        assert _visibility(_comment(properties=["not-a-dict"])) is None

    def test_unresolved_comments_are_counted_in_a_warning(self, caplog):
        issue = _issue(_comment("1", jsdPublic=True), _comment("2"), _comment("3"))
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(issue, preserve_on_incomplete=False)
        assert "2 of 3 comments" in caplog.text
        assert "public_visibility written as NULL" in caplog.text

    def test_no_warning_when_every_comment_resolves(self, caplog):
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(_issue(_comment("1", jsdPublic=True)), preserve_on_incomplete=False)
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
