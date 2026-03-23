"""OpenAI-compatible provider for structured JSON extraction.

Supports any OpenAI-compatible API endpoint with progressive fallback
for structured output: json_schema -> json_object -> prompt-based JSON.
"""

import json
import logging
import re
import time
from urllib.parse import urlparse

import openai

from .exceptions import (
    LLMAuthError,
    LLMFormatError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMTimeoutError,
    LLMUnsupportedError,
)

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2
BACKOFF_MULTIPLIER = 2

# Regex to strip markdown code fences and extract JSON
_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _sanitize_url(url: str) -> str:
    """Extract scheme://host from a URL for safe logging.

    Never logs path, query params, or fragments which may contain
    tokens or sensitive information.
    """
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _extract_json_from_text(text: str) -> dict:
    """Parse JSON from potentially markdown-wrapped text.

    Tries direct parsing first, then strips markdown code fences,
    then falls back to finding content between first { and last }.

    Raises:
        LLMFormatError: If no valid JSON can be extracted.
    """
    # Try direct parse first
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Try stripping markdown code fences
    fence_match = _JSON_FENCE_PATTERN.search(stripped)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Fallback: find JSON between first { and last }
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(stripped[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass

    raise LLMFormatError(f"Could not extract valid JSON from model response")


class OpenAICompatExtractor:
    """Structured JSON extractor for OpenAI-compatible APIs.

    Supports progressive fallback for structured output based on the
    configured strategy:
    - "strict": json_schema only, raises LLMUnsupportedError if not supported
    - "json": json_schema -> json_object fallback
    - "auto": json_schema -> json_object -> prompt-based JSON (default)
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        structured_output: str = "auto",
    ) -> None:
        """Initialize the OpenAI-compatible extractor.

        Args:
            api_key: API key for authentication.
            base_url: Base URL of the OpenAI-compatible API.
            model: Model identifier.
            structured_output: Fallback strategy - "strict", "json", or "auto".
        """
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._structured_output = structured_output
        self._safe_url = _sanitize_url(base_url)

    def extract_json(
        self,
        prompt: str,
        max_tokens: int,
        json_schema: dict,
        schema_name: str,
    ) -> dict:
        """Extract structured JSON using an OpenAI-compatible API.

        Attempts structured output strategies in order of preference,
        falling back as allowed by the configured strategy.

        Args:
            prompt: The extraction prompt to send to the model.
            max_tokens: Maximum tokens in the response.
            json_schema: JSON Schema that the response must conform to.
            schema_name: Human-readable name for the schema.

        Returns:
            Parsed JSON dictionary conforming to the provided schema.

        Raises:
            LLMAuthError: Invalid API key.
            LLMRateLimitError: Rate limited after all retries.
            LLMTimeoutError: Timeout/connection error after all retries.
            LLMFormatError: Response is not valid JSON.
            LLMRefusalError: Model refused to respond.
            LLMUnsupportedError: Required feature not supported and no fallback allowed.
        """
        strategies = self._get_strategies()

        for strategy in strategies:
            try:
                logger.info(
                    "OpenAI-compat extraction: url=%s, model=%s, strategy=%s, schema=%s",
                    self._safe_url, self._model, strategy, schema_name,
                )
                return self._extract_with_strategy(
                    prompt, max_tokens, json_schema, schema_name, strategy,
                )
            except LLMUnsupportedError:
                logger.info(
                    "Strategy %s not supported at %s, trying next fallback",
                    strategy, self._safe_url,
                )
                continue

        raise LLMUnsupportedError(
            f"No supported structured output strategy for {self._safe_url} "
            f"with configured mode '{self._structured_output}'"
        )

    def _get_strategies(self) -> list[str]:
        """Get ordered list of strategies to try based on configuration."""
        if self._structured_output == "strict":
            return ["json_schema"]
        elif self._structured_output == "json":
            return ["json_schema", "json_object"]
        else:  # "auto"
            return ["json_schema", "json_object", "text"]

    def _extract_with_strategy(
        self,
        prompt: str,
        max_tokens: int,
        json_schema: dict,
        schema_name: str,
        strategy: str,
    ) -> dict:
        """Execute extraction with a specific structured output strategy."""
        last_exception: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._attempt_extraction(
                    prompt, max_tokens, json_schema, schema_name,
                    strategy, attempt,
                )
            except LLMAuthError:
                raise
            except LLMRefusalError:
                raise
            except LLMUnsupportedError:
                raise
            except (LLMRateLimitError, LLMTimeoutError) as e:
                last_exception = e
                if attempt < MAX_RETRIES:
                    delay = INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1))
                    logger.warning(
                        "Transient error on attempt %d/%d for %s model %s, "
                        "retrying in %ds: %s",
                        attempt, MAX_RETRIES, self._safe_url,
                        self._model, delay, type(e).__name__,
                    )
                    time.sleep(delay)

        raise last_exception  # type: ignore[misc]

    def _attempt_extraction(
        self,
        prompt: str,
        max_tokens: int,
        json_schema: dict,
        schema_name: str,
        strategy: str,
        attempt: int,
    ) -> dict:
        """Single extraction attempt with a specific strategy."""
        logger.info(
            "OpenAI-compat attempt %d/%d, url=%s, model=%s, strategy=%s",
            attempt, MAX_RETRIES, self._safe_url, self._model, strategy,
        )

        messages = [{"role": "user", "content": prompt}]
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": messages,
        }

        if strategy == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": json_schema,
                },
            }
        elif strategy == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        elif strategy == "text":
            # Append JSON instruction to prompt for text-based fallback
            messages = [
                {
                    "role": "user",
                    "content": prompt + "\n\nIMPORTANT: Respond with valid JSON only, no markdown.",
                },
            ]
            kwargs["messages"] = messages

        try:
            response = self._client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as e:
            raise LLMAuthError(
                f"OpenAI-compat authentication failed at {self._safe_url} (check API key)"
            ) from e
        except openai.RateLimitError as e:
            raise LLMRateLimitError(
                f"OpenAI-compat rate limited at {self._safe_url}"
            ) from e
        except (openai.APITimeoutError, openai.APIConnectionError) as e:
            raise LLMTimeoutError(
                f"OpenAI-compat connection error at {self._safe_url} ({type(e).__name__})"
            ) from e
        except openai.BadRequestError as e:
            # json_schema format not supported by this endpoint
            error_msg = str(e).lower()
            if "response_format" in error_msg or "json_schema" in error_msg:
                raise LLMUnsupportedError(
                    f"Structured output strategy '{strategy}' not supported "
                    f"at {self._safe_url}"
                ) from e
            raise LLMFormatError(
                f"Bad request at {self._safe_url} ({type(e).__name__})"
            ) from e

        choice = response.choices[0]

        # Check for truncation - raise and let outer retry loop handle it
        if choice.finish_reason == "length":
            raise LLMFormatError(
                f"Response truncated (max_tokens) for schema {schema_name} "
                f"at {self._safe_url}"
            )

        # Check for refusal
        content = choice.message.content
        if not content:
            raise LLMRefusalError(
                f"Model at {self._safe_url} refused to generate response "
                f"for schema {schema_name}"
            )

        # Parse JSON from response
        if strategy == "text":
            return _extract_json_from_text(content)

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMFormatError(
                f"Failed to parse response as JSON for schema {schema_name} "
                f"at {self._safe_url} ({type(e).__name__})"
            ) from e
