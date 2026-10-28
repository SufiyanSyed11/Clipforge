"""Pick the configured provider. LLM_PROVIDER=ollama (default) | anthropic.
VISION_PROVIDER=none (default) | ollama | anthropic, used to check B-roll contact sheets."""

from __future__ import annotations

from clipforge.llm.base import LLMProvider, ProviderStatus
from clipforge.settings import Settings, get_settings


def resolve_num_ctx(settings: Settings, default: int | None = None) -> int:
    """OLLAMA_NUM_CTX if set; else what clipforge-check sized for this GPU; else 12288."""
    if settings.ollama_num_ctx:
        return settings.ollama_num_ctx
    if default:
        return default
    from clipforge.tools.check_env import load_report

    report = load_report() or {}
    return int(report.get("defaults", {}).get("ollama_num_ctx") or 12288)


def _build(
    provider: str, settings: Settings, *, vision: bool, num_ctx_default: int | None = None
) -> LLMProvider:
    if provider == "ollama":
        from clipforge.llm.ollama import OllamaProvider

        return OllamaProvider(
            model=settings.ollama_vision_model if vision else settings.ollama_model,
            base_url=settings.ollama_base_url,
            num_ctx=resolve_num_ctx(settings, num_ctx_default),
            keep_alive=settings.ollama_keep_alive,
            timeout_seconds=settings.ollama_timeout_seconds,
            temperature=settings.ollama_temperature,
        )
    if provider == "anthropic":
        from clipforge.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=settings.anthropic_model,
            effort=settings.anthropic_effort,
            fallbacks=settings.anthropic_fallbacks,
            api_key=settings.anthropic_api_key or None,
        )
    raise ValueError(f"Unknown provider '{provider}' (use ollama or anthropic)")


def get_text_provider(
    settings: Settings | None = None, num_ctx_default: int | None = None
) -> LLMProvider:
    settings = settings or get_settings()
    if settings.fake_models:
        from clipforge.llm.doubles import FakeLLM

        return FakeLLM()
    return _build(settings.llm_provider, settings, vision=False, num_ctx_default=num_ctx_default)


class NoVisionProvider:
    """Vision checking is off: B-roll can't be auto-verified and needs manual approval."""

    name = "none"
    model = "-"
    real = True
    context_tokens = None

    def check(self) -> ProviderStatus:
        return ProviderStatus(
            False,
            self.name,
            self.model,
            "No vision model configured; B-roll contact sheets need manual approval.",
            "Optional: `ollama pull qwen2.5vl:3b` and set VISION_PROVIDER=ollama.",
        )

    def complete_json(self, *a, **kw) -> str:  # pragma: no cover - never called
        raise RuntimeError("vision is disabled")


def get_vision_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.fake_models:
        from clipforge.llm.doubles import FakeLLM

        return FakeLLM()
    if settings.vision_provider == "none":
        return NoVisionProvider()
    return _build(settings.vision_provider, settings, vision=True)
