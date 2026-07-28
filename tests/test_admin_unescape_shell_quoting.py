"""Unit tests for `app.api.admin._unescape_shell_quoting`.

Pinning the contract: descriptions arriving with bash-style backslash
escapes (`Don\\'t`, `\\n`, `\\t`, `\\"`, etc.) are normalized at register /
update time so the row in `table_registry` carries the resolved text and
the UI doesn't have to render the literal escape bytes.

The JS mirror (`unescapeShellQuoting` in
`app/web/templates/admin_tables.html`) uses the same NUL-byte sentinel
to protect real backslashes during the unescape pass — these tests
indirectly pin the symmetry by anchoring the Python end of it.
"""

from __future__ import annotations

import pytest

from app.api.admin import _unescape_shell_quoting


class TestNoOpInputs:
    @pytest.mark.parametrize("value", [None, ""])
    def test_passes_through(self, value):
        assert _unescape_shell_quoting(value) == value

    def test_plain_text_unchanged(self):
        assert _unescape_shell_quoting("hello world") == "hello world"

    def test_real_apostrophes_unchanged(self):
        assert _unescape_shell_quoting("Don't worry") == "Don't worry"

    def test_real_newline_unchanged(self):
        assert _unescape_shell_quoting("line1\nline2") == "line1\nline2"


class TestStandardEscapes:
    def test_backslash_apostrophe(self):
        assert _unescape_shell_quoting(r"Don\'t") == "Don't"

    def test_backslash_n_to_real_newline(self):
        assert _unescape_shell_quoting(r"a\nb") == "a\nb"

    def test_backslash_t_to_real_tab(self):
        assert _unescape_shell_quoting(r"a\tb") == "a\tb"

    def test_backslash_r_to_real_cr(self):
        assert _unescape_shell_quoting(r"a\rb") == "a\rb"

    def test_backslash_double_quote(self):
        assert _unescape_shell_quoting(r'say \"hi\"') == 'say "hi"'

    def test_multiple_in_one_string(self):
        src = r"Don\'t do this:\nfoo\tbar"
        assert _unescape_shell_quoting(src) == "Don't do this:\nfoo\tbar"


class TestNulSentinel:
    """Real backslashes must survive the unescape pass — the helper uses a
    NUL-byte sentinel to protect them. Tests target that path explicitly
    so a refactor that breaks the sentinel order is caught immediately.
    """

    def test_double_backslash_becomes_single(self):
        """`\\\\` (4 chars: `\\` `\\`) → `\\` (1 char)."""
        assert _unescape_shell_quoting("\\\\") == "\\"

    def test_real_backslash_followed_by_n_is_preserved(self):
        """A real backslash + literal `n` (`\\\\n`) must not collapse to a
        newline — the sentinel pass protects the leading backslash."""
        assert _unescape_shell_quoting(r"\\n") == r"\n"

    def test_real_backslash_followed_by_apostrophe_is_preserved(self):
        assert _unescape_shell_quoting(r"\\'") == r"\'"


class TestIdempotency:
    """After one pass, the well-known escape *digraphs* (``\\n``, ``\\t``,
    ``\\'``, ``\\"``) are gone. A second pass on **canonical** output must
    be a no-op — that's what the migration script relies on so re-runs are
    safe.

    Caveat: a raw single backslash followed by ``n`` / ``r`` / ``t`` / ``'``
    / ``"`` is ambiguous with the digraph form, so an input shaped like
    ``\\not`` (single backslash + `not`) is NOT idempotent under repeated
    application — the second pass would unescape ``\\n`` to a newline.
    Documented as a known limitation; the migration script's `--dry-run`
    surface lets operators preview before committing.
    """

    @pytest.mark.parametrize(
        "src",
        [
            r"Don\'t do this",
            r"a\nb\tc",
            r"mix \'ed \\ stuff \n here",
            "plain text",
            "real\nnewline",
            "Don't worry — apostrophes are fine",
        ],
    )
    def test_second_pass_is_no_op_on_canonical(self, src):
        once = _unescape_shell_quoting(src)
        twice = _unescape_shell_quoting(once)
        assert once == twice

    def test_documented_non_idempotent_case(self):
        """Anchor the known-limitation behavior so a refactor can't silently
        change it. ``\\\\not`` (4 chars: two backslashes + `not`) collapses
        to ``\\not`` (4 chars: backslash + `not`) on the first pass; a
        second pass would then read ``\\n`` as a newline escape. Operators
        running the migration script with `--dry-run` see this preview
        before committing."""
        first = _unescape_shell_quoting(r"\\not")
        assert first == r"\not"
        second = _unescape_shell_quoting(first)
        # Second pass DOES change the value — because `\not` reads as
        # `\n` + `ot` to the unescape logic.
        assert second != first
        assert second == "\not"  # newline + 'ot'
