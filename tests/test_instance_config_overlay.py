"""Regression tests for app.instance_config overlay handling (#179 review).

Two paths to a working LLM pipeline must both function:

1. Operator hand-edits config/instance.yaml — covered by config.loader's
   existing ``_resolve_env_refs`` pass.
2. Operator hits /api/admin/configure on first-time setup — that handler
   seeds an ``ai:`` block in the writable overlay at
   ``${DATA_DIR}/state/instance.yaml`` referencing ``${ANTHROPIC_API_KEY}``.

Path 2 used to be dead code: the three LLM consumers
(``services.corporate_memory.collector.collect_all``,
``app.api.admin.run_verification_detector`` and
``services.verification_detector.__main__``) imported from
``config.loader.load_instance_config`` (overlay-blind), and even if they
hadn't, ``app.instance_config.load_instance_config`` deep-merged the
overlay through raw ``yaml.safe_load`` without resolving ``${ENV_VAR}``
references. The factory then rejected the literal placeholder string as
an invalid api_key.

This file pins both fixes:
- env-ref resolution runs against the overlay before deep-merge
- the three consumers reach the overlay-aware loader
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture(autouse=True)
def _reset_instance_cache():
    """Drop the ``app.instance_config._instance_config`` cache between tests.

    Without this, a test that pollutes the cache leaks into the next one.
    The same reset endpoint that ``/api/admin/server-config`` uses after
    a save is the supported public entry point.
    """
    from app import instance_config as ic
    ic.reset_cache()
    yield
    ic.reset_cache()


def _write_overlay(data_dir: Path, payload: dict) -> Path:
    """Drop a writable overlay at the path the loader actually reads."""
    overlay_path = data_dir / "state" / "instance.yaml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(yaml.dump(payload))
    return overlay_path


class TestOverlayEnvResolution:
    """${ENV_VAR} placeholders in the overlay must resolve at load time."""

    def test_env_ref_in_overlay_resolves_when_env_set(self, tmp_path, monkeypatch):
        """Overlay's ${ANTHROPIC_API_KEY} resolves to the env value."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret123")

        _write_overlay(tmp_path, {"ai": {"api_key": "${ANTHROPIC_API_KEY}"}})

        # Block the static base loader so this test is hermetic — the only
        # signal we care about is that the overlay path resolves the ref.
        with patch("config.loader.load_instance_config", return_value={}):
            from app.instance_config import load_instance_config
            cfg = load_instance_config()

        assert cfg.get("ai", {}).get("api_key") == "secret123"

    def test_env_ref_in_overlay_left_unresolved_when_env_missing(
        self, tmp_path, monkeypatch,
    ):
        """When the env var isn't set, the placeholder collapses to empty.

        This mirrors ``_resolve_env_refs``'s contract: missing env logs a
        warning and substitutes an empty string. The LLM factory's separate
        env fallback (ANTHROPIC_API_KEY -> AnthropicExtractor) is what
        ultimately surfaces the actionable error to the operator — this
        test pins that the config layer doesn't fabricate a valid-looking
        key when the env is empty.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        _write_overlay(tmp_path, {"ai": {"api_key": "${ANTHROPIC_API_KEY}"}})

        with patch("config.loader.load_instance_config", return_value={}):
            from app.instance_config import load_instance_config
            cfg = load_instance_config()

        # Empty string, not the literal "${ANTHROPIC_API_KEY}". The factory
        # treats empty as missing and raises the documented ValueError, so
        # the eventual error message points the operator at the env, not
        # at a malformed YAML.
        assert cfg.get("ai", {}).get("api_key") == ""

    def test_overlay_deep_merges_with_static_base(self, tmp_path, monkeypatch):
        """Overlay wins per-leaf; sections only in the base still flow through."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

        _write_overlay(tmp_path, {"ai": {"api_key": "${ANTHROPIC_API_KEY}"}})

        # Static base contributes a section the overlay doesn't touch.
        static_base = {
            "instance": {"name": "Test"},
            "datasets": {"foo": {"id": 1}},
        }
        with patch("config.loader.load_instance_config", return_value=static_base):
            from app.instance_config import load_instance_config
            cfg = load_instance_config()

        assert cfg["instance"]["name"] == "Test"
        assert cfg["datasets"] == {"foo": {"id": 1}}
        assert cfg["ai"]["api_key"] == "k"


