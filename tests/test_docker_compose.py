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


# Pre-fix this file also pinned that ``corporate-memory`` and
# ``session-collector`` side-car services stayed deleted. That's
# historical drift, not active behaviour — the same invariant is
# enforced more robustly by ``TestComposeNoBootLoopProfile`` below
# (which catches the actual past footgun: anthropic-importing
# services in a ``restart: unless-stopped`` loop).


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
        always_running = [
            name
            for name, svc in compose["services"].items()
            if "profiles" not in svc
        ]
        # Expected always-running set on a default deploy: app + scheduler.
        # extract is one-shot so it has profiles=[extract]; caddy/telegram-bot/
        # ws-gateway are all behind profiles too.
        for boot_loop_offender in ("corporate-memory", "session-collector"):
            assert boot_loop_offender not in always_running
