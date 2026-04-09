"""Tests for instance_config loading."""
import pytest


class TestInstanceConfig:
    def test_missing_config_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
        from app.instance_config import get_instance_name
        name = get_instance_name()
        assert isinstance(name, str)
