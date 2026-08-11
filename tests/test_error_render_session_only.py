"""A session-only endpoint refusing a PAT must say what to do instead.

`/api/v1/agents` management routes require interactive owner auth by design
(`app/api/agents_admin.py` module docstring — a PAT must never be able to
mint another PAT or re-scope an agent). But `agnes agent list/show/...`
authenticate with the PAT `agnes auth login` stores, so those subcommands
can never succeed, and the CLI printed the server's sentence verbatim:

    HTTP 403: This endpoint requires an interactive session, not a PAT

which reads like a bug in the CLI rather than a deliberate boundary, and
names no way forward. The renderer appends one.
"""

from unittest.mock import patch


def _render(body, status=403):
    from cli.error_render import render_error

    with patch("cli.config.get_server_url", return_value="https://agnes.example"):
        return render_error(status, body)


class TestSessionOnlyHint:
    def test_pat_refusal_gains_an_actionable_hint(self):
        out = _render({"detail": "This endpoint requires an interactive session, not a PAT"})
        # The server's own sentence survives — it is the accurate diagnosis.
        assert "requires an interactive session" in out
        # ...and now carries somewhere to go.
        assert "https://agnes.example/agents" in out

    def test_service_token_refusal_gains_the_same_hint(self):
        out = _render({"detail": "This endpoint requires an interactive session, not a service token"})
        assert "https://agnes.example/agents" in out

    def test_unrelated_403_is_unchanged(self):
        out = _render({"detail": "Not authorized"})
        assert out == "HTTP 403: Not authorized"

    def test_unrelated_string_body_is_unchanged(self):
        assert _render("plain text", status=500) == "HTTP 500: plain text"

    def test_typed_dict_errors_still_render_as_before(self):
        out = _render({"detail": {"code": "budget_exhausted", "hint": "raise the cap"}}, status=429)
        assert out.startswith("Error: budget_exhausted (HTTP 429)")

    def test_hint_survives_an_unreachable_server_url(self):
        """The renderer runs on the error path — it must not raise there."""
        from cli.error_render import render_error

        with patch("cli.config.get_server_url", side_effect=OSError("no config")):
            out = render_error(403, {"detail": "This endpoint requires an interactive session, not a PAT"})
        assert "requires an interactive session" in out
