"""Regression guard for the install-prompt de-escalation.

The install prompt used to lean on force-style phrasing (ALL-CAPS
commands, "do NOT", "verbatim", "REFUSE", "PROCEED SILENTLY", mandatory /
default-yes framing, piped shell installers) that Claude Code's safety
classifier increasingly stalls on mid-install. This test pins the
de-escalated wording so it doesn't regress.

All three tiers are now enforced unconditionally: tier 1 is the
builder-owned scaffolding in `app/web/setup_instructions.py`, tier 2 the
bundled seed's install-prompt template, tier 3 the bundled connector
SKILL.md bodies. The tier 2/3 known-dirty ratchet is gone — the vendored
seed's de-escalation pass has landed, so any banned phrase reappearing
in it is a regression that fails here instead of skipping.

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
    """The thin default render — pure builder scaffolding."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    _assert_clean(joined, label="default render")


def test_ca_render_is_clean():
    """The TLS trust block (step 0) must also read as calm guidance."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM))
    _assert_clean(joined, label="ca render")


def test_operator_preamble_render_is_clean():
    """The one remaining render variant: an operator-authored preamble
    prepended above the scaffolding. Its own text is operator content, but
    the scaffolding around it must still pass the guard unchanged."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            instance_brand="BrandCo",
            workspace_dir="BrandCo",
            custom_preamble="Ask IT before installing anything on a managed laptop.",
        )
    )
    _assert_clean(joined, label="custom-preamble render")


def _scan_bundled_seed_file(rel_path: str) -> list[str]:
    from src.connectors_manifest import bundled_seed_path

    path = bundled_seed_path() / rel_path
    text = path.read_text(encoding="utf-8")
    return [phrase for phrase in BANNED_PHRASES if phrase in text]


def test_bundled_install_prompt_template_tier2():
    """Tier 2: the bundled install-prompt template — enforced with no
    baseline, so a banned phrase reappearing in the vendored seed fails
    here instead of being skipped.
    """
    hits = _scan_bundled_seed_file("install-prompt/template.md.tmpl")
    assert not hits, f"bundled install-prompt/template.md.tmpl: banned phrase(s) found: {sorted(hits)}"


def test_bundled_connector_skills_tier3():
    """Tier 3: bundled connector SKILL.md bodies — enforced with no
    baseline, per file, same as tier 2.
    """
    from src.connectors_manifest import bundled_seed_path

    root = bundled_seed_path() / "workspace" / ".claude" / "skills"
    dirty: dict[str, list[str]] = {}
    for skill_dir in sorted(root.glob("connector-*")):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        hits = sorted(phrase for phrase in BANNED_PHRASES if phrase in text)
        if hits:
            dirty[skill_dir.name] = hits

    assert not dirty, f"bundled connector SKILL.md file(s): banned phrase(s) found: {dirty}"
