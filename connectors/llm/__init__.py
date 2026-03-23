"""LLM connector module for structured extraction.

Provides a provider-agnostic interface for extracting structured JSON
from language models. Supports Anthropic (native) and OpenAI-compatible
providers with automatic fallback strategies for structured output.
"""

from .base import StructuredExtractor
from .factory import create_extractor

__all__ = ["StructuredExtractor", "create_extractor"]
