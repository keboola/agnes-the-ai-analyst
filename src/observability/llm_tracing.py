"""LLM call instrumentation that emits PostHog ``$ai_generation`` events.

PostHog's LLM Observability product consumes events with a documented
property schema (``$ai_provider``, ``$ai_model``, ``$ai_input_tokens``,
``$ai_output_tokens``, ``$ai_latency``, ``$ai_trace_id``, ``$ai_input``,
``$ai_output_choices``, ``$ai_is_error``).

Use the :func:`trace_generation` context manager around the synchronous
provider call. The capture object lets the caller record token counts
and (when ``POSTHOG_LLM_PAYLOADS=1``) prompt/completion content.

Example::

    from src.observability import trace_generation

    with trace_generation(provider="anthropic", model="claude-opus-4") as cap:
        cap.set_input(prompt)
        response = client.messages.create(...)
        cap.set_output_from_anthropic(response)
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from src.observability.posthog_client import get_posthog

logger = logging.getLogger(__name__)


class _Capture:
    """Per-call mutable bag the wrapped code uses to report token counts.

    All setters are best-effort — exceptions are swallowed so a tracing
    bug never breaks the LLM call itself.
    """

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.prompt: Any = None
        self.output: Any = None
        self.extra: dict[str, Any] = {}

    def set_input(self, prompt: Any) -> None:
        try:
            self.prompt = prompt
        except Exception:
            pass

    def set_output(self, output: Any) -> None:
        try:
            self.output = output
        except Exception:
            pass

    def set_tokens(self, input_tokens: int | None, output_tokens: int | None) -> None:
        try:
            self.input_tokens = input_tokens
            self.output_tokens = output_tokens
        except Exception:
            pass

    def set_output_from_anthropic(self, response: Any) -> None:
        """Pull token counts + completion text from an Anthropic response.

        Tolerates SDK version differences and partial responses.
        """
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.input_tokens = getattr(usage, "input_tokens", None)
                self.output_tokens = getattr(usage, "output_tokens", None)
            content = getattr(response, "content", None)
            if content is not None:
                # Anthropic returns a list of blocks; capture text payloads.
                texts: list[str] = []
                for block in content:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        texts.append(text)
                if texts:
                    self.output = texts if len(texts) > 1 else texts[0]
        except Exception:
            logger.debug("set_output_from_anthropic: extraction failed", exc_info=True)

    def set_output_from_openai(self, response: Any) -> None:
        """Pull token counts + completion text from an OpenAI-compat response."""
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.input_tokens = getattr(usage, "prompt_tokens", None)
                self.output_tokens = getattr(usage, "completion_tokens", None)
            choices = getattr(response, "choices", None)
            if choices:
                first = choices[0]
                msg = getattr(first, "message", None)
                if msg is not None:
                    self.output = getattr(msg, "content", None)
        except Exception:
            logger.debug("set_output_from_openai: extraction failed", exc_info=True)


class _Noop:
    """Same surface as :class:`_Capture`; all setters drop on the floor."""

    def set_input(self, prompt: Any) -> None: ...
    def set_output(self, output: Any) -> None: ...
    def set_tokens(self, input_tokens: int | None, output_tokens: int | None) -> None: ...
    def set_output_from_anthropic(self, response: Any) -> None: ...
    def set_output_from_openai(self, response: Any) -> None: ...

    extra: dict[str, Any] = {}


@contextmanager
def trace_generation(
    provider: str,
    model: str,
    distinct_id: str | None = None,
    parent_trace_id: str | None = None,
) -> Iterator[Any]:
    """Wrap an LLM call and emit one ``$ai_generation`` event on exit.

    Yields a capture object; the caller fills it in from the response.
    Disabled state yields a :class:`_Noop`. Exceptions in the wrapped
    block are re-raised after emitting an error variant of the event.
    """
    pc = get_posthog()
    if not pc.enabled:
        yield _Noop()
        return

    cap = _Capture()
    trace_id = parent_trace_id or uuid.uuid4().hex
    started = time.perf_counter()
    error: BaseException | None = None
    try:
        yield cap
    except BaseException as exc:
        error = exc
        raise
    finally:
        latency_s = time.perf_counter() - started
        props: dict[str, Any] = {
            "$ai_provider": provider,
            "$ai_model": model,
            "$ai_trace_id": trace_id,
            "$ai_latency": latency_s,
        }
        if cap.input_tokens is not None:
            props["$ai_input_tokens"] = cap.input_tokens
        if cap.output_tokens is not None:
            props["$ai_output_tokens"] = cap.output_tokens
        if pc.llm_payloads_enabled:
            if cap.prompt is not None:
                props["$ai_input"] = cap.prompt
            if cap.output is not None:
                props["$ai_output_choices"] = cap.output
        for key, value in cap.extra.items():
            props.setdefault(key, value)
        if error is not None:
            props["$ai_is_error"] = True
            props["$ai_error"] = repr(error)

        pc.capture("$ai_generation", distinct_id or "system", props)
