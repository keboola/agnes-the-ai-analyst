"""Base protocol for structured extraction from LLMs."""

from typing import Protocol


class StructuredExtractor(Protocol):
    """Protocol for structured JSON extraction from language models.

    This is a structured extraction interface, NOT a general LLM chat
    interface. Implementations must return parsed JSON matching the
    provided schema.
    """

    def extract_json(
        self,
        prompt: str,
        max_tokens: int,
        json_schema: dict,
        schema_name: str,
    ) -> dict:
        """Extract structured JSON from a prompt.

        Args:
            prompt: The extraction prompt to send to the model.
            max_tokens: Maximum tokens in the response.
            json_schema: JSON Schema that the response must conform to.
            schema_name: Human-readable name for the schema (used in
                logging and error messages).

        Returns:
            Parsed JSON dictionary conforming to the provided schema.

        Raises:
            LLMError: Base class for all LLM-related errors.
            LLMAuthError: Invalid API key (permanent, do not retry).
            LLMRateLimitError: Rate limited (transient, retry with backoff).
            LLMTimeoutError: Timeout or connection error (transient, retry).
            LLMFormatError: Invalid JSON or unexpected structure.
            LLMRefusalError: Model refused due to safety filter.
        """
        ...