class TestConsumersUseOverlayAwareLoader:
    """The three LLM pipeline consumers must reach the overlay path."""

    def test_collector_imports_app_instance_config(self):
        """``collect_all`` imports load_instance_config from app.instance_config."""
        import inspect

        from services.corporate_memory.collector import collect_all

        src = inspect.getsource(collect_all)
        # The overlay-aware loader is the only one that merges
        # DATA_DIR/state/instance.yaml; a consumer that imports
        # config.loader.load_instance_config silently misses overlay edits.
        assert "from app.instance_config import load_instance_config" in src
        assert "from config.loader import load_instance_config" not in src

    def test_admin_run_verification_detector_uses_overlay_loader(self):
        """``run_verification_detector`` imports the overlay-aware loader."""
        import inspect

        from app.api.admin import run_verification_detector

        src = inspect.getsource(run_verification_detector)
        assert "from app.instance_config import load_instance_config" in src
        assert "from config.loader import load_instance_config" not in src

    def test_verification_detector_main_uses_overlay_loader(self):
        """The verification-detector CLI main reads through the overlay."""
        import inspect

        from services.verification_detector import __main__ as vd_main

        src = inspect.getsource(vd_main)
        assert "from app.instance_config import load_instance_config" in src
        # config.loader may legitimately appear in other contexts in this
        # module someday; keep the assertion narrow to the same statement.
        assert "from config.loader import load_instance_config" not in src


class TestSeededOverlayReachesFactory:
    """End-to-end: seeded overlay + env -> factory receives a usable api_key."""

    def test_collector_seeded_overlay_flows_through_to_factory(
        self, tmp_path, monkeypatch,
    ):
        """The seeded ai: block + ANTHROPIC_API_KEY env yields a real extractor.

        Reproduces the path /api/admin/configure produces on first-time
        setup: an overlay containing only ``ai: {api_key: ${ANTHROPIC_API_KEY}, ...}``
        plus the env var set by the operator. With the #179 review fixes,
        the factory must see the resolved key — without them, it would
        either miss the overlay entirely or get the literal placeholder.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")

        _write_overlay(tmp_path, {
            "ai": {
                "provider": "anthropic",
                "api_key": "${ANTHROPIC_API_KEY}",
                "model": "claude-haiku-4-5-20251001",
            },
        })

        # Static base empty so only the overlay path matters.
        with patch("config.loader.load_instance_config", return_value={}):
            captured = {}

            def _spy(ai_config):
                captured["ai_config"] = ai_config
                from unittest.mock import MagicMock
                return MagicMock()

            from connectors.llm import factory as llm_factory

            with patch.object(llm_factory, "create_extractor_from_env_or_config", _spy):
                # Re-import via the lazy import inside collect_all by mocking
                # the lookup at the package level (matches how collector imports).
                import connectors.llm as llm_pkg
                with patch.object(
                    llm_pkg, "create_extractor_from_env_or_config", _spy,
                ):
                    home = tmp_path / "home"
                    home.mkdir()
                    user_dir = home / "alice"
                    user_dir.mkdir()
                    (user_dir / "CLAUDE.local.md").write_text("hello")

                    from services.corporate_memory.collector import collect_all
                    with patch(
                        "services.corporate_memory.collector.HOME_BASE", home,
                    ), patch(
                        "services.corporate_memory.collector._read_json",
                        return_value={},
                    ):
                        # The factory mock returns a MagicMock extractor whose
                        # extract_json default returns a MagicMock — the catalog
                        # processing code expects a dict-shaped response. We
                        # don't care about post-extractor behavior here, only
                        # that the factory was called with the resolved overlay.
                        try:
                            collect_all(dry_run=True)
                        except Exception:
                            pass

        assert captured.get("ai_config") is not None, (
            "Factory was never called — collector did not reach the overlay loader"
        )
        assert captured["ai_config"].get("api_key") == "sk-ant-from-env", (
            "Factory received an unresolved or missing api_key — "
            "overlay env-ref resolution is broken"
        )
