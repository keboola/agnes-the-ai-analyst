"""Role gates: api-only process must not own chat/warmup; all-mode unchanged."""

import pytest

from app.roles import Role, reset_roles_cache, role_enabled


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("AGNES_ROLE", raising=False)
    reset_roles_cache()
    yield
    reset_roles_cache()


def test_gateway_gate_helper(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    assert role_enabled(Role.GATEWAY) is False


def test_main_uses_role_gates():
    # Structural guard: the four lifecycle sites in app/main.py must consult
    # role_enabled — cheap regression net until the E2E harness (Task 7)
    # exercises real processes.
    src = open("app/main.py").read()
    assert src.count("role_enabled(Role.GATEWAY)") >= 2, "chat + slack socket must be gateway-gated"
    assert src.count("role_enabled(Role.WORKER)") >= 2, "warmup + rebuild-on-boot must be worker-gated"
