"""Language-model providers: Ollama (default), Anthropic (optional), factory, fail-fast checks.

Ollama is exercised against a simulated HTTP server (httpx.MockTransport) that speaks its API;
no real model runs here. Real local inference is checked on the owner's PC.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from clipforge.core import pick
from clipforge.llm import (
    LLMError,
    ProviderOutputError,
    ProviderUnavailable,
    get_text_provider,
    get_vision_provider,
)
from clipforge.llm.anthropic_provider import AnthropicProvider
from clipforge.llm.doubles import FakeLLM
from clipforge.llm.ollama import OllamaProvider
from clipforge.settings import Settings
from tests.helpers import api_client
from tests.test_pick import _clip, _transcript

GOOD = json.dumps({"clips": [_clip([(100, 122)])]})


class FakeOllama:
    """Simulated Ollama server. `chat` is a list of (status, body) answered in order."""

    def __init__(self, chat=None, version="0.12.3", models=("qwen3:4b",), down=False):
        self.chat = list(chat or [])
        self.version = version
        self.models = models
        self.down = down
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": self.version})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if path == "/api/generate":
            self.requests.append({"path": path, **json.loads(request.content)})
            return httpx.Response(200, json={"done": True})
        if path == "/api/chat":
            body = json.loads(request.content)
            self.requests.append(body)
            status, payload = self.chat.pop(0)
            if isinstance(payload, str):
                return httpx.Response(status, text=payload)
            return httpx.Response(status, json=payload)
        return httpx.Response(404, text="not found")


def _reply(content: str, done_reason: str = "stop") -> tuple[int, dict]:
    return 200, {
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": 1234,
        "eval_count": 321,
        "total_duration": 12_000_000_000,
    }


def _provider(server: FakeOllama, **kw) -> OllamaProvider:
    return OllamaProvider(
        model=kw.pop("model", "qwen3:4b"), transport=httpx.MockTransport(server.handler), **kw
    )


# ---- availability ---------------------------------------------------------------------------


def test_check_ok():
    status = _provider(FakeOllama()).check()
    assert status.ok and status.real
    assert "qwen3:4b" in status.detail and "12288" in status.detail


def test_check_service_down_explains_how_to_fix():
    status = _provider(FakeOllama(down=True)).check()
    assert not status.ok
    assert "isn't reachable" in status.detail
    assert "ollama pull qwen3:4b" in status.fix


def test_check_model_not_pulled():
    status = _provider(FakeOllama(models=("llama3.2:3b",))).check()
    assert not status.ok and status.fix == "Run `ollama pull qwen3:4b`."


def test_check_accepts_latest_tag_and_rejects_old_ollama():
    assert _provider(FakeOllama(models=("mymodel:latest",)), model="mymodel").check().ok
    old = _provider(FakeOllama(version="0.4.7")).check()
    assert not old.ok and "too old" in old.detail


# ---- requests -------------------------------------------------------------------------------


def test_request_shape_structured_json_no_thinking_and_context_limit():
    server = FakeOllama(chat=[_reply(GOOD)])
    logs: list[str] = []
    out = _provider(server, num_ctx=12288, keep_alive="2m").complete_json(
        "system text", "user text", pick.OUTPUT_SCHEMA, logs.append, max_output_tokens=6000
    )
    assert out == GOOD
    body = server.requests[0]
    assert body["model"] == "qwen3:4b"
    assert body["format"] == pick.OUTPUT_SCHEMA  # constrained decoding to the picker schema
    assert body["stream"] is False and body["think"] is False
    assert body["options"]["num_ctx"] == 12288
    assert body["options"]["num_predict"] == 6000
    assert body["keep_alive"] == "2m"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert any("321 output tokens" in m for m in logs)


def test_prompt_that_cannot_fit_the_context_is_refused_before_sending():
    server = FakeOllama()
    with pytest.raises(LLMError, match="don't fit"):
        _provider(server, num_ctx=2048).complete_json(
            "s", "x" * 9000, pick.OUTPUT_SCHEMA, print, max_output_tokens=1000
        )
    assert server.requests == []


def test_truncated_answer_is_an_error():
    server = FakeOllama(chat=[_reply('{"clips": [', "length")])
    with pytest.raises(ProviderOutputError, match="cut off"):
        _provider(server).complete_json("s", "u", pick.OUTPUT_SCHEMA, print)


def test_empty_answer_is_an_error():
    with pytest.raises(ProviderOutputError, match="empty"):
        _provider(FakeOllama(chat=[_reply("  ")])).complete_json("s", "u", {}, print)


def test_model_missing_at_request_time():
    server = FakeOllama(chat=[(404, {"error": "model 'qwen3:4b' not found"})])
    with pytest.raises(ProviderUnavailable, match="ollama pull qwen3:4b"):
        _provider(server).complete_json("s", "u", {}, print)


def test_service_down_at_request_time():
    with pytest.raises(ProviderUnavailable, match="isn't reachable"):
        _provider(FakeOllama(down=True)).complete_json("s", "u", {}, print)


def test_gpu_out_of_memory_suggests_smaller_context():
    server = FakeOllama(chat=[(500, {"error": "llama runner: CUDA error: out of memory"})])
    with pytest.raises(LLMError, match="OLLAMA_NUM_CTX"):
        _provider(server).complete_json("s", "u", {}, print)


def test_think_flag_rejected_is_retried_without_it():
    server = FakeOllama(chat=[(400, {"error": '"qwen3:4b" does not support think'}), _reply(GOOD)])
    assert _provider(server).complete_json("s", "u", {}, print) == GOOD
    assert "think" in server.requests[0] and "think" not in server.requests[1]


def test_unload_frees_vram():
    server = FakeOllama()
    _provider(server).unload()
    assert server.requests == [{"path": "/api/generate", "model": "qwen3:4b", "keep_alive": 0}]


def test_images_are_sent_base64_for_vision():
    server = FakeOllama(chat=[_reply("{}")])
    _provider(server, model="qwen2.5vl:3b").complete_json("s", "u", {}, print, images=[b"\xff"])
    assert server.requests[0]["messages"][1]["images"] == ["/w=="]


# ---- the picker on top of the local provider --------------------------------------------------


def test_picker_retries_malformed_json_from_local_model_then_succeeds():
    server = FakeOllama(chat=[_reply("Sure! Here are clips: {oops"), _reply(GOOD)])
    res = pick.pick_clips(_transcript(), "S1", _provider(server), print, n=8, max_retries=2)
    assert res.attempts == 2 and res.real and res.provider == "ollama"
    assert [c.duration for c in res.clips] == [22]


def test_picker_fails_safely_when_local_model_keeps_returning_garbage():
    server = FakeOllama(chat=[_reply("{}"), _reply("[1,2]"), _reply('{"clips": [{"x": 1}]}')])
    with pytest.raises(pick.PickerError, match="invalid JSON every time"):
        pick.pick_clips(_transcript(), "S1", _provider(server), print, max_retries=2)


def test_picker_still_rejects_over_30s_from_local_model():
    bad = json.dumps({"clips": [_clip([(10, 45)]), _clip([(100, 125)])]})
    res = pick.pick_clips(_transcript(), "S1", _provider(FakeOllama(chat=[_reply(bad)])), print)
    assert [c.duration for c in res.clips] == [25]
    assert "30 s hard max" in res.rejected[0]["reason"]


def test_picker_provider_down_mid_job_fails_once():
    with pytest.raises(pick.PickerError, match="isn't reachable"):
        pick.pick_clips(_transcript(), "S1", _provider(FakeOllama(down=True)), print)


# ---- Anthropic (optional) -------------------------------------------------------------------


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def _sdk_client(msg, record):
    def stream(**kw):
        record.append(kw)
        return _Stream(msg)

    return SimpleNamespace(
        messages=SimpleNamespace(stream=stream),
        beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)),
    )


def _msg(text, stop="end_turn", model="claude-opus-5-5"):
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=None,
        model=model,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
    )


def test_anthropic_request_shape():
    calls: list[dict] = []
    p = AnthropicProvider(client=_sdk_client(_msg('{"clips": []}'), calls))
    assert p.complete_json("sys", "user", pick.OUTPUT_SCHEMA, print) == '{"clips": []}'
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": pick.OUTPUT_SCHEMA}
    assert kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in kw

    calls.clear()
    AnthropicProvider(fallbacks="off", client=_sdk_client(_msg("{}"), calls)).complete_json(
        "s", "u", {}, print
    )
    assert "fallbacks" not in calls[0] and "betas" not in calls[0]


@pytest.mark.parametrize(("stop", "match"), [("refusal", "declined"), ("max_tokens", "cut off")])
def test_anthropic_stop_reasons(stop, match):
    p = AnthropicProvider(client=_sdk_client(_msg("", stop=stop), []))
    with pytest.raises(ProviderOutputError, match=match):
        p.complete_json("s", "u", {}, print)


def test_anthropic_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    status = AnthropicProvider().check()
    assert not status.ok and "LLM_PROVIDER=ollama" in status.fix


# ---- factory and job-level checks -------------------------------------------------------------


def test_default_provider_is_local_ollama_and_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = get_text_provider(Settings())
    assert isinstance(p, OllamaProvider)
    assert p.model == "qwen3:4b" and p.context_tokens == 12288  # no env report: safe default


def test_provider_is_configurable():
    p = get_text_provider(Settings(ollama_model="qwen3:8b", ollama_num_ctx=8192))
    assert (p.model, p.context_tokens) == ("qwen3:8b", 8192)
    assert isinstance(get_text_provider(Settings(llm_provider="anthropic")), AnthropicProvider)
    with pytest.raises(ValueError):
        get_text_provider(Settings(llm_provider="nope"))


def test_test_double_only_with_fake_models():
    assert isinstance(get_text_provider(Settings(fake_models=True)), FakeLLM)
    assert not FakeLLM().check().real


def test_vision_is_off_by_default():
    status = get_vision_provider(Settings()).check()
    assert not status.ok and "manual approval" in status.detail


def test_job_fails_fast_when_ollama_is_down(monkeypatch, tmp_path):
    """process_project checks the picker model before the long ingest/transcribe steps."""

    from clipforge.jobs.worker import run_one
    from clipforge.llm import factory
    from tests.media import make_video

    down = OllamaProvider(
        model="qwen3:4b", transport=httpx.MockTransport(FakeOllama(down=True).handler)
    )
    monkeypatch.setattr(factory, "_build", lambda *a, **k: down)
    c = api_client()
    video = make_video(tmp_path / "ep.mp4", seconds=3)
    with video.open("rb") as f:
        pid = c.post(
            "/api/projects",
            data={"rights_confirmed": "true"},
            files={"file": ("ep.mp4", f, "video/mp4")},
        ).json()["id"]
    run_one("t")
    p = c.get(f"/api/projects/{pid}").json()
    assert p["status"] == "failed"
    err = p["jobs"][0]["error"]
    assert "Clip picker unavailable" in err and "ollama pull qwen3:4b" in err
    assert p["media_info"] is None  # nothing was ingested
