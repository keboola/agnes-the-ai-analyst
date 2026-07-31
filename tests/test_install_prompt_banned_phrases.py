"""Regression guard for the install-prompt de-escalation.

The install prompt used to lean on force-style phrasing (ALL-CAPS
commands, "do NOT", "verbatim", "REFUSE", "PROCEED SILENTLY", mandatory /
default-yes framing, piped shell installers) that Claude Code's safety
classifier increasingly stalls on mid-install. This test pins the
de-escalated wording so it doesn't regress.

Tier 1 (builder-owned scaffolding in `app/web/setup_instructions.py`) is
enforced unconditionally. Tier 2/3 (the bundled seed's install-prompt
template + connector SKILL.md bodies) is vendored content out of this
module's scope — those checks `pytest.skip` and list any hits until the
seed repo does its own pass, then start enforcing automatically.

Banned-phrase / required-fact lists are shared with
`scripts/dev/check_prompt.py` via `scripts/dev/prompt_phrases.py` so the
manual verification loop and this guard never drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DEV = Path(__file__).resolve().parents[1] / "scripts" / "dev"
if str(_SCRIPTS_DEV) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DEV))

from prompt_phrases import BANNED_PHRASES, REQUIRED_FACTS  # noqa: E402


def _assert_clean(text: str, *, label: str) -> None:
    hits = [phrase for phrase in BANNED_PHRASES if phrase in text]
    assert not hits, f"{label}: banned phrase(s) found: {hits}"
    missing = [fact for fact in REQUIRED_FACTS if fact not in text]
    assert not missing, f"{label}: required fact(s) missing: {missing}"


_FAKE_CA_PEM = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"


def test_default_render_is_clean():
    """No connectors at all — the leanest render, pure builder scaffolding."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", connector_manifest=[]))
    _assert_clean(joined, label="default render (no connectors)")


def test_ca_render_is_clean():
    """The TLS trust block (step 0) must also read as calm guidance."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", connector_manifest=[], ca_pem=_FAKE_CA_PEM))
    _assert_clean(joined, label="ca render (no connectors)")


def test_fake_connectors_render_is_clean(monkeypatch):
    """Fake connector entries with deterministic bodies isolate the
    builder-owned connector scaffolding (Ask lines, headers, trailers)
    from the bundled seed's own (out-of-scope) SKILL.md prose.
    """
    from tests.test_setup_instructions import _connector_entry, _fake_bodies

    from app.web.setup_instructions import resolve_lines

    _fake_bodies(monkeypatch)
    manifest = [
        _connector_entry("connector-xtool", "XTool", required=True),
        _connector_entry("connector-ztool", "ZTool"),
    ]
    joined = "\n".join(resolve_lines("agnes.whl", connector_manifest=manifest))
    _assert_clean(joined, label="fake-connector render (required + optional)")


def _scan_bundled_seed_file(rel_path: str) -> list[str]:
    from src.connectors_manifest import bundled_seed_path

    path = bundled_seed_path() / rel_path
    text = path.read_text(encoding="utf-8")
    return [phrase for phrase in BANNED_PHRASES if phrase in text]


def test_bundled_install_prompt_template_tier2():
    """Tier 2: the bundled install-prompt template is vendored content
    (out of this PR's scope). Skip with the findings listed until the
    seed repo does its own de-escalation pass; once clean this starts
    enforcing automatically.
    """
    hits = _scan_bundled_seed_file("install-prompt/template.md.tmpl")
    if hits:
        import pytest

        pytest.skip(
            "bundled install-prompt/template.md.tmpl still has banned "
            f"phrase(s), pending the seed repo's own de-escalation pass: {hits}"
        )


def test_bundled_connector_skills_tier3():
    """Tier 3: bundled connector SKILL.md bodies are vendored content
    (out of this PR's scope, guarded by the bundled-seed CI check). Skip
    with the findings listed per-file until the seed repo does its own
    de-escalation pass; once clean this starts enforcing automatically.
    """
    import pytest

    from src.connectors_manifest import bundled_seed_path

    root = bundled_seed_path() / "workspace" / ".claude" / "skills"
    findings: dict[str, list[str]] = {}
    for skill_dir in sorted(root.glob("connector-*")):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        hits = [phrase for phrase in BANNED_PHRASES if phrase in text]
        if hits:
            findings[skill_dir.name] = hits

    if findings:
        pytest.skip(
            "bundled connector SKILL.md file(s) still have banned phrase(s), "
            f"pending the seed repo's own de-escalation pass: {findings}"
        )
