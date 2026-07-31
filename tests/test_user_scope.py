"""cli/lib/user_scope.py — marker splice + user-level hook writers (spec §6.4).

Recovery philosophy under test: on anything unexpected, warn and leave the
user's file untouched. Never rebuild a user-owned file.
"""

import json

from cli.lib.user_scope import (
    RAILS_BEGIN,
    RAILS_END,
    rails_block_state,
    remove_rails_block,
    upsert_rails_block,
)

RAILS = "line one\nline two\n"


def test_upsert_creates_file_and_block(tmp_path):
    md = tmp_path / "CLAUDE.md"
    assert upsert_rails_block(md, RAILS) == "created"
    text = md.read_text(encoding="utf-8")
    assert text.count(RAILS_BEGIN) == 1 and text.count(RAILS_END) == 1
    assert "line one" in text
    assert rails_block_state(md, RAILS) == "ok"


def test_upsert_appends_to_existing_content_untouched(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# my own rules\ndo not touch\n", encoding="utf-8")
    assert upsert_rails_block(md, RAILS) == "created"
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# my own rules\ndo not touch\n")


def test_upsert_replaces_only_inside_markers(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(
        f"before\n{RAILS_BEGIN}\nOLD CONTENT\n{RAILS_END}\nafter\n",
        encoding="utf-8",
    )
    assert upsert_rails_block(md, RAILS) == "updated"
    text = md.read_text(encoding="utf-8")
    assert "OLD CONTENT" not in text
    assert text.startswith("before\n") and text.rstrip().endswith("after")
    assert rails_block_state(md, RAILS) == "ok"


def test_upsert_idempotent(tmp_path):
    md = tmp_path / "CLAUDE.md"
    upsert_rails_block(md, RAILS)
    before = md.read_text(encoding="utf-8")
    assert upsert_rails_block(md, RAILS) == "unchanged"
    assert md.read_text(encoding="utf-8") == before


def test_duplicated_markers_leave_file_untouched(tmp_path):
    md = tmp_path / "CLAUDE.md"
    broken = f"{RAILS_BEGIN}\na\n{RAILS_END}\n{RAILS_BEGIN}\nb\n{RAILS_END}\n"
    md.write_text(broken, encoding="utf-8")
    assert upsert_rails_block(md, RAILS) == "skipped_malformed"
    assert md.read_text(encoding="utf-8") == broken
    assert rails_block_state(md, RAILS) == "malformed"


def test_unmatched_marker_leaves_file_untouched(tmp_path):
    md = tmp_path / "CLAUDE.md"
    broken = f"{RAILS_BEGIN}\nno end marker\n"
    md.write_text(broken, encoding="utf-8")
    assert upsert_rails_block(md, RAILS) == "skipped_malformed"
    assert md.read_text(encoding="utf-8") == broken


def test_remove_strips_block_exactly(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("mine\n", encoding="utf-8")
    upsert_rails_block(md, RAILS)
    assert remove_rails_block(md) == "removed"
    assert md.read_text(encoding="utf-8") == "mine\n"


def test_remove_absent_and_state_missing(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("mine\n", encoding="utf-8")
    assert remove_rails_block(md) == "absent"
    assert rails_block_state(md, RAILS) == "missing"


def test_remove_deletes_file_when_block_was_everything(tmp_path):
    md = tmp_path / "CLAUDE.md"
    upsert_rails_block(md, RAILS)
    assert remove_rails_block(md) == "removed"
    assert not md.exists()


def test_state_drifted_on_stale_content(tmp_path):
    md = tmp_path / "CLAUDE.md"
    upsert_rails_block(md, "old rails\n")
    assert rails_block_state(md, RAILS) == "drifted"


def test_load_global_rails_compact_and_marker_free():
    from cli.lib.user_scope import load_global_rails

    text = load_global_rails()
    assert 5 < len(text.splitlines()) <= 25, "rails block must stay compact (spec §13.3)"
    assert RAILS_BEGIN not in text, "template carries content only, markers are added by the splice"
    assert "agnes catalog" in text and "agnes skills show agnes-data-querying" in text


# ---------------------------------------------------------------------------
# User-level SessionStart hook writers (spec §7.3)
# ---------------------------------------------------------------------------


def _fake_user_settings(tmp_path, monkeypatch, initial):
    settings = tmp_path / "user-claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(initial, dict):
        settings.write_text(json.dumps(initial), encoding="utf-8")
    elif isinstance(initial, str):
        settings.write_text(initial, encoding="utf-8")
    monkeypatch.setattr("cli.lib.user_scope.user_settings_path", lambda: settings)
    return settings


def test_install_user_hook_creates_entry_and_is_idempotent(tmp_path, monkeypatch):
    from cli.lib.user_scope import GLOBAL_UPDATE_HOOK_CMD, install_user_session_hook, user_session_hook_state

    settings = _fake_user_settings(tmp_path, monkeypatch, {"env": {"FOO": "1"}})
    assert install_user_session_hook() == "installed"
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    assert cfg["env"] == {"FOO": "1"}, "foreign keys preserved"
    cmds = [h["command"] for entry in cfg["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert cmds == [GLOBAL_UPDATE_HOOK_CMD]
    assert install_user_session_hook() == "unchanged"
    assert user_session_hook_state() == "ok"


def test_install_preserves_third_party_hooks(tmp_path, monkeypatch):
    from cli.lib.user_scope import install_user_session_hook, remove_user_session_hook

    third_party = {"hooks": [{"type": "command", "command": "echo hello-from-elsewhere"}]}
    settings = _fake_user_settings(tmp_path, monkeypatch, {"hooks": {"SessionStart": [third_party]}})
    install_user_session_hook()
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    assert len(cfg["hooks"]["SessionStart"]) == 2
    assert remove_user_session_hook() == "removed"
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    cmds = [h["command"] for entry in cfg["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert cmds == ["echo hello-from-elsewhere"]


def test_corrupt_user_settings_left_untouched(tmp_path, monkeypatch):
    from cli.lib.user_scope import install_user_session_hook, remove_user_session_hook, user_session_hook_state

    settings = _fake_user_settings(tmp_path, monkeypatch, "{not json")
    assert install_user_session_hook() == "skipped_malformed"
    assert settings.read_text(encoding="utf-8") == "{not json"
    assert remove_user_session_hook() == "skipped_malformed"
    assert user_session_hook_state() == "malformed"


def test_remove_hook_absent(tmp_path, monkeypatch):
    from cli.lib.user_scope import remove_user_session_hook, user_session_hook_state

    _fake_user_settings(tmp_path, monkeypatch, None)
    assert remove_user_session_hook() == "absent"
    assert user_session_hook_state() == "missing"
