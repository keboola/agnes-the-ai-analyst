import pytest

from app.roles import reset_roles_cache
from app.startup_guards import DeploymentConfigError, validate_deployment


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("AGNES_ROLE", "UVICORN_WORKERS", "JWT_SECRET_KEY", "SESSION_SECRET", "AGNES_COORDINATION_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    reset_roles_cache()
    yield
    reset_roles_cache()


def test_all_in_one_passes_with_no_config():
    validate_deployment()  # must not raise — spec §5.4.1 default unchanged


def test_split_role_without_pg_refuses(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    with pytest.raises(DeploymentConfigError, match="Postgres"):
        validate_deployment()


def test_multi_worker_is_multi_process(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    with pytest.raises(DeploymentConfigError):
        validate_deployment()


def test_split_role_names_missing_secrets(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    with pytest.raises(DeploymentConfigError) as exc:
        validate_deployment()
    assert "JWT_SECRET_KEY" in str(exc.value)
    assert "SESSION_SECRET" in str(exc.value)
    assert "docs/DEPLOYMENT.md#multi-process" in str(exc.value)


def test_split_role_requires_redis_coordination(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "memory")
    with pytest.raises(DeploymentConfigError, match="coordination"):
        validate_deployment()


def test_split_role_fully_configured_passes(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    validate_deployment()


def test_redis_coordination_alone_triggers_multi_process(monkeypatch):
    """coordination.backend=redis is itself multi-process intent, even in an
    otherwise all-in-one topology (no AGNES_ROLE split, single worker) —
    closes the wave-1 deferred finding."""
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    with pytest.raises(DeploymentConfigError, match="Postgres"):
        validate_deployment()


def test_redis_coordination_alone_fully_configured_passes(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    validate_deployment()  # all-in-one + redis coordination + PG + secrets — OK


def test_memory_coordination_all_in_one_stays_single_process(monkeypatch):
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "memory")
    validate_deployment()  # must not raise — unchanged default topology


def test_coordination_backend_env_override_wins_over_yaml(monkeypatch):
    """``_coordination_backend`` must honor ``AGNES_COORDINATION_BACKEND`` —
    the same env-overrides-yaml resolution used by
    ``app.coordination.factory``, so this guard reacts to the backend the
    process will actually use."""
    from app.startup_guards import _coordination_backend

    monkeypatch.setenv("AGNES_COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *a, **k: "memory",
    )
    assert _coordination_backend() == "redis"
