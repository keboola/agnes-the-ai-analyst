"""The shipped Claude settings template must point hooks at `da sync`, not the deleted server/scripts."""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "docs" / "setup" / "claude_settings.json"


def test_template_has_session_start_da_sync():
    cfg = json.loads(TEMPLATE.read_text())
    starts = cfg.get("hooks", {}).get("SessionStart", [])
    assert starts, "SessionStart hook missing"
    cmds = [h["command"] for entry in starts for h in entry.get("hooks", [])]
    assert any("da sync" in c and "--upload-only" not in c for c in cmds), (
        f"Expected `da sync` in SessionStart, got {cmds}"
    )


def test_template_has_session_end_upload():
    cfg = json.loads(TEMPLATE.read_text())
    ends = cfg.get("hooks", {}).get("SessionEnd", [])
    cmds = [h["command"] for entry in ends for h in entry.get("hooks", [])]
    assert any("da sync --upload-only" in c for c in cmds), (
        f"Expected `da sync --upload-only` in SessionEnd, got {cmds}"
    )


def test_template_drops_dead_server_scripts_reference():
    raw = TEMPLATE.read_text()
    assert "server/scripts/collect_session.py" not in raw, (
        "Template still references the deleted server/scripts/collect_session.py — "
        "the SessionEnd hook would silently fail."
    )
