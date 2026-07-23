"""Tests for `agnes marketplace` Typer wrapper.

Smoke + happy-path. Network calls are mocked so tests don't depend on a
running server.
"""

from __future__ import annotations

import json
import re

from typer.testing import CliRunner

from cli.commands.marketplace import marketplace_app

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ---------------------------------------------------------------------------
# Help smoke tests — guard against accidental command renames.
# ---------------------------------------------------------------------------


def test_marketplace_help_lists_subcommands():
    r = runner.invoke(marketplace_app, ["--help"])
    assert r.exit_code == 0
    out = _clean(r.output)
    for cmd in ("search", "detail", "add", "remove"):
        assert cmd in out, f"missing subcommand {cmd!r} in help"


def test_marketplace_search_help():
    r = runner.invoke(marketplace_app, ["search", "--help"])
    assert r.exit_code == 0
    out = _clean(r.output)
    for opt in ("--query", "--type", "--source", "--sort", "--limit", "--json"):
        assert opt in out, f"missing option {opt!r}"


def test_marketplace_detail_help():
    r = runner.invoke(marketplace_app, ["detail", "--help"])
    assert r.exit_code == 0
    assert "--json" in _clean(r.output)


def test_marketplace_add_help():
    r = runner.invoke(marketplace_app, ["add", "--help"])
    assert r.exit_code == 0


def test_marketplace_remove_help():
    r = runner.invoke(marketplace_app, ["remove", "--help"])
    assert r.exit_code == 0


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

_CURATED_ITEMS = [
    {
        "id": "foundry-ai/pdf-generator",
        "source": "curated",
        "type": "skill",
        "name": "pdf-generator",
        "owner": "c-marustamyan",
        "installed": True,
    }
]

_FLEA_ITEMS = [
    {
        "id": "abc123def456abc1",
        "source": "flea",
        "type": "agent",
        "name": "pdf-extractor",
        "owner": "someone",
        "installed": False,
    }
]


def _make_search_mock(curated=None, flea=None):
    """Returns a mock api_get_json that returns curated/flea data by tab param."""
    curated = curated if curated is not None else _CURATED_ITEMS
    flea = flea if flea is not None else _FLEA_ITEMS

    def _mock(*args, **kwargs):
        tab = kwargs.get("tab", "curated")
        if tab == "curated":
            return {"items": curated, "total": len(curated)}
        return {"items": flea, "total": len(flea)}

    return _mock


def test_marketplace_search_no_source_queries_both(monkeypatch):
    calls: list = []

    def _mock(*args, **kwargs):
        calls.append(kwargs.get("tab"))
        return _make_search_mock()(*args, **kwargs)

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _mock)

    r = runner.invoke(marketplace_app, ["search"])
    assert r.exit_code == 0, r.output
    assert "curated" in calls
    assert "flea" in calls
    out = _clean(r.output)
    assert "pdf-generator" in out
    assert "pdf-extractor" in out


def test_marketplace_search_source_curated(monkeypatch):
    calls: list = []

    def _mock(*args, **kwargs):
        calls.append(kwargs.get("tab"))
        return {"items": _CURATED_ITEMS, "total": 1}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _mock)

    r = runner.invoke(marketplace_app, ["search", "--source", "curated"])
    assert r.exit_code == 0, r.output
    assert calls == ["curated"]
    assert "pdf-generator" in _clean(r.output)


def test_marketplace_search_source_flea(monkeypatch):
    calls: list = []

    def _mock(*args, **kwargs):
        calls.append(kwargs.get("tab"))
        return {"items": _FLEA_ITEMS, "total": 1}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _mock)

    r = runner.invoke(marketplace_app, ["search", "--source", "flea"])
    assert r.exit_code == 0, r.output
    assert calls == ["flea"]
    assert "pdf-extractor" in _clean(r.output)


def test_marketplace_search_json(monkeypatch):
    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _make_search_mock())

    r = runner.invoke(marketplace_app, ["search", "--json"])
    assert r.exit_code == 0, r.output
    body = json.loads(_clean(r.output))
    assert "items" in body
    assert "total" in body


def test_marketplace_search_type_filter(monkeypatch):
    captured: dict = {}

    def _mock(*args, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _mock)

    r = runner.invoke(marketplace_app, ["search", "--type", "skill"])
    assert r.exit_code == 0, r.output
    assert captured.get("type") == "skill"


