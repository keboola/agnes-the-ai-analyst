"""Tests for lexical duplicate recall (SL012 support) — corpus + in-memory FTS."""

import duckdb
import pytest

from src.fts import ensure_fts_loaded
from src.store_guardrails.lint_corpus import top_candidates


def _skip_if_fts_unavailable():
    """Probe extension availability; skip BM25-dependent tests if it can't load.

    Mirrors ``tests/test_knowledge_fts_search.py`` — sandboxed/offline CI
    runners that block extension downloads must not fail these tests; the
    degraded (``[]``-returning) path has its own dedicated test.
    """
    if not ensure_fts_loaded(duckdb.connect(":memory:")):
        pytest.skip("fts extension not loadable in this environment")


_CORPUS = [
    {
        "id": "1",
        "name": "gong-import",
        "description": "Import Gong call transcripts into the CRM",
        "body": "Import call transcripts, match participants to accounts, MEDDPICC insights.",
    },
    {
        "id": "2",
        "name": "weather",
        "description": "Fetch weather forecasts",
        "body": "Query the forecast API for a city.",
    },
]


def test_body_similarity_recalls_renamed_duplicate():
    _skip_if_fts_unavailable()
    # same body, fresh AI name+description — the case name/description-only search misses
    got = top_candidates(
        "call-helper",
        "Assists with sales call data",
        "Import call transcripts, match participants to accounts, MEDDPICC insights.",
        _CORPUS,
        n=5,
    )
    assert got and got[0][0]["id"] == "1"


def test_unrelated_returns_low_or_empty():
    _skip_if_fts_unavailable()
    got = top_candidates(
        "weather-two", "Fetch weather forecasts", "Query the forecast API.", _CORPUS, n=5, exclude_id="2"
    )
    assert all(c["id"] != "2" for c, _ in got) or got == []


def test_fts_failure_degrades_to_empty(monkeypatch):
    monkeypatch.setattr("src.store_guardrails.lint_corpus.ensure_fts_loaded", lambda conn: False)
    assert top_candidates("x", "y", "z", _CORPUS, n=5) == []


def test_sl012_degraded_finding():
    from src.store_guardrails.skill_lint import lint_skill

    cands = [(_CORPUS[0], 3.2)]
    r = lint_skill(
        {"name": "call-helper", "description": "Use when importing call data into the CRM system.", "type": "skill"},
        "---\n---\n\nbody",
        candidates=cands,
    )
    f = next(f for f in r["findings"] if f["rule_id"] == "SL012")
    assert f["severity"] == "info" and "gong-import" in f["message"]


def test_empty_corpus_returns_empty():
    assert top_candidates("x", "y", "z", [], n=5) == []


def test_load_corpus_reads_published_skills(monkeypatch, tmp_path):
    """load_corpus() pulls published skills + reads their baked SKILL.md."""
    from src.store_guardrails import lint_corpus

    plugin_root = tmp_path / "e1" / "plugin"
    skill_dir = plugin_root / "skills" / "e1-suffixed"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: e1-suffixed\n---\n\nbody text", encoding="utf-8")

    class _FakeRepo:
        def list(self, **kwargs):
            assert kwargs.get("type") == "skill"
            assert kwargs.get("visibility_status") == ["approved"]
            return (
                [
                    {"id": "e1", "name": "example-skill", "description": "An example skill"},
                    {"id": "e2", "name": "unreadable-skill", "description": "No SKILL.md on disk"},
                ],
                2,
            )

    def _fake_plugin_dir(entity_id):
        return tmp_path / entity_id / "plugin"

    monkeypatch.setattr("src.repositories.store_entities_repo", lambda: _FakeRepo())
    monkeypatch.setattr("app.api.store._plugin_dir", _fake_plugin_dir)

    corpus = lint_corpus.load_corpus()

    by_id = {d["id"]: d for d in corpus}
    assert by_id["e1"]["body"] == "---\nname: e1-suffixed\n---\n\nbody text"
    assert by_id["e2"]["body"] == ""  # unreadable/missing degrades to ""
