"""Language-model providers. Ollama (local, free) is the default; Anthropic is optional.

Every caller asks a provider for JSON that matches a schema and validates it itself; providers
never invent output. Test doubles live in `clipforge.llm.doubles` and are only reachable when
FAKE_MODELS=true (or directly from tests).
"""

from clipforge.llm.base import (
    LLMError,
    LLMProvider,
    ProviderOutputError,
    ProviderStatus,
    ProviderUnavailable,
)
from clipforge.llm.factory import get_text_provider, get_vision_provider

__all__ = [
    "LLMError",
    "LLMProvider",
    "ProviderOutputError",
    "ProviderStatus",
    "ProviderUnavailable",
    "get_text_provider",
    "get_vision_provider",
]
