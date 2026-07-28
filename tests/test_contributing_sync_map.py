"""Guard: the CONTRIBUTING.md sync-map must reference only real, current paths.

The sync-map (docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md §5)
is the single source of truth for the review team. If a referenced file is
renamed or deleted, this test fails so the doc is updated in the same change.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# Load-bearing paths the sync-map references. Each MUST exist on disk AND appear
# (in backticks) in CONTRIBUTING.md. Verified real at 0.66.1; keep in sync.
REFERENCED_PATHS = [
    "src/repositories/__init__.py",
    "src/db.py",
    "app/resource_types.py",
    "app/auth/access.py",
    "CHANGELOG.md",
    "connectors/keboola/extractor.py",
    "tests/test_backend_split_guard.py",
    "tests/db_pg/_parity_sweep_util.py",
    "tests/test_db_schema_version.py",
    "tests/test_design_system_contract.py",
    "tests/test_repository_registry.py",
    "cli/client.py",
    "cli/mcp/server.py",
    "app/api/mcp/tools_generator.py",
    "scripts/generate_openapi.py",
    "tests/snapshots/openapi.json",
    "tests/test_cli_api_parity.py",
]


def test_contributing_exists_with_sync_map_heading():
    assert CONTRIBUTING.exists(), "CONTRIBUTING.md must exist at repo root"
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "## Sync-map" in text, "CONTRIBUTING.md must have a '## Sync-map' section"


def test_referenced_paths_exist_on_disk():
    missing = [p for p in REFERENCED_PATHS if not (REPO_ROOT / p).exists()]
    assert not missing, f"sync-map references nonexistent paths: {missing}"


def test_referenced_paths_appear_in_doc():
    text = CONTRIBUTING.read_text(encoding="utf-8")
    absent = [p for p in REFERENCED_PATHS if f"`{p}`" not in text]
    assert not absent, f"paths in REFERENCED_PATHS but not in CONTRIBUTING.md: {absent}"


def test_sync_map_has_api_coverage_section():
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "API coverage (REST × CLI × MCP)" in text, (
        "CONTRIBUTING.md must document the REST/CLI/MCP coverage invariant"
    )
