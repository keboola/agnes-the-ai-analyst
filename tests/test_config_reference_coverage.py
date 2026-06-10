"""Ratchet: every per-instance config resolver must be documented.

``app/instance_config.py`` exposes the per-instance customization surface as
``get_*`` resolvers (env-var override > ``instance.yaml`` path > built-in
default). Knobs were historically added commit-by-commit without a matching row
in ``docs/CONFIGURATION.md`` — the same documentation-drift class the repo
already polices for DuckDB<->PG parity and REST x CLI x MCP coverage. This test
closes that gap: it fails when a resolver exists with no mention in the
configuration reference, so the doc stays the single authoritative map of the
override surface.

To satisfy it: add a row to the knob table in ``docs/CONFIGURATION.md`` keyed on
the resolver name in backticks (e.g. ``get_home_route()``). If a new ``get_*``
function is genuinely NOT an operator-facing knob, add it to
``NON_KNOB_RESOLVERS`` below with a one-line reason.
"""

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INSTANCE_CONFIG = _REPO_ROOT / "app" / "instance_config.py"
_CONFIG_DOC = _REPO_ROOT / "docs" / "CONFIGURATION.md"

# ``get_*`` functions that are NOT operator-facing instance.yaml/env knobs and
# therefore don't belong in the customization reference. Each exemption carries
# its reason so the list can't silently grow into a coverage loophole.
NON_KNOB_RESOLVERS = {
    # Primitive nested-key accessor used to build every other resolver.
    "get_value",
    # App-state backend selection (state machine in src/db_state_machine.py),
    # not an instance.yaml knob — documented in docs/postgres-cutover-runbook.md.
    "get_database_config",
    # Runtime credential probe (reads the ANTHROPIC_API_KEY / LLM_API_KEY .env
    # secrets), not an operator config knob — those secrets are documented in
    # the ".env infrastructure variables" section.
    "get_guardrails_llm_provider_ready",
}


def _config_resolvers() -> list[str]:
    """Top-level ``get_*`` function names defined in app/instance_config.py."""
    tree = ast.parse(_INSTANCE_CONFIG.read_text(encoding="utf-8"))
    return sorted(node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("get_"))


def test_every_config_resolver_is_documented() -> None:
    doc = _CONFIG_DOC.read_text(encoding="utf-8")
    undocumented = [
        name
        for name in _config_resolvers()
        if name not in NON_KNOB_RESOLVERS and not re.search(rf"\b{re.escape(name)}\b", doc)
    ]
    assert not undocumented, (
        "Config resolvers in app/instance_config.py missing from "
        "docs/CONFIGURATION.md:\n"
        + "\n".join(f"  - {name}()" for name in undocumented)
        + "\n\nAdd a row to the knob table (keyed on the resolver name in "
        "backticks), or add the name to NON_KNOB_RESOLVERS with a reason if it "
        "is not an operator-facing knob."
    )


def test_non_knob_exemptions_still_exist() -> None:
    """Guard the allowlist against bit-rot: an exemption naming a deleted
    resolver is a stale loophole. Keeps NON_KNOB_RESOLVERS honest."""
    resolvers = set(_config_resolvers())
    stale = sorted(NON_KNOB_RESOLVERS - resolvers)
    assert not stale, (
        "NON_KNOB_RESOLVERS names resolvers that no longer exist in "
        "app/instance_config.py — remove them:\n" + "\n".join(f"  - {name}" for name in stale)
    )
