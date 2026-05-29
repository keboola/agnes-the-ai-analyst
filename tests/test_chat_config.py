from pathlib import Path

from app.chat.config import ChatConfig, load_chat_config


def test_default_disabled(tmp_path: Path):
    yaml = tmp_path / "instance.yaml"
    yaml.write_text("instance_name: test\n")
    cfg = load_chat_config(yaml)
    assert cfg.enabled is False
    assert cfg.provider == "e2b"
    assert cfg.concurrency_per_user == 3
    assert cfg.idle_ttl_seconds == 1800
    assert cfg.per_tool_call_seconds == 90
    assert cfg.per_session_bq_scan_bytes == 20 * 1024**3
    assert cfg.daily_anthropic_spend_usd == 20.0
    assert cfg.e2b_template_id is None
    assert cfg.e2b_workspace_max_bytes == 100 * 1024 * 1024
    assert cfg.e2b_kill_on_ws_disconnect is True


def test_enabled_with_overrides(tmp_path: Path):
    yaml = tmp_path / "instance.yaml"
    yaml.write_text(
        "instance_name: test\n"
        "chat:\n"
        "  enabled: true\n"
        "  provider: e2b\n"
        "  e2b_template_id: agnes-chat\n"
        "  e2b_workspace_max_bytes: 52428800\n"
        "  e2b_kill_on_ws_disconnect: false\n"
        "  concurrency_per_user: 5\n"
        "  idle_ttl_seconds: 900\n"
    )
    cfg = load_chat_config(yaml)
    assert cfg.enabled is True
    assert cfg.provider == "e2b"
    assert cfg.e2b_template_id == "agnes-chat"
    assert cfg.e2b_workspace_max_bytes == 52428800
    assert cfg.e2b_kill_on_ws_disconnect is False
    assert cfg.concurrency_per_user == 5
    assert cfg.idle_ttl_seconds == 900


def test_legacy_sandbox_uid_knob_is_dropped(tmp_path: Path):
    """The deprecated sandbox_uid / require_isolation keys are silently
    ignored — the ChatConfig dataclass no longer exposes them and the
    loader doesn't trip on their presence in older instance.yaml files."""
    yaml = tmp_path / "instance.yaml"
    yaml.write_text(
        "chat:\n"
        "  enabled: true\n"
        "  e2b_template_id: agnes-chat\n"
        "  require_isolation: true\n"
        "  sandbox_uid: 1500\n"
    )
    cfg = load_chat_config(yaml)
    assert cfg.enabled is True
    assert not hasattr(cfg, "require_isolation")
    assert not hasattr(cfg, "sandbox_uid")
