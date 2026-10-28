"""Local inference through Ollama (https://ollama.com). Free, runs on the owner's PC.

Structured output: the JSON schema is passed as Ollama's `format`, which constrains decoding
to valid JSON for that schema (Ollama >= 0.5). Qwen3's thinking mode is turned off (`think:
false`, Ollama >= 0.9) so the whole output budget goes to the answer.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from clipforge.llm.base import (
    LLMError,
    Logger,
    ProviderOutputError,
    ProviderStatus,
    ProviderUnavailable,
    estimate_tokens,
)

INSTALL_FIX = (
    "Install Ollama from https://ollama.com/download, start it (it runs in the tray on "
    "Windows, or run `ollama serve`), then `ollama pull {model}`."
)


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.split("-")[0].split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


class OllamaProvider:
    name = "ollama"
    real = True

    def __init__(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        num_ctx: int = 12288,
        keep_alive: str = "2m",
        timeout_seconds: float = 900.0,
        temperature: float = 0.2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.context_tokens = num_ctx
        self.keep_alive = keep_alive
        self.temperature = temperature
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            transport=transport,
        )

    # ---- availability -----------------------------------------------------------------------

    def _model_names(self) -> list[str]:
        r = self._client.get("/api/tags", timeout=10.0)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]

    def has_model(self, names: list[str]) -> bool:
        want = self.model if ":" in self.model else f"{self.model}:latest"
        return any(n == want or n == self.model for n in names)

    def check(self) -> ProviderStatus:
        fix = INSTALL_FIX.format(model=self.model)
        try:
            v = self._client.get("/api/version", timeout=5.0)
            v.raise_for_status()
            version = v.json().get("version", "?")
            names = self._model_names()
        except httpx.HTTPError as e:
            return ProviderStatus(
                False,
                self.name,
                self.model,
                f"Ollama isn't reachable at {self.base_url} ({type(e).__name__}).",
                fix,
            )
        if _version_tuple(version) and _version_tuple(version) < (0, 9):
            return ProviderStatus(
                False,
                self.name,
                self.model,
                f"Ollama {version} is too old (structured output + thinking control need 0.9+).",
                "Update Ollama from https://ollama.com/download.",
            )
        if not self.has_model(names):
            return ProviderStatus(
                False,
                self.name,
                self.model,
                f"Ollama {version} is running but model '{self.model}' isn't downloaded.",
                f"Run `ollama pull {self.model}`.",
            )
        return ProviderStatus(
            True,
            self.name,
            self.model,
            f"Ollama {version} with {self.model} (context {self.context_tokens} tokens).",
        )

    # ---- inference --------------------------------------------------------------------------

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
        prompt_tokens = estimate_tokens(system) + estimate_tokens(user)
        if prompt_tokens + max_output_tokens > self.context_tokens:
            raise LLMError(
                f"Prompt (~{prompt_tokens} tokens) + answer ({max_output_tokens}) don't fit the "
                f"{self.context_tokens}-token context. The caller should split the input."
            )
        user_msg: dict[str, Any] = {"role": "user", "content": user}
        if images:
            user_msg["images"] = [base64.b64encode(b).decode("ascii") for b in images]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, user_msg],
            "format": schema,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {
                "num_ctx": self.context_tokens,
                "num_predict": max_output_tokens,
                "temperature": self.temperature,
            },
        }
        r = self._post_chat(body, log)
        data = r.json()
        content = (data.get("message") or {}).get("content", "")
        if data.get("done_reason") == "length":
            raise ProviderOutputError(
                f"The local model's answer was cut off at {max_output_tokens} tokens."
            )
        if not content.strip():
            raise ProviderOutputError("The local model returned an empty answer.")
        evals = data.get("eval_count")
        secs = (data.get("total_duration") or 0) / 1e9
        if evals:
            log(
                f"{self.model}: {data.get('prompt_eval_count', '?')} prompt + {evals} output "
                f"tokens in {secs:.0f} s"
            )
        return content

    def _post_chat(self, body: dict[str, Any], log: Logger) -> httpx.Response:
        try:
            r = self._client.post("/api/chat", json=body)
        except httpx.ConnectError as e:
            raise ProviderUnavailable(
                f"Ollama isn't reachable at {self.base_url}. "
                + INSTALL_FIX.format(model=self.model)
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(
                f"The local model took longer than the timeout. On CPU this can happen with long "
                f"transcripts; raise OLLAMA_TIMEOUT_SECONDS or lower OLLAMA_NUM_CTX. ({e})"
            ) from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama request failed: {e}") from e
        if r.status_code == 200:
            return r
        text = r.text[:500]
        if r.status_code == 404 or "not found" in text.lower():
            raise ProviderUnavailable(
                f"Ollama doesn't have model '{self.model}'. Run `ollama pull {self.model}`."
            )
        if r.status_code == 400 and "think" in text.lower() and body.get("think") is not None:
            # Older Ollama or a model without thinking support: retry without the flag.
            log("Ollama rejected the thinking flag; retrying without it")
            body = {k: v for k, v in body.items() if k != "think"}
            return self._post_chat(body, log)
        if "out of memory" in text.lower() or "cudamalloc" in text.lower():
            raise LLMError(
                "The GPU ran out of memory loading the local model. Lower OLLAMA_NUM_CTX "
                f"(now {self.context_tokens}) or close other GPU apps. Ollama said: {text}"
            )
        raise LLMError(f"Ollama error {r.status_code}: {text}")

    def unload(self) -> None:
        """Free the model's VRAM now (e.g. before Whisper needs the GPU). Best effort."""
        try:
            self._client.post(
                "/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=15.0
            )
        except httpx.HTTPError:
            pass
