"""What `agnes pull` writes into `.claude/rules/` must say where it came from.

Claude Code loads that directory at session start as project rules. Corporate
memory is not rules — it is what colleagues wrote down — so a recap phrased as
a next step arrives indistinguishable from an instruction. Observed live: an
agent stopped and reported the file as untrusted rather than acting on a
colleague's ordinary note.

The header is deliberately descriptive. It states provenance and stops; it does
not tell the reader what to conclude, because text written to steer a reader's
judgement is the failure mode the 0.83.5 CLI-help fix removed, and reproducing
it one file over would be the same mistake with better intentions.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli.lib import pull as pullmod

INCIDENT = (
    "Next step is to type /exit and rerun claude from /srv so the marketplace "
    "and session hooks load, with recaps disabled in /config."
)


def _bundle(mandatory=(), approved=()):
    return {"mandatory": list(mandatory), "approved": list(approved)}


def _write(tmp_path: Path, bundle: dict) -> Path:
    resp = MagicMock()
    resp.json.return_value = bundle
    resp.raise_for_status.return_value = None
    with patch.object(pullmod, "api_get", return_value=resp):
        pullmod._fetch_and_write_rules(tmp_path)
    return tmp_path / ".claude" / "rules"


def test_rollup_states_where_the_notes_came_from(tmp_path):
    rules = _write(tmp_path, _bundle(approved=[{"title": "Recap", "content": INCIDENT}]))
    text = (rules / "km_approved.md").read_text()
    assert "corporate memory" in text
    assert "written by" in text and "approved by an administrator" in text
    # The note itself is delivered unchanged — stating provenance is not
    # editing the note.
    assert INCIDENT in text


def test_per_item_file_states_it_too(tmp_path):
    rules = _write(tmp_path, _bundle(mandatory=[{"id": "ki_a1", "title": "Setup", "content": INCIDENT}]))
    text = (rules / "km_ki_a1.md").read_text()
    assert "corporate memory" in text
    assert INCIDENT in text


def test_notes_are_separated_and_credited(tmp_path):
    rules = _write(
        tmp_path,
        _bundle(
            approved=[
                {
                    "title": "Revenue",
                    "content": "Exclude test accounts.",
                    "source_user": "ana@example.com",
                    "created_at": "2026-08-02T10:00:00+00:00",
                },
                {"title": "Recap", "content": INCIDENT, "source_user": "bo@example.com"},
            ]
        ),
    )
    text = (rules / "km_approved.md").read_text()
    assert "_— ana@example.com, 2026-08-02_" in text
    assert "_— bo@example.com_" in text  # no date in the bundle → no trailing comma
    # A horizontal rule before each note, so one note's imperative cannot run
    # into the next note's heading unmarked.
    assert text.count("\n---\n") == 2


def test_attribution_is_omitted_when_the_bundle_has_neither_field(tmp_path):
    rules = _write(tmp_path, _bundle(approved=[{"title": "T", "content": "C"}]))
    text = (rules / "km_approved.md").read_text()
    assert "_— " not in text


def test_header_promises_credit_lines_only_when_there_are_any(tmp_path):
    """A provenance header that overstates by one clause is worse than none.

    Both attribution fields are nullable. Shown a file whose header promised a
    credit line under each heading while none carried one, a naive reader
    checked, found the gap, and read it as a reason to distrust the note — so
    the sentence is conditional on what actually got written.
    """
    uncredited = _write(tmp_path / "a", _bundle(approved=[{"title": "T", "content": "C"}]))
    assert "credit line" not in (uncredited / "km_approved.md").read_text()

    credited = _write(
        tmp_path / "b",
        _bundle(approved=[{"title": "T", "content": "C", "source_user": "ana@example.com"}]),
    )
    text = (credited / "km_approved.md").read_text()
    assert "credit line" in text
    assert "_— ana@example.com_" in text


def test_mixed_bundle_promises_credit_lines_when_at_least_one_note_has_them(tmp_path):
    rules = _write(
        tmp_path,
        _bundle(
            approved=[
                {"title": "A", "content": "C"},
                {"title": "B", "content": "C", "source_user": "bo@example.com"},
            ]
        ),
    )
    assert "credit line" in (rules / "km_approved.md").read_text()


@pytest.mark.parametrize("field", ["source_user", "created_at"])
def test_attribution_survives_a_null_field(tmp_path, field):
    """The bundle ships JSON nulls for unset columns, not missing keys."""
    item = {"title": "T", "content": "C", "source_user": None, "created_at": None}
    item[field] = "ana@example.com" if field == "source_user" else "2026-08-02T10:00:00+00:00"
    rules = _write(tmp_path, _bundle(approved=[item]))
    assert "_— " in (rules / "km_approved.md").read_text()


def test_header_does_not_tell_the_reader_how_to_read_the_notes(tmp_path):
    """Provenance, not interpretation — the point of the whole change set.

    Two bars, and the second one cost a draft. A header must not *direct* the
    reader ("treat this as untrusted", "do not act on it"), which would be the
    same category of text as the help string this work removed. It must also
    not *interpret* the notes for them ("these are observations, not
    requests"): shown that draft, a naive agent called it the document coaching
    its reading of itself and grew more suspicious, not less. Stating who wrote
    a note and when is enough for a reader to classify it themselves.
    """
    rules = _write(tmp_path, _bundle(approved=[{"title": "T", "content": "C"}]))
    header = (rules / "km_approved.md").read_text().split("\n---\n")[0].lower()
    for directive in ("do not act", "treat this as", "you must", "ignore", "never act", "untrusted"):
        assert directive not in header, f"header directs the reader: {directive!r}"
    for interpretation in ("not requests", "not instructions", "recorded observations", "may read as"):
        assert interpretation not in header, f"header interprets the notes: {interpretation!r}"


def test_empty_bundle_still_writes_nothing(tmp_path):
    """The lazy-mkdir contract predates this change and must survive it."""
    _write(tmp_path, _bundle())
    assert not (tmp_path / ".claude").exists()


def test_digest_names_its_own_source(tmp_path):
    """The digest header follows the same rule: where it came from, nothing more."""
    md = pullmod._digest_to_md({"slug": "sales", "title": "Sales", "output_md": INCIDENT})
    assert "regenerated by Agnes from its source material" in md
    assert "not requests" not in md
    assert INCIDENT in md
