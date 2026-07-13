"""Tests for skill linter (SL002, SL011, composition)."""

from src.store_guardrails.skill_lint import lint_skill, compute_content_hash


_GOOD = {
    "name": "demo-skill",
    "description": "Use when importing call transcripts into the CRM and matching participants to accounts.",
    "type": "skill",
}


def _md(body_len=300):
    return "---\nname: demo-skill\ndescription: x\n---\n\n# Demo\n\n" + ("word " * (body_len // 5))


def test_clean_skill_no_warn_findings():
    r = lint_skill(_GOOD, _md(), candidates=[])
    assert r["content_hash"] == compute_content_hash(_md())
    assert not [f for f in r["findings"] if f["severity"] == "warn"]


def test_sl002_fires_over_threshold():
    r = lint_skill(_GOOD, _md(body_len=9000), candidates=[])
    f = next(f for f in r["findings"] if f["rule_id"] == "SL002")
    assert f["severity"] == "warn" and "references/" in f["message"]
    assert f["doc_url"] == "/docs/skill-guidelines#sl002"


def test_sl011_degraded_only_and_info():
    bad = dict(_GOOD, description="A collection of many helpful things.")
    r = lint_skill(bad, _md(), candidates=[])  # craft=None → degraded
    f = next(f for f in r["findings"] if f["rule_id"] == "SL011")
    assert f["severity"] == "info"
    assert r["llm_used"] is False


def test_quality_check_composed_via_temp_tree():
    # A bare "TODO:" line trips quality_check's todo_floor slop pattern;
    # the stub body also trips its doc-too-short floor.
    r = lint_skill(_GOOD, "---\nname: demo-skill\n---\n\nTODO:\nwrite me later", candidates=[])
    assert any("placeholder" in f["message"].lower() or "todo" in f["message"].lower() for f in r["findings"])
    # Passthrough findings link the guidelines root, not a rule anchor.
    qc = [f for f in r["findings"] if f["rule_id"].startswith("QC-")]
    assert qc and all(f["doc_url"] == "/docs/skill-guidelines" for f in qc)


def test_engine_never_raises_on_broken_input():
    r = lint_skill({"name": "x", "description": None, "type": "skill"}, "", candidates=[])
    assert isinstance(r["findings"], list)
