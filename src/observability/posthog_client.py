"""PostHog client wrapper — env-gated, lazy, no-op when disabled.

The integration is **off by default**. It activates only when the
``POSTHOG_API_KEY`` environment variable holds a non-empty PostHog
project key (the publishable ``phc_…`` key, never a personal API key).

When disabled, every public method is a cheap no-op and the underlying
``posthog`` package's ``Posthog(...)`` client is never instantiated, so
no background flush thread starts and no network calls are made.

Configuration (environment variables):

    POSTHOG_API_KEY            phc_… project key. Unset = integration off.
    POSTHOG_HOST               default ``https://eu.i.posthog.com``.
    POSTHOG_IDENTIFY_PII       ``none`` | ``id`` | ``email`` | ``full``
                               (default ``email``).
    POSTHOG_REPLAY             ``true`` (default) | ``false`` — gates the
                               JS-side ``session_recording`` opt-in.
    POSTHOG_LLM_PAYLOADS       ``1`` ships prompt/completion bodies inside
                               ``$ai_generation`` events. Default off — the
                               LLM-tracing helper still emits the event with
                               token counts and latency.
    POSTHOG_REPLAY_MASK_SELECTOR
                               extra CSS selector appended to the default
                               replay mask list.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)


_VALID_IDENTIFY_MODES = ("none", "id", "email", "full")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_environment() -> str:
    """Pick the environment label attached to every captured event.

    Resolution order:
        1. ``POSTHOG_ENVIRONMENT`` — explicit operator override.
        2. ``local`` when ``LOCAL_DEV_MODE`` is on (dev laptops).
        3. ``RELEASE_CHANNEL`` (the existing channel marker — typically
           ``stable`` for production tags, ``dev`` for branch builds).
        4. ``AGNES_DEPLOYMENT_ENV`` (free-form sister variable some
           operator playbooks set).
        5. ``unknown`` — final fallback so a missing label never silently
           pollutes the production view.
    """
    explicit = os.environ.get("POSTHOG_ENVIRONMENT", "").strip()
    if explicit:
        return explicit
    if os.environ.get("LOCAL_DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
        return "local"
    channel = os.environ.get("RELEASE_CHANNEL", "").strip()
    if channel:
        return channel
    deployment = os.environ.get("AGNES_DEPLOYMENT_ENV", "").strip()
    if deployment:
        return deployment
    return "unknown"


class PosthogClient:
    """Single-process PostHog client.

    Construct via :func:`get_posthog`; do not instantiate directly outside
    of tests.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("POSTHOG_API_KEY", "").strip()
        self._enabled = bool(api_key)
        self._api_key = api_key
        self._host = os.environ.get("POSTHOG_HOST", "https://eu.i.posthog.com").strip() or "https://eu.i.posthog.com"

        identify_mode = os.environ.get("POSTHOG_IDENTIFY_PII", "email").strip().lower()
        if identify_mode not in _VALID_IDENTIFY_MODES:
            logger.warning(
                "POSTHOG_IDENTIFY_PII=%r is invalid; falling back to 'email'. "
                "Valid: %s.",
                identify_mode,
                ", ".join(_VALID_IDENTIFY_MODES),
            )
            identify_mode = "email"
        self._identify_mode = identify_mode

        self._replay_enabled = _bool_env("POSTHOG_REPLAY", True)
        self._llm_payloads_enabled = _bool_env("POSTHOG_LLM_PAYLOADS", False)
        self._replay_extra_mask = os.environ.get("POSTHOG_REPLAY_MASK_SELECTOR", "").strip()
        self._environment = _resolve_environment()
        self._release = os.environ.get("AGNES_VERSION", "").strip() or os.environ.get("RELEASE_CHANNEL", "").strip() or None

        self._client: Any = None

        if not self._enabled:
            return

        try:
            from posthog import Posthog
        except ImportError:  # pragma: no cover — posthog is in base deps
            logger.warning("POSTHOG_API_KEY is set but the `posthog` package is not installed; disabling integration.")
            self._enabled = False
            return

        super_props: dict = {"environment": self._environment}
        if self._release:
            super_props["release"] = self._release

        try:
            self._client = Posthog(
                project_api_key=api_key,
                host=self._host,
                feature_flags_request_timeout_seconds=2,
                super_properties=super_props,
            )
        except Exception:
            logger.exception("PostHog client init failed; disabling integration.")
            self._enabled = False
            self._client = None

    # ----- introspection -----

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def host(self) -> str:
        return self._host

    @property
    def api_key_public(self) -> str:
        """The project key. Safe to embed in browser-served HTML."""
        return self._api_key

    @property
    def identify_mode(self) -> str:
        return self._identify_mode

    @property
    def replay_enabled(self) -> bool:
        return self._replay_enabled

    @property
    def llm_payloads_enabled(self) -> bool:
        return self._llm_payloads_enabled

    @property
    def replay_mask_selector_extra(self) -> str:
        return self._replay_extra_mask

    @property
    def environment(self) -> str:
        return self._environment

    @property
    def release(self) -> str | None:
        return self._release

    # ----- capture API -----

    def capture(self, event: str, distinct_id: str, properties: dict | None = None) -> None:
        if not self._enabled or self._client is None:
            return
        try:
            self._client.capture(
                distinct_id=distinct_id,
                event=event,
                properties=properties or {},
            )
        except Exception:
            logger.exception("PostHog capture failed (event=%s)", event)

    def capture_exception(
        self,
        exc: BaseException,
        distinct_id: str | None = None,
        request: Any = None,
        properties: dict | None = None,
    ) -> None:
        if not self._enabled or self._client is None:
            return
        props: dict = dict(properties or {})
        if request is not None:
            try:
                props.setdefault("path", str(request.url.path))
                props.setdefault("method", str(request.method))
            except Exception:
                pass
            if distinct_id is None:
                distinct_id = self._distinct_id_from_request(request)

        if distinct_id is None:
            distinct_id = "anonymous"

        try:
            # PostHog SDK ≥ 3.7 exposes ``capture_exception``. Older
            # builds don't — fall back to a manual ``$exception`` event.
            cap = getattr(self._client, "capture_exception", None)
            if callable(cap):
                cap(exc, distinct_id=distinct_id, properties=props)
            else:  # pragma: no cover — only triggered on old SDK
                self._client.capture(
                    distinct_id=distinct_id,
                    event="$exception",
                    properties={
                        **props,
                        "$exception_type": type(exc).__name__,
                        "$exception_message": str(exc),
                    },
                )
        except Exception:
            logger.exception("PostHog capture_exception failed")

    # ----- feature flags -----

    def is_feature_enabled(self, key: str, distinct_id: str, default: bool = False) -> bool:
        if not self._enabled or self._client is None:
            return default
        # PostHog SDK 3.x exposed ``is_feature_enabled``; 7.x renamed it to
        # ``feature_enabled``. Try the new name first, fall back to the old.
        method = getattr(self._client, "feature_enabled", None) or getattr(
            self._client, "is_feature_enabled", None
        )
        if method is None:
            return default
        try:
            value = method(key, distinct_id)
            return bool(value) if value is not None else default
        except Exception:
            logger.exception("PostHog feature_enabled failed (key=%s)", key)
            return default

    def get_feature_flag_payload(self, key: str, distinct_id: str) -> Any:
        if not self._enabled or self._client is None:
            return None
        try:
            return self._client.get_feature_flag_payload(key, distinct_id)
        except Exception:
            logger.exception("PostHog get_feature_flag_payload failed (key=%s)", key)
            return None

    # ----- lifecycle -----

    def shutdown(self) -> None:
        if not self._enabled or self._client is None:
            return
        try:
            self._client.shutdown()
        except Exception:
            logger.exception("PostHog shutdown failed")

    # ----- helpers -----

    @staticmethod
    def _distinct_id_from_request(request: Any) -> str | None:
        try:
            user = getattr(request.state, "user", None)
        except Exception:
            user = None
        if user is None:
            return None
        for attr in ("id", "user_id", "email"):
            value = getattr(user, attr, None)
            if value:
                return str(value)
        if isinstance(user, dict):
            for key in ("id", "user_id", "email"):
                if user.get(key):
                    return str(user[key])
        return None


_singleton_lock = threading.Lock()
_singleton: PosthogClient | None = None


def get_posthog() -> PosthogClient:
    """Return the process-wide :class:`PosthogClient`, constructing on first call."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PosthogClient()
    return _singleton


def reset_posthog() -> None:
    """Drop the singleton (test hook only)."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            try:
                _singleton.shutdown()
            except Exception:
                pass
        _singleton = None
