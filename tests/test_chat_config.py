from pathlib import Path

from app.chat.config import ChatConfig, load_chat_config


def test_default_disabled(tmp_path: Path):
    yaml = tmp_path / "instance.yaml"
    yaml.write_text("instance_name: test\n")
    cfg = load_chat_config(yaml)
    assert cfg.enabled is False
    assert cfg.require_isolation is True
    assert cfg.concurrency_per_user == 3
    assert cfg.idle_ttl_seconds == 1800
    assert cfg.per_tool_call_seconds == 90
    assert cfg.per_session_bq_scan_bytes == 20 * 1024**3
    assert cfg.daily_anthropic_spend_usd == 20.0


def test_enabled_with_overrides(tmp_path: Path):
    yaml = tmp_path / "instance.yaml"
    yaml.write_text(
        "instance_name: test\n"
        "chat:\n"
        "  enabled: true\n"
        "  require_isolation: false\n"
        "  concurrency_per_user: 5\n"
        "  idle_ttl_seconds: 900\n"
    )
    cfg = load_chat_config(yaml)
    assert cfg.enabled is True
    assert cfg.require_isolation is False
    assert cfg.concurrency_per_user == 5
    assert cfg.idle_ttl_seconds == 900