def test_marketplace_search_query_passed(monkeypatch):
    captured: dict = {}

    def _mock(*args, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _mock)

    runner.invoke(marketplace_app, ["search", "-q", "pdf"])
    assert captured.get("q") == "pdf"


def test_marketplace_search_no_results(monkeypatch):
    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", lambda *a, **kw: {"items": [], "total": 0})

    r = runner.invoke(marketplace_app, ["search", "-q", "nothing"])
    assert r.exit_code == 0
    assert "No results" in _clean(r.output)


def test_marketplace_search_positional_query_passed(monkeypatch):
    """Positional query text reaches the API as `q`, same as -q/--query."""
    captured: dict = {}

    def _mock(*args, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _mock)

    r = runner.invoke(marketplace_app, ["search", "pdf"])
    assert r.exit_code == 0, r.output
    assert captured.get("q") == "pdf"


def test_marketplace_search_positional_and_option_mismatch_exits_1(monkeypatch):
    """Conflicting positional query and -q/--query is an error, not a silent pick."""
    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", lambda *a, **kw: {"items": [], "total": 0})

    r = runner.invoke(marketplace_app, ["search", "pdf", "-q", "other"])
    assert r.exit_code == 1


def test_marketplace_search_positional_and_option_same_ok(monkeypatch):
    """Positional and -q/--query agreeing is fine."""
    captured: dict = {}

    def _mock(*args, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _mock)

    r = runner.invoke(marketplace_app, ["search", "pdf", "-q", "pdf"])
    assert r.exit_code == 0, r.output
    assert captured.get("q") == "pdf"


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------

_CURATED_DETAIL = {
    "source": "curated",
    "marketplace_id": "foundry-ai",
    "plugin_name": "pdf-generator",
    "manifest_name": "pdf-generator",
    "display_name": "PDF Generator",
    "type": "skill",
    "version": "1.2.0",
    "tagline": "Generate PDFs from data",
    "description": "Generates PDF documents.",
    "installed": True,
    "use_cases": [{"title": "Export report"}, {"title": "Generate invoice"}],
    "skills": [{"name": "pdf-generator"}],
    "commands": ["/pdf-by-c-marustamyan"],
    "mcps": [],
    "agents": [],
}

_FLEA_DETAIL = {
    "source": "flea",
    "entity_id": "abc123def456abc1",
    "plugin_name": "pdf-extractor",
    "manifest_name": "pdf-extractor",
    "type": "agent",
    "version": "0.9.0",
    "description": "Extracts text from PDFs.",
    "installed": False,
    "use_cases": [],
    "skills": [],
    "commands": [],
    "mcps": [],
    "agents": [],
}


