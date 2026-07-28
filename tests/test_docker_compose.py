"""Static contract tests for docker-compose.yml.

The corporate-memory and session-collector side-car services were dropped
in #176 — the scheduler container now drives them through HTTP. These
tests pin that contract so the services can't quietly come back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def compose() -> dict:
    root = Path(__file__).resolve().parent.parent
    return yaml.safe_load((root / "docker-compose.yml").read_text())


class TestComposeServicesRemoved:
    """The two side-car services must not exist in docker-compose.yml."""

    def test_corporate_memory_service_removed(self, compose):
        assert "corporate-memory" not in compose["services"], (
            "corporate-memory was dropped in #176 — scheduler drives it via HTTP. Do not re-add the service stanza."
        )

    def test_session_collector_service_removed(self, compose):
        assert "session-collector" not in compose["services"], (
            "session-collector was dropped in #176 — scheduler drives it via HTTP. Do not re-add the service stanza."
        )


class TestComposeSchedulerWires:
    """The scheduler service must remain — it's the sole driver now."""

    def test_scheduler_service_present(self, compose):
        assert "scheduler" in compose["services"]
        scheduler = compose["services"]["scheduler"]
        assert scheduler["command"] == "python -m services.scheduler"

    def test_app_service_present(self, compose):
        assert "app" in compose["services"]


class TestComposeNoBootLoopProfile:
    """No service that imports anthropic / openai should ship as a tight
    `restart: unless-stopped` boot loop. The previous corporate-memory and
    session-collector stanzas were exactly this footgun."""

    def test_only_scheduler_is_unconditional_long_running(self, compose):
        # Services WITHOUT a `profiles:` key run on default `docker compose up`.
        always_running = [name for name, svc in compose["services"].items() if "profiles" not in svc]
        # Expected always-running set on a default deploy: app + scheduler.
        # extract is one-shot so it has profiles=[extract]; caddy/telegram-bot
        # are behind profiles too. ws-gateway was removed (wave-2F task 6) —
        # its notifications now ride the coordination pub/sub channel served
        # by the GATEWAY-role app process, see app/api/notifications_ws.py.
        for boot_loop_offender in ("corporate-memory", "session-collector"):
            assert boot_loop_offender not in always_running
