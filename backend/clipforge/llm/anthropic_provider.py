"""Optional provider: Claude through the Anthropic API (paid; needs ANTHROPIC_API_KEY)."""

from __future__ import annotations

import base64
import os
from typing import Any

from clipforge.llm.base import (
    LLMError,
    Logger,
    ProviderOutputError,
    ProviderStatus,
    ProviderUnavailable,
)


class AnthropicProvider:
    name = "anthropic"
    real = True
    context_tokens = None  # 1M-token context; no chunking needed for podcast transcripts

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        effort: str = "high",
        fallbacks: str = "default",
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.use_fallbacks = fallbacks == "default"
        self._api_key = api_key
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key
                else anthropic.Anthropic()
            )
        return self._client

    def check(self) -> ProviderStatus:
        if self._client is None and not (self._api_key or os.environ.get("ANTHROPIC_API_KEY")):
            return ProviderStatus(
                False,
                self.name,
                self.model,
                "LLM_PROVIDER=anthropic but no ANTHROPIC_API_KEY is set.",
                "Add ANTHROPIC_API_KEY to .env, or set LLM_PROVIDER=ollama for free local "
                "inference.",
            )
        return ProviderStatus(True, self.name, self.model, f"Anthropic API, model {self.model}.")

    def _request(self, kwargs: dict[str, Any], with_fallbacks: bool):
        if with_fallbacks:
            # A request the model declines is re-run on a fallback model in the same call.
            stream = self.client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            stream = self.client.messages.stream(**kwargs)
        with stream as s:
            return s.get_final_message()

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
        import anthropic

        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(img).decode("ascii"),
                },
            }
            for img in images or []
        ]
        content.append({"type": "text", "text": user})
        kwargs: dict[str, Any] = dict(
            model=self.model,
            # Adaptive thinking shares this budget; keep room above the JSON answer itself.
            max_tokens=max(64000, max_output_tokens),
            system=system,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[{"role": "user", "content": content}],
        )
        try:
            msg = self._request(kwargs, self.use_fallbacks)
        except anthropic.BadRequestError as e:
            if not self.use_fallbacks or "fallback" not in str(e).lower():
                raise LLMError(f"Claude API rejected the request: {e}") from e
            log("Server-side fallback not accepted for this request; retrying without it")
            msg = self._request(kwargs, False)
        except anthropic.AuthenticationError as e:
            raise ProviderUnavailable(
                "Claude API key missing or invalid (ANTHROPIC_API_KEY)"
            ) from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"Claude API rate limit: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderUnavailable(f"Can't reach the Claude API: {e}") from e

        if msg.stop_reason == "refusal":
            raise ProviderOutputError(
                f"The model declined this request ({getattr(msg, 'stop_details', None)})"
            )
        if msg.stop_reason == "max_tokens":
            raise ProviderOutputError("The model's answer was cut off (max_tokens)")
        served_by = getattr(msg, "model", self.model)
        if served_by and served_by != self.model:
            log(f"Answered by fallback model {served_by}")
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
