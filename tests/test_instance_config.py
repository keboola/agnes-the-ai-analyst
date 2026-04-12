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

    def test_reads_nested_instance_name(self, tmp_path, monkeypatch):
        """get_instance_name should read instance.name from YAML, not flat instance_name."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")

        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(
            "instance:\n  name: Acme Analytics\n  subtitle: Data Team\n"
        )

        import importlib
        import app.instance_config as mod
        # Reset cached config to force reload
        mod._instance_config = None
        importlib.reload(mod)

        assert mod.get_instance_name() == "Acme Analytics"
        assert mod.get_instance_subtitle() == "Data Team"

        # Cleanup: reset cache after test
        mod._instance_config = None
