"""Anthropic provider for structured JSON extraction.

Uses the Anthropic API with native structured output (json_schema)
for reliable JSON extraction. Includes retry logic for transient errors.
"""

import json
import logging
import time

import anthropic

from .exceptions import (
    LLMAuthError,
    LLMFormatError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2
BACKOFF_MULTIPLIER = 2


class AnthropicExtractor:
    """Structured JSON extractor using the Anthropic API.

    Uses output_config with json_schema format for structured output.
    Retries transient errors (rate limit, timeout, connection) with
    exponential backoff.
    """

    def __init__(self, api_key: str, model: str) -> None:
        """Initialize the Anthropic extractor.

        Args:
            api_key: Anthropic API key.
            model: Model identifier (e.g., "claude-haiku-4-5-20251001").
        """
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def extract_json(
        self,
        prompt: str,
        max_tokens: int,
        json_schema: dict,
        schema_name: str,
    ) -> dict:
        """Extract structured JSON using the Anthropic API.

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
        """
        last_exception: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._attempt_extraction(
                    prompt, max_tokens, json_schema, schema_name, attempt,
                )
            except LLMAuthError:
                raise
            except LLMRefusalError:
                raise
            except (LLMRateLimitError, LLMTimeoutError) as e:
                last_exception = e
                if attempt < MAX_RETRIES:
                    delay = INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1))
                    logger.warning(
                        "Transient error on attempt %d/%d for model %s, "
                        "retrying in %ds: %s",
                        attempt, MAX_RETRIES, self._model, delay,
                        type(e).__name__,
                    )
                    time.sleep(delay)

        raise last_exception  # type: ignore[misc]

    def _attempt_extraction(
        self,
        prompt: str,
        max_tokens: int,
        json_schema: dict,
        schema_name: str,
        attempt: int,
    ) -> dict:
        """Single extraction attempt against the Anthropic API."""
        logger.info(
            "Anthropic extraction attempt %d/%d, model=%s, schema=%s",
            attempt, MAX_RETRIES, self._model, schema_name,
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": json_schema,
                    },
                },
            )
        except anthropic.AuthenticationError as e:
            raise LLMAuthError("Anthropic authentication failed (check API key)") from e
        except anthropic.RateLimitError as e:
            raise LLMRateLimitError("Anthropic rate limited") from e
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            raise LLMTimeoutError(
                f"Anthropic connection error ({type(e).__name__})"
            ) from e

        # Check for truncation - raise and let outer retry loop handle it
        if response.stop_reason == "max_tokens":
            raise LLMFormatError(
                f"Response truncated (max_tokens) for schema {schema_name}"
            )

        # Check for refusal
        if response.stop_reason == "end_turn" and not response.content:
            raise LLMRefusalError(
                f"Model refused to generate response for schema {schema_name}"
            )

        # Parse JSON from response
        try:
            text = response.content[0].text
            return json.loads(text)
        except (json.JSONDecodeError, IndexError, AttributeError) as e:
            raise LLMFormatError(
                f"Failed to parse Anthropic response as JSON for "
                f"schema {schema_name} ({type(e).__name__})"
            ) from e
