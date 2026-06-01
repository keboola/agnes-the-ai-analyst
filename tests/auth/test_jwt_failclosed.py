import importlib
import pytest


def _fresh_jwt(monkeypatch, *, testing=None, jwt_key=None, local_dev=False):
    """Re-import app.auth.jwt with a controlled environment + dev-mode flag."""
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    if testing is not None:
        monkeypatch.setenv("TESTING", testing)
    if jwt_key is not None:
        monkeypatch.setenv("JWT_SECRET_KEY", jwt_key)
    import app.auth.dependencies as deps
    monkeypatch.setattr(deps, "is_local_dev_mode", lambda: local_dev)
    jwtmod = importlib.import_module("app.auth.jwt")
    jwtmod._SECRET_KEY_CACHE = None
    return jwtmod


def test_production_missing_key_raises(monkeypatch):
    jwtmod = _fresh_jwt(monkeypatch, local_dev=False)
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY is required"):
        jwtmod.validate_jwt_secret_or_raise()


def test_production_short_key_raises(monkeypatch):
    jwtmod = _fresh_jwt(monkeypatch, jwt_key="too-short", local_dev=False)
    with pytest.raises(RuntimeError, match="too short"):
        jwtmod.validate_jwt_secret_or_raise()


def test_production_strong_key_ok(monkeypatch):
    jwtmod = _fresh_jwt(monkeypatch, jwt_key="x" * 32, local_dev=False)
    jwtmod.validate_jwt_secret_or_raise()  # no raise
    assert jwtmod._get_cached_secret_key() == "x" * 32


def test_local_dev_missing_key_allowed(monkeypatch, tmp_path):
    # `validate_jwt_secret_or_raise()` under local-dev triggers
    # `app.secrets._load_or_generate()`, which writes the freshly minted
    # key to ``${DATA_DIR}/state/.jwt_secret`` for reuse. Point DATA_DIR
    # at this test's tmp_path so the auto-generated file is per-test
    # isolated — otherwise pytest-xdist workers can race on the shared
    # conftest tempdir and a later test reads a stale or mid-write file
    # (Devin Review on #483).
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    jwtmod = _fresh_jwt(monkeypatch, local_dev=True)
    jwtmod.validate_jwt_secret_or_raise()  # auto-generate path, no raise
