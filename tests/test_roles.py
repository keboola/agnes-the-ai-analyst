import pytest

from app.roles import Role, active_roles, is_all_in_one, reset_roles_cache, role_enabled


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AGNES_ROLE", raising=False)
    reset_roles_cache()
    yield
    reset_roles_cache()


def test_default_is_all_roles():
    assert active_roles() == frozenset({Role.API, Role.GATEWAY, Role.WORKER})
    assert is_all_in_one() is True
    assert role_enabled(Role.API) and role_enabled(Role.WORKER)


def test_env_single_role(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    assert active_roles() == frozenset({Role.API})
    assert is_all_in_one() is False
    assert role_enabled(Role.GATEWAY) is False


def test_env_comma_list_and_whitespace(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", " api, worker ")
    reset_roles_cache()
    assert active_roles() == frozenset({Role.API, Role.WORKER})


def test_all_token(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "all")
    reset_roles_cache()
    assert is_all_in_one() is True


def test_unknown_token_raises(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "apii")
    reset_roles_cache()
    with pytest.raises(ValueError, match="apii"):
        active_roles()


def test_instance_yaml_fallback(monkeypatch):
    monkeypatch.setattr("app.roles._config_role", lambda: "worker")
    reset_roles_cache()
    assert active_roles() == frozenset({Role.WORKER})
