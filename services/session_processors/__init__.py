"""Pluggable session processors for the session-pipeline framework.

Each processor implements the SessionProcessor protocol from
services.session_pipeline.contract and lives in its own module here.

The PROCESSORS list + PROCESSORS_BY_NAME dict are populated lazily so that
processors needing runtime config (LLM extractor, instance config, etc.)
don't fail at import time when those aren't available — relevant for tests
and for instances where the LLM is intentionally unconfigured.
"""

from __future__ import annotations

from functools import lru_cache

from services.session_pipeline.contract import SessionProcessor
from services.session_processors.usage import UsageProcessor
from services.session_processors.verification import build_verification_processor


@lru_cache(maxsize=1)
def _build_registry() -> dict[str, SessionProcessor]:
    """Construct the registry once per process. Verification needs an LLM
    extractor which is built from instance config + env, so we delay until
    something actually asks for the registry — meaning admin endpoint or
    scheduler call, not test imports."""
    registry: dict[str, SessionProcessor] = {
        "usage": UsageProcessor(),
    }
    try:
        registry["verification"] = build_verification_processor()
    except Exception:
        # Verification needs an LLM; if construction fails (no API key,
        # bad config), the endpoint will report a clean 400 "unknown
        # processor" rather than a 500 at import time. The error is logged
        # by build_verification_processor.
        pass
    return registry


def get_processor(name: str) -> SessionProcessor | None:
    return _build_registry().get(name)


def list_processor_names() -> list[str]:
    return sorted(_build_registry().keys())
