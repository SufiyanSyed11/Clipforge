from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

Logger = Callable[[str], None]


class LLMError(Exception):
    """A model call failed in a way the user should see (no traceback needed)."""


class ProviderUnavailable(LLMError):
    """The provider can't be used right now (service down, model missing, no key)."""


class ProviderOutputError(LLMError):
    """The model answered but the answer can't be used (cut off, refused)."""


@dataclass
class ProviderStatus:
    ok: bool
    provider: str
    model: str
    detail: str
    fix: str | None = None
    real: bool = True  # False only for test doubles

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "model": self.model,
            "detail": self.detail,
            "fix": self.fix,
            "real": self.real,
        }

    def message(self) -> str:
        return self.detail + (f" Fix: {self.fix}" if self.fix else "")


class LLMProvider(Protocol):
    name: str
    model: str
    # True for real inference; False for test doubles. Results from a provider with
    # real=False are always labelled as not real in QA reports.
    real: bool
    # Context window the provider is configured for, in tokens; None = effectively unlimited
    # for our prompt sizes. Callers chunk long inputs to fit.
    context_tokens: int | None

    def check(self) -> ProviderStatus:
        """Is the provider usable right now? Cheap; called before every job that needs it."""
        ...

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        log: Logger,
        *,
        max_output_tokens: int = 4096,
        images: list[bytes] | None = None,
    ) -> str:
        """Return the model's raw JSON text for `schema`. The caller validates it."""
        ...


def estimate_tokens(text: str) -> int:
    """Conservative token estimate. Transcripts are full of digits and timestamps, which
    tokenize densely, so assume ~3 characters per token rather than the usual ~4."""
    return len(text) // 3 + 1
