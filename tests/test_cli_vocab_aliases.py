"""CLI vocabulary sweep: consistent search flags across commands.

Covers the aliases added to `agnes collections search` (--limit for --k) and
`agnes admin sessions list` (--query/-q for --q). The marketplace positional
query and catalog `--show` implying `--metrics` are covered in
tests/test_cli_marketplace.py and tests/test_cli_catalog_metrics.py
respectively.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

runner = CliRunner()


# ---------------------------------------------------------------------------
# collections search --limit (alias for --k)
# ---------------------------------------------------------------------------


def test_collections_search_limit_alias_maps_to_k():
    from cli.commands.collections import collections_app

    captured: dict = {}

    def _fake_get(path, **kwargs):
        captured.update(kwargs)
        return {"results": []}

    with patch("cli.commands.collections.api_get_json", _fake_get):
        r = runner.invoke(collections_app, ["search", "hello", "--limit", "5"])
    assert r.exit_code == 0, r.output
    assert captured.get("k") == 5


def test_collections_search_k_flag_still_works():
    from cli.commands.collections import collections_app

    captured: dict = {}

    def _fake_get(path, **kwargs):
        captured.update(kwargs)
        return {"results": []}

    with patch("cli.commands.collections.api_get_json", _fake_get):
        r = runner.invoke(collections_app, ["search", "hello", "--k", "3"])
    assert r.exit_code == 0, r.output
    assert captured.get("k") == 3


# ---------------------------------------------------------------------------
# admin sessions list --query / -q (aliases for --q)
# ---------------------------------------------------------------------------


def test_admin_sessions_list_query_alias_maps_to_q(monkeypatch):
    from cli.commands.admin_sessions import sessions_app

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"rows": [], "total": 0}

    def _fake_get(path, params=None, **kwargs):
        captured.update(params or {})
        return _Resp()

    monkeypatch.setattr("cli.commands.admin_sessions.api_get", _fake_get)
    r = runner.invoke(sessions_app, ["list", "--query", "abc-123"])
    assert r.exit_code == 0, r.output
    assert captured.get("q") == "abc-123"


def test_admin_sessions_list_short_q_alias_maps_to_q(monkeypatch):
    from cli.commands.admin_sessions import sessions_app

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"rows": [], "total": 0}

    def _fake_get(path, params=None, **kwargs):
        captured.update(params or {})
        return _Resp()

    monkeypatch.setattr("cli.commands.admin_sessions.api_get", _fake_get)
    r = runner.invoke(sessions_app, ["list", "-q", "abc-123"])
    assert r.exit_code == 0, r.output
    assert captured.get("q") == "abc-123"


def test_admin_sessions_list_long_q_flag_still_works(monkeypatch):
    from cli.commands.admin_sessions import sessions_app

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"rows": [], "total": 0}

    def _fake_get(path, params=None, **kwargs):
        captured.update(params or {})
        return _Resp()

    monkeypatch.setattr("cli.commands.admin_sessions.api_get", _fake_get)
    r = runner.invoke(sessions_app, ["list", "--q", "abc-123"])
    assert r.exit_code == 0, r.output
    assert captured.get("q") == "abc-123"