def test_marketplace_detail_curated(monkeypatch):
    captured: dict = {}

    def _mock(path, **kw):
        captured["path"] = path
        return _CURATED_DETAIL

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _mock)

    r = runner.invoke(marketplace_app, ["detail", "foundry-ai/pdf-generator"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/marketplace/curated/foundry-ai/pdf-generator"
    out = _clean(r.output)
    assert "PDF Generator" in out
    assert "In your stack" in out
    assert "Export report" in out


def test_marketplace_detail_flea(monkeypatch):
    captured: dict = {}

    def _mock(path, **kw):
        captured["path"] = path
        return _FLEA_DETAIL

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", _mock)

    r = runner.invoke(marketplace_app, ["detail", "abc123def456abc1"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/marketplace/flea/abc123def456abc1/detail"
    out = _clean(r.output)
    assert "pdf-extractor" in out
    assert "Not in stack" in out
    assert "agnes marketplace add abc123def456abc1" in out


def test_marketplace_detail_json(monkeypatch):
    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_get_json", lambda *a, **kw: _CURATED_DETAIL)

    r = runner.invoke(marketplace_app, ["detail", "--json", "foundry-ai/pdf-generator"])
    assert r.exit_code == 0, r.output
    body = json.loads(_clean(r.output))
    assert body["plugin_name"] == "pdf-generator"


def test_marketplace_detail_not_found(monkeypatch):
    from cli.v2_client import V2ClientError

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(
        mp_mod,
        "api_get_json",
        lambda *a, **kw: (_ for _ in ()).throw(V2ClientError(404, {"detail": "not_found"})),
    )

    r = runner.invoke(marketplace_app, ["detail", "foundry-ai/missing"])
    assert r.exit_code == 1


def test_marketplace_detail_forbidden(monkeypatch):
    from cli.v2_client import V2ClientError

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(
        mp_mod,
        "api_get_json",
        lambda *a, **kw: (_ for _ in ()).throw(V2ClientError(403, {"detail": "forbidden"})),
    )

    r = runner.invoke(marketplace_app, ["detail", "foundry-ai/secret"])
    assert r.exit_code == 1


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_marketplace_add_curated(monkeypatch):
    captured: dict = {}

    def _post(path, payload):
        captured["path"] = path
        return {"installed": True}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_post_json", _post)

    r = runner.invoke(marketplace_app, ["add", "foundry-ai/pdf-generator"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/marketplace/curated/foundry-ai/pdf-generator/install"
    assert "Added" in _clean(r.output)
    assert "update-agnes-plugins" in _clean(r.output)


def test_marketplace_add_flea(monkeypatch):
    captured: dict = {}

    def _post(path, payload):
        captured["path"] = path
        return {"entity_id": "abc123def456abc1", "installed": True}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_post_json", _post)

    r = runner.invoke(marketplace_app, ["add", "abc123def456abc1"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/store/entities/abc123def456abc1/install"
    assert "Added" in _clean(r.output)


def test_marketplace_add_accepts_tab_prefixed_search_ids(monkeypatch):
    """`agnes marketplace search` prints tab-prefixed ids (`curated-<mid>/<plugin>`,
    `flea-<uuid>`); a copy-pasted id must route to the bare-form endpoints."""
    paths: list = []

    def _post(path, payload):
        paths.append(path)
        return {"installed": True}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_post_json", _post)

    r = runner.invoke(marketplace_app, ["add", "curated-foundry-ai/pdf-generator"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(marketplace_app, ["add", "flea-abc123def456abc1"])
    assert r.exit_code == 0, r.output
    assert paths == [
        "/api/marketplace/curated/foundry-ai/pdf-generator/install",
        "/api/store/entities/abc123def456abc1/install",
    ]


def test_marketplace_add_system_plugin_409(monkeypatch):
    from cli.v2_client import V2ClientError

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(
        mp_mod,
        "api_post_json",
        lambda *a, **kw: (_ for _ in ()).throw(V2ClientError(409, {"detail": "cannot_unsubscribe_system_plugin"})),
    )

    r = runner.invoke(marketplace_app, ["add", "foundry-ai/core"])
    assert r.exit_code == 1
    assert "system plugin" in _clean(r.stderr or r.output)


def test_marketplace_add_not_approved_409(monkeypatch):
    from cli.v2_client import V2ClientError

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(
        mp_mod,
        "api_post_json",
        lambda *a, **kw: (_ for _ in ()).throw(V2ClientError(409, {"detail": "entity_not_approved"})),
    )

    r = runner.invoke(marketplace_app, ["add", "abc123def456abc1"])
    assert r.exit_code == 1
    assert "approved" in _clean(r.stderr or r.output)


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_marketplace_remove_curated(monkeypatch):
    captured: dict = {}

    def _delete(path):
        captured["path"] = path
        return {"installed": False}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_delete", _delete)

    r = runner.invoke(marketplace_app, ["remove", "foundry-ai/pdf-generator"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/marketplace/curated/foundry-ai/pdf-generator/install"
    assert "Removed" in _clean(r.output)
    assert "update-agnes-plugins" in _clean(r.output)


def test_marketplace_remove_flea(monkeypatch):
    captured: dict = {}

    def _delete(path):
        captured["path"] = path
        return {"entity_id": "abc123def456abc1", "installed": False}

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(mp_mod, "api_delete", _delete)

    r = runner.invoke(marketplace_app, ["remove", "abc123def456abc1"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/store/entities/abc123def456abc1/install"
    assert "Removed" in _clean(r.output)


def test_marketplace_remove_system_plugin_409(monkeypatch):
    from cli.v2_client import V2ClientError

    import cli.commands.marketplace as mp_mod

    monkeypatch.setattr(
        mp_mod,
        "api_delete",
        lambda *a, **kw: (_ for _ in ()).throw(V2ClientError(409, {"detail": "cannot_unsubscribe_system_plugin"})),
    )

    r = runner.invoke(marketplace_app, ["remove", "foundry-ai/core"])
    assert r.exit_code == 1
    assert "system plugin" in _clean(r.stderr or r.output)
