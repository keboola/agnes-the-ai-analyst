"""Tests for `agnes config set-server` — merge-safe server URL write."""

import json

from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def test_set_server_creates_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    # Assert on the PERSISTED config, not get_server_url(): that getter reads
    # the AGNES_SERVER env var first, and auth/setup set it via os.environ
    # without restoring, so it leaks across xdist tests and would shadow what
    # we just wrote. Drop any leaked value and check config.yaml directly.
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    result = runner.invoke(app, ["config", "set-server", "https://s.example.com"])
    assert result.exit_code == 0
    from cli.config import load_config

    assert load_config().get("server") == "https://s.example.com"


def test_set_server_preserves_existing_keys(tmp_path, monkeypatch):
    """Setting the server URL must NOT drop other config keys (workspace_root)."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli.config import load_config, save_config, set_workspace_root

    set_workspace_root("/home/me/ws")
    save_config({"server": "https://old.example.com"})

    result = runner.invoke(app, ["config", "set-server", "https://new.example.com"])
    assert result.exit_code == 0

    cfg = load_config()
    assert cfg["server"] == "https://new.example.com"
    assert cfg["workspace_root"] == "/home/me/ws"  # preserved, not clobbered


def test_set_server_repairs_malformed_config(tmp_path, monkeypatch):
    """`set-server` is the install-time repair path — an unparseable config.yaml
    must be backed up and rewritten cleanly, not crash the command."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    (tmp_path / "config.yaml").write_text("this: is: not: valid: [unclosed\n", encoding="utf-8")

    result = runner.invoke(app, ["config", "set-server", "https://s.example.com"])
    assert result.exit_code == 0, result.output

    from cli.config import load_config

    assert load_config().get("server") == "https://s.example.com"
    # the unparseable original is preserved for forensics, not silently dropped
    assert len(list(tmp_path.glob("config.yaml.corrupt.*"))) == 1


def test_set_server_repairs_non_mapping_config(tmp_path, monkeypatch):
    """A valid-YAML-but-non-mapping config.yaml (a list/scalar) can't be merged;
    it must be backed up and replaced with a clean mapping."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    (tmp_path / "config.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")

    result = runner.invoke(app, ["config", "set-server", "https://s.example.com"])
    assert result.exit_code == 0, result.output

    from cli.config import load_config

    assert load_config() == {"server": "https://s.example.com"}
    assert len(list(tmp_path.glob("config.yaml.corrupt.*"))) == 1


# ---------------------------------------------------------------------------
# Workspace-scoped sync_state (#1311). A bare call stays machine-global
# (back-compat for every pre-existing caller, e.g.
# `agnes diagnose system --local`); passing `workspace=` reads/writes
# `<workspace>/.claude/agnes/sync_state.json` instead, so two workspaces on
# one machine no longer share a single download-hash record.
# ---------------------------------------------------------------------------


def test_bare_sync_state_call_stays_machine_global(tmp_path, monkeypatch):
    """No `workspace` argument -> unchanged legacy behavior."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli.config import get_sync_state, save_sync_state

    assert get_sync_state() == {}
    save_sync_state({"tables": {"t": {"hash": "x"}}})
    assert (tmp_path / "sync_state.json").exists()
    assert get_sync_state() == {"tables": {"t": {"hash": "x"}}}


def test_workspace_scoped_state_is_written_under_dot_claude_agnes(tmp_path, monkeypatch):
    """`workspace=` writes to `<workspace>/.claude/agnes/sync_state.json` —
    not the legacy machine-global file, and not step 8's own
    `<workspace>/.claude/sync_state.json` (a different schema, owned by
    `cli/lib/pull_sync.py`)."""
    cfg_dir = tmp_path / "_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    from cli.config import get_sync_state, save_sync_state

    ws = tmp_path / "ws"
    save_sync_state({"tables": {"t": {"hash": "x"}}}, workspace=ws)

    scoped = ws / ".claude" / "agnes" / "sync_state.json"
    assert scoped.exists()
    assert not (cfg_dir / "sync_state.json").exists(), "must not also write the legacy file"
    assert not (ws / ".claude" / "sync_state.json").exists(), "must not collide with step 8's own state file"
    assert get_sync_state(workspace=ws) == {"tables": {"t": {"hash": "x"}}}


def test_two_workspaces_have_independent_scoped_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    from cli.config import get_sync_state, save_sync_state

    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    save_sync_state({"tables": {"t": {"hash": "from-a"}}}, workspace=ws_a)
    save_sync_state({"tables": {"t": {"hash": "from-b"}}}, workspace=ws_b)

    assert get_sync_state(workspace=ws_a)["tables"]["t"]["hash"] == "from-a"
    assert get_sync_state(workspace=ws_b)["tables"]["t"]["hash"] == "from-b"


def test_workspace_state_read_does_not_write_anything(tmp_path, monkeypatch):
    """A read-only `get_sync_state(workspace=...)` call — including one that
    migrates from the legacy file — must not itself create any file:
    `run_pull(dry_run=True)` reads sync state before its own dry-run
    short-circuit, and that path must stay side-effect-free."""
    cfg_dir = tmp_path / "_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    from cli.config import get_sync_state, save_sync_state

    save_sync_state({"tables": {"t": {"hash": "legacy"}}})  # legacy file only
    ws = tmp_path / "ws"

    state = get_sync_state(workspace=ws)
    assert state == {"tables": {"t": {"hash": "legacy"}}}
    assert not (ws / ".claude").exists(), "a pure read must not create the workspace's .claude dir"


def test_legacy_state_migrates_into_a_fresh_workspace(tmp_path, monkeypatch):
    """One-time migration: a workspace with no scoped state yet, but a
    legacy machine-global file present, is seeded from it so the first pull
    after upgrading doesn't re-download every table — and the legacy file is
    left untouched for an older CLI (or another not-yet-migrated workspace)
    still reading it."""
    cfg_dir = tmp_path / "_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    from cli.config import get_sync_state, save_sync_state

    save_sync_state(
        {
            "tables": {"tbl1": {"hash": "abc", "rows": 0, "size_bytes": 0}},
            "last_sync": "2026-01-01T00:00:00+00:00",
        }
    )
    ws = tmp_path / "ws"

    migrated = get_sync_state(workspace=ws)
    assert migrated["tables"]["tbl1"]["hash"] == "abc"

    # Persist (what a real caller does next) and confirm the legacy file is
    # untouched.
    save_sync_state(migrated, workspace=ws)
    legacy_file = cfg_dir / "sync_state.json"
    assert legacy_file.exists()
    assert json.loads(legacy_file.read_text())["tables"]["tbl1"]["hash"] == "abc"


def test_migration_does_not_reapply_once_scoped_state_exists(tmp_path, monkeypatch):
    """Once a workspace has its own scoped state, a later write to the
    legacy file must NOT retroactively overwrite it — the two have fully
    diverged."""
    cfg_dir = tmp_path / "_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    from cli.config import get_sync_state, save_sync_state

    ws = tmp_path / "ws"
    save_sync_state({"tables": {"tbl1": {"hash": "workspace-own"}}}, workspace=ws)
    # The legacy file changes afterwards (e.g. a second, still-unmigrated
    # workspace pulling through an older CLI).
    save_sync_state({"tables": {"tbl1": {"hash": "legacy-newer"}}})

    assert get_sync_state(workspace=ws)["tables"]["tbl1"]["hash"] == "workspace-own"


def test_missing_legacy_file_migrates_to_empty_state(tmp_path, monkeypatch):
    """A brand-new machine (no legacy file at all) — migration is a no-op,
    not an error."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    from cli.config import get_sync_state

    assert get_sync_state(workspace=tmp_path / "ws") == {}


def test_corrupt_legacy_file_migrates_to_empty_state(tmp_path, monkeypatch):
    """An unreadable legacy file must degrade to an empty seed, not crash."""
    cfg_dir = tmp_path / "_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    (cfg_dir / "sync_state.json").write_text("{not json", encoding="utf-8")
    from cli.config import get_sync_state

    assert get_sync_state(workspace=tmp_path / "ws") == {}


def test_corrupt_workspace_scoped_file_degrades_to_empty(tmp_path, monkeypatch):
    """An unreadable file that already exists at the SCOPED path (not the
    legacy one) also degrades rather than crashing `agnes status`/`agnes
    pull` — and is treated as "no scoped state" without falling back to
    the legacy file, so a corrupt scoped file cannot resurrect a stale
    legacy hash record."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    from cli.config import get_sync_state

    ws = tmp_path / "ws"
    scoped_dir = ws / ".claude" / "agnes"
    scoped_dir.mkdir(parents=True)
    (scoped_dir / "sync_state.json").write_text("{not json", encoding="utf-8")

    assert get_sync_state(workspace=ws) == {}
