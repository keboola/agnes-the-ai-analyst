"""AgentHarness seam (app/chat/harness.py + runner registry + boot gate).

Explicit-invalid refuses at boot (_chat_harness_ok); inherited-invalid
degrades to the default in the runner (_select_harness). claude-code is
the only registered production harness.
"""

from app.chat.config import ChatConfig
from app.chat.harness import APPROVED_HARNESSES, DEFAULT_HARNESS, AgentHarnessLoop
from app.chat.runner import _real_agent_loop, _select_harness
from app.main import _chat_harness_ok


def test_default_harness_is_approved():
    assert DEFAULT_HARNESS in APPROVED_HARNESSES


def test_registry_resolves_claude_code_and_default():
    assert _select_harness("claude-code") is _real_agent_loop
    assert _select_harness(None) is _real_agent_loop
    assert _select_harness("") is _real_agent_loop


def test_unknown_inherited_id_degrades_to_default(capsys):
    assert _select_harness("space-modulator-9000") is _real_agent_loop
    assert "space-modulator-9000" in capsys.readouterr().err


def test_real_loop_satisfies_harness_protocol():
    assert isinstance(_real_agent_loop, AgentHarnessLoop)


def test_boot_gate_accepts_approved_and_disabled():
    assert _chat_harness_ok(ChatConfig(enabled=True, harness="claude-code")) is True
    # disabled chat never blocks boot on harness config
    assert _chat_harness_ok(ChatConfig(enabled=False, harness="whatever")) is True


def test_boot_gate_refuses_unknown_explicit_harness():
    assert _chat_harness_ok(ChatConfig(enabled=True, harness="not-a-harness")) is False


def test_config_parses_harness_key(tmp_path):
    from app.chat.config import load_chat_config

    cfg_file = tmp_path / "instance.yaml"
    cfg_file.write_text("chat:\n  enabled: true\n  harness: claude-code\n")
    cfg = load_chat_config(cfg_file)
    assert cfg.harness == "claude-code"
    cfg_file.write_text("chat:\n  enabled: true\n")
    assert load_chat_config(cfg_file).harness == "claude-code"
