"""Optional observability layer (PostHog).

Disabled by default. Enabled when ``POSTHOG_API_KEY`` is set in the
environment. See ``docs/observability.md`` for the operator guide.
"""

from src.observability.posthog_client import get_posthog, reset_posthog
from src.observability.llm_tracing import trace_generation

__all__ = ["get_posthog", "reset_posthog", "trace_generation"]
