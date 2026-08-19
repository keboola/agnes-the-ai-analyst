"""Guards for the bundled reference install-prompt template.

`src/_bundled_seed/install-prompt/template.md.tmpl` is two things at once:

  * the fallback the install prompt renders when no Initial Workspace
    Template is configured (resolved by `src.initial_workspace.
    resolve_seed_file`, exercised by the sync render dry-run in
    `app/api/initial_workspace.py::_compute_render_dry_run`), and
  * the reference document operators fork into their own IWT.

Both roles make it a placeholder-contract surface: a placeholder nothing
substitutes is rendered LITERALLY into the analyst's prompt (deliberately —
see `docs/seed-repo-contract.md` §5 — a typo must surface as visible text,
not a 500 on `/home`). So a retired placeholder here is a visible defect on
every fresh install AND a broken example every operator copies.

Nothing else in the suite reads this file for shape (the banned-phrase
ratchet in `tests/test_install_prompt_banned_phrases.py` scans it for
coercive wording only), which is how it survived the thin-prompt rewrite
still carrying the fat prompt's blocks.
"""

from __future__ import annotations

import re

# Placeholders `docs/seed-repo-contract.md` §5 declares live. Anything else
# in the template is either a typo or a block that was retired with the fat
# prompt — both render as literal text.
_LIVE_PLACEHOLDERS = frozenset(
    {
        "{server_url}",
        "{wheel_filename}",
        "{server_host}",
        "{workspace_dir}",
        "{instance_brand}",
        "{tls_trust_block}",
        "{install_cli_block}",
    }
)

# Retired with the blocks they injected when the prompt went thin
# (2026-08-19). Pinned explicitly — the generic scan above would catch them
# too, but naming them keeps the failure message actionable.
_RETIRED_PLACEHOLDERS = (
    "{marketplace_block}",
    "{connector_tiles}",
    "{ca_bundle_finale_bullet}",
    # The access token is delivered out-of-band to `~/.agnes/token`; a
    # template referencing it would write the literal string `{token}` into
    # every analyst's token file.
    "{token}",
)

_PLACEHOLDER_RE = re.compile(r"\{[a-z][a-z0-9_]*\}")


def _template_text() -> str:
    from src.connectors_manifest import bundled_seed_path

    return (bundled_seed_path() / "install-prompt" / "template.md.tmpl").read_text(encoding="utf-8")


def test_no_retired_placeholders():
    text = _template_text()
    hits = [p for p in _RETIRED_PLACEHOLDERS if p in text]
    assert not hits, (
        f"bundled install-prompt/template.md.tmpl references retired placeholder(s) {hits} — "
        "nothing substitutes them, so they render literally into the analyst's prompt"
    )


def test_only_contract_declared_placeholders():
    """Stronger form of the check above: whatever the retired set forgets,
    the seed-repo contract's own table catches."""
    unknown = sorted(set(_PLACEHOLDER_RE.findall(_template_text())) - _LIVE_PLACEHOLDERS)
    assert not unknown, (
        f"bundled install-prompt/template.md.tmpl uses placeholder(s) {unknown} that "
        "docs/seed-repo-contract.md §5 does not declare — they render literally"
    )


def test_mirrors_the_thin_prompt_shape():
    """The template is the reference an operator forks, so it must show the
    thin shape: install the CLI, `agnes onboard`, restart, confirm — not the
    old English program whose steps `agnes onboard` now owns."""
    text = _template_text()

    assert "{install_cli_block}" in text
    assert "agnes onboard --server-url" in text
    assert "--accept-dir" in text
    assert "3) Restart Claude Code" in text
    assert "4) Confirm:" in text
    assert "5)" not in text

    # Orchestration that moved into the CLI must not be spelled out again.
    for gone in (
        "agnes init",
        "agnes catalog",
        "agnes refresh-marketplace",
        "agnes connectors show",
        "Run diagnostics",
        "(Y/n)",
    ):
        assert gone not in text, f"retired instruction {gone!r} survives in the bundled template"


def test_stays_short():
    """Same ceiling logic as `tests/test_setup_instructions.py::
    test_prompt_stays_short`, minus the ~20 lines step 1 contributes through
    `{install_cli_block}` — a re-grown section shows up here first."""
    assert len(_template_text().splitlines()) < 60
