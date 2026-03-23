"""Factory for creating structured extractors from instance configuration.

Reads the ai: section from instance.yaml (already resolved by config/loader.py)
and creates the appropriate StructuredExtractor implementation.
"""

import logging
from urllib.parse import urlparse

from .anthropic_provider import AnthropicExtractor
from .base import StructuredExtractor
from .openai_compat import OpenAICompatExtractor

logger = logging.getLogger(__name__)

# Default model when not specified in config
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Default structured output strategy
DEFAULT_STRUCTURED_OUTPUT = "auto"


def create_extractor(ai_config: dict) -> StructuredExtractor:
    """Create a structured extractor from the ai: config section.

    Supports two configuration formats:

    New format (explicit provider):
        ai:
          provider: anthropic | openai_compat
          api_key: ${ANTHROPIC_API_KEY}
          model: claude-haiku-4-5-20251001
          base_url: https://api.example.com/v1  # required for openai_compat
          structured_output: auto  # strict | json | auto

    Legacy format (backward compatible):
        ai:
          anthropic_api_key: ${ANTHROPIC_API_KEY}

    Args:
        ai_config: The ai: section dict from instance.yaml,
            already resolved by config/loader.py.

    Returns:
        A StructuredExtractor instance.

    Raises:
        ValueError: If configuration is invalid or incomplete.
    """
    if not ai_config or not isinstance(ai_config, dict):
        raise ValueError(
            "ai: section in instance.yaml must be a non-empty dict. "
            "Example:\n  ai:\n    provider: anthropic\n    api_key: ${ANTHROPIC_API_KEY}"
        )

    provider = ai_config.get("provider")

    # Legacy format detection: anthropic_api_key present, no provider
    if not provider and "anthropic_api_key" in ai_config:
        api_key = ai_config["anthropic_api_key"]
        _validate_api_key(api_key)
        model = ai_config.get("model", DEFAULT_MODEL)
        logger.info(
            "Creating AnthropicExtractor (legacy config), model=%s", model
        )
        return AnthropicExtractor(api_key=api_key, model=model)

    if not provider:
        raise ValueError(
            "ai.provider is required in instance.yaml. "
            "Supported: 'anthropic', 'openai_compat'. "
            "Hint: use ${ENV_VAR} syntax for secrets."
        )

    api_key = ai_config.get("api_key", "")
    _validate_api_key(api_key)
    model = ai_config.get("model", DEFAULT_MODEL)

    if provider == "anthropic":
        logger.info("Creating AnthropicExtractor, model=%s", model)
        return AnthropicExtractor(api_key=api_key, model=model)

    elif provider == "openai_compat":
        base_url = ai_config.get("base_url", "")
        if not base_url:
            raise ValueError(
                "ai.base_url is required when provider is 'openai_compat'. "
                "Example: base_url: https://api.openai.com/v1"
            )
        structured_output = ai_config.get(
            "structured_output", DEFAULT_STRUCTURED_OUTPUT,
        )
        if structured_output not in ("strict", "json", "auto"):
            raise ValueError(
                f"ai.structured_output must be 'strict', 'json', or 'auto', "
                f"got '{structured_output}'"
            )

        safe_url = _sanitize_url(base_url)
        logger.info(
            "Creating OpenAICompatExtractor, url=%s, model=%s, "
            "structured_output=%s",
            safe_url, model, structured_output,
        )
        return OpenAICompatExtractor(
            api_key=api_key,
            base_url=base_url,
            model=model,
            structured_output=structured_output,
        )

    else:
        raise ValueError(
            f"Unknown ai.provider '{provider}'. "
            f"Supported: 'anthropic', 'openai_compat'. "
            f"Hint: use ${{ENV_VAR}} syntax for secrets."
        )


def _validate_api_key(api_key: str) -> None:
    """Validate that an API key is present and non-empty.

    Raises:
        ValueError: If api_key is empty or missing.
    """
    if not api_key or not api_key.strip():
        raise ValueError(
            "ai.api_key (or ai.anthropic_api_key) must not be empty. "
            "Check that the corresponding environment variable is set "
            "and referenced with ${ENV_VAR} syntax in instance.yaml."
        )


def _sanitize_url(url: str) -> str:
    """Extract scheme://host from a URL for safe logging."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
