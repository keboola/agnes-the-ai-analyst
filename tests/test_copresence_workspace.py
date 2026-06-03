from pathlib import Path


def _mgr(tmp_path):
    from app.chat.workdir import WorkdirManager

    class _StubRepo:
        def get_workdir_row(self, *a, **k):
            return None
        def upsert_workdir(self, *a, **k):
            return None
        def get_workdir(self, *a, **k):
            return None

    bundled = tmp_path / "bundled"
    (bundled / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=_StubRepo(),
        bundled_template_dir=bundled,
        server_url="https://example.com",
        agnes_version="0.0.0-test",
        get_marketplace_sha=lambda: "sha",
        get_template_status=lambda: None,
        render_workspace_prompt=lambda email: "# CLAUDE\n",
    )


def test_ephemeral_dir_has_no_claude_local_and_only_intersection_plugins(tmp_path):
    mgr = _mgr(tmp_path)
    # seed one allowed + one disallowed plugin in the bundled template
    skills = tmp_path / "bundled" / ".claude" / "skills"
    (skills / "pluginX").mkdir(parents=True, exist_ok=True)
    (skills / "pluginX" / "SKILL.md").write_text("x", encoding="utf-8")
    (skills / "pluginY").mkdir(parents=True, exist_ok=True)
    (skills / "pluginY" / "SKILL.md").write_text("y", encoding="utf-8")
    sdir = Path(mgr.prepare_ephemeral_session_dir(
        chat_id="chat_co1",
        participant_emails=["a@example.com", "b@example.com"],
        intersection={"marketplace_plugin": frozenset({"pluginX"})},
    ))
    assert not (sdir / "CLAUDE.local.md").exists()
    for p in sdir.rglob("*"):
        if p.is_symlink():
            assert "users" not in str(p.resolve())
    assert (sdir / "CLAUDE.md").exists()
    assert (sdir / "memory").is_dir() and not any((sdir / "memory").iterdir())
    assert (sdir / "work").is_dir()
    present = {p.name for p in (sdir / ".claude" / "skills").iterdir()}
    assert present == {"pluginX"}


def test_prepare_session_dir_no_claude_local_by_default(tmp_path):
    mgr = _mgr(tmp_path)
    # Create the workspace dir structure needed
    ws = mgr.user_workspace("a@example.com")
    (ws / ".claude").mkdir(parents=True, exist_ok=True)
    (ws / "CLAUDE.md").write_text("# hi", encoding="utf-8")
    # Also create a CLAUDE.local.md in the workspace to verify it is NOT linked
    (ws / "CLAUDE.local.md").write_text("personal override", encoding="utf-8")
    sdir = Path(mgr.prepare_session_dir("a@example.com", "chat_personal"))
    assert not (sdir / "CLAUDE.local.md").exists()
