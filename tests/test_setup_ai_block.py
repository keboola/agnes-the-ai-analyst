"""Tests for /api/admin/configure writing a default ai: block.

First-time setup must seed an ai: section in the instance.yaml overlay so
LLM-driven services (corporate_memory, verification_detector) can boot
without a manual edit. Closes one of five defects in #176.
"""

from __future__ import annotations

import yaml
from unittest.mock import patch


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _read_overlay(env: dict) -> dict:
    overlay_path = env["data_dir"] / "state" / "instance.yaml"
    if not overlay_path.exists():
        return {}
    return yaml.safe_load(overlay_path.read_text()) or {}


class TestConfigureSeedsAiBlock:
    def test_configure_seeds_ai_block_when_anthropic_api_key_is_set(self, seeded_app, monkeypatch):
        """ANTHROPIC_API_KEY in the env → overlay gets an ai: block referencing it."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-keyvalue")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        overlay = _read_overlay(seeded_app["env"])
        assert "ai" in overlay, "configure must seed ai: block when ANTHROPIC_API_KEY is set"
        ai = overlay["ai"]
        assert ai.get("provider") == "anthropic"
        # The overlay stores the env-var reference (${ANTHROPIC_API_KEY}), not
        # the raw secret — secrets belong in .env_overlay only.
        assert ai.get("api_key") == "${ANTHROPIC_API_KEY}"
        assert "model" in ai

    def test_configure_seeds_ai_block_when_llm_api_key_is_set(self, seeded_app, monkeypatch):
        """LLM_API_KEY (proxy/openai_compat fallback) is also acceptable."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("LLM_API_KEY", "sk-proxy-keyvalue")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        overlay = _read_overlay(seeded_app["env"])
        assert "ai" in overlay
        # The fallback uses ${LLM_API_KEY} — same env-var-reference pattern.
        assert overlay["ai"].get("api_key") == "${LLM_API_KEY}"

    def test_configure_does_not_seed_ai_when_no_key_in_env(self, seeded_app, monkeypatch):
        """No env keys → no ai block written. Operator must add manually.

        We deliberately do not write a placeholder block: the LLM services
        fail-fast on a missing block and the operator gets a clear error.
        """
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        overlay = _read_overlay(seeded_app["env"])
        assert "ai" not in overlay

    def test_configure_preserves_existing_ai_block(self, seeded_app, monkeypatch):
        """If overlay already has ai: section, configure must not overwrite it."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        # Pre-populate the overlay with a custom ai block.
        overlay_path = seeded_app["env"]["data_dir"] / "state" / "instance.yaml"
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text(yaml.dump({
            "ai": {
                "provider": "openai_compat",
                "api_key": "${LLM_API_KEY}",
                "base_url": "https://litellm.example.com",
                "model": "claude-haiku-4-5-20251001",
            }
        }))

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        overlay = _read_overlay(seeded_app["env"])
        assert overlay["ai"]["provider"] == "openai_compat"
        assert overlay["ai"]["base_url"] == "https://litellm.example.com"
