import asyncio
import json
import random
from types import SimpleNamespace

import httpx
import pytest

from spool.models.anthropic_client import AnthropicClient, to_anthropic_messages
from spool.models.base import ModelError, ModelRequest, parse_arguments
from spool.models.cassette import RecordingModel, ReplayModel
from spool.models.openai_compat import OpenAICompatClient
from spool.models.pricing import cost_usd
from spool.models.retry import RetryPolicy, TokenBucket, with_retries
from spool.models.scripted import ScriptedModel, reply
from spool.models.sse import SSEParser

from .conftest import run

REQ = ModelRequest(
    system="sys",
    messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "bash", "arguments": {"command": "ls"}, "raw_arguments": '{"command": "ls"}'}]},
        {"role": "tool", "tool_call_id": "c1", "name": "bash", "content": "a.txt", "is_error": False},
    ],
    tools=[{"name": "bash", "description": "run", "parameters": {"type": "object", "properties": {}}}],
    max_tokens=100,
    temperature=0.5,
)


def sse(*chunks) -> bytes:
    out = []
    for c in chunks:
        out.append("data: " + (c if isinstance(c, str) else json.dumps(c)) + "\n\n")
    return "".join(out).encode()


def client_with(handler, **kw) -> OpenAICompatClient:
    return OpenAICompatClient(base_url="http://test/v1", model="m", transport=httpx.MockTransport(handler),
                              retry=RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0), **kw)


def test_sse_parser():
    p = SSEParser()
    lines = [": comment", "event: ping", "data: a", "data: b", "", "data:no-space", "", "id: 7", ""]
    events = [e for line in lines if (e := p.feed(line))]
    assert [(e.event, e.data) for e in events] == [("ping", "a\nb"), (None, "no-space")]


def test_parse_arguments():
    assert parse_arguments('{"a": 1}') == ({"a": 1}, None)
    assert parse_arguments("") == ({}, None)
    assert parse_arguments('```json\n{"a": 1}\n```') == ({"a": 1}, None)
    assert parse_arguments("[1]")[1].startswith("arguments must be a JSON object")
    assert "not valid JSON" in parse_arguments('{"a": ')[1]


def test_openai_stream_with_fragmented_tool_call_and_llama_timings():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        body = sse(
            {"choices": [{"delta": {"role": "assistant", "content": "Let me "}}]},
            {"choices": [{"delta": {"content": "check."}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_A", "function": {"name": "read_file", "arguments": '{"pa'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_B", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 500, "completion_tokens": 30},
             "timings": {"cache_n": 450, "prompt_n": 50, "prompt_ms": 300.25, "predicted_n": 30, "predicted_ms": 2900.0}},
            "[DONE]",
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = client_with(handler, extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    resp = run(client.complete(REQ))
    body = seen["body"]
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"command": "ls"}'  # raw bytes kept
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "a.txt"}
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["stream"] is True
    assert resp.content == "Let me check."
    assert [(c["id"], c["name"], c["arguments"]) for c in resp.tool_calls] == [
        ("call_A", "read_file", {"path": "a.txt"}), ("call_B", "bash", {"command": "ls"})]
    assert resp.stop_reason == "tool_use"
    assert resp.usage["input_tokens"] == 500 and resp.usage["cached_tokens"] == 450
    assert resp.timings["cache_n"] == 450 and resp.timings["prompt_ms"] == 300.2
    run(client.aclose())


def test_openai_inline_tool_call_recovery():
    def handler(request):
        return httpx.Response(200, content=sse(
            {"choices": [{"delta": {"content": 'ok <tool_call>{"name": "bash", "arguments": {"command": "pwd"}}</tool_call>'}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}, "[DONE]"))

    resp = run(client_with(handler).complete(REQ))
    assert resp.tool_calls[0]["name"] == "bash" and resp.tool_calls[0]["arguments"] == {"command": "pwd"}
    assert resp.content == "ok" and resp.usage["inline_tool_calls"] == 1 and resp.stop_reason == "tool_use"


def test_openai_retries_429_then_succeeds_and_does_not_retry_400():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down", headers={"retry-after": "0"})
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "fine"}, "finish_reason": "stop"}]}, "[DONE]"))

    assert run(client_with(flaky).complete(REQ)).content == "fine" and calls["n"] == 2

    def bad(request):
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    calls["n"] = 0
    with pytest.raises(ModelError) as info:
        run(client_with(bad).complete(REQ))
    assert calls["n"] == 1 and info.value.status == 400 and not info.value.retriable


def test_openai_non_streaming():
    def handler(request):
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(200, json={
            "model": "m", "choices": [{"message": {"content": "hi", "tool_calls": [
                {"id": "x", "function": {"name": "bash", "arguments": "{bad"}}]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 8}}})

    resp = run(client_with(handler, stream=False).complete(REQ))
    assert resp.tool_calls[0]["parse_error"] and resp.usage["cached_tokens"] == 8


def test_with_retries_backoff_is_capped_full_jitter():
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    attempts = {"n": 0}

    async def fn():
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise ModelError("busy", retriable=True)
        return "ok"

    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=3.0)
    assert run(with_retries(fn, policy, sleep=fake_sleep, rng=random.Random(0))) == "ok"
    assert len(sleeps) == 3 and all(0 <= d <= 3.0 for d in sleeps)
    assert sleeps[0] <= 1.0 and sleeps[1] <= 2.0


def test_token_bucket_waits_for_refill():
    now = {"t": 0.0}
    waited = []

    async def fake_sleep(d):
        waited.append(d)
        now["t"] += d

    bucket = TokenBucket(rate=2.0, capacity=2.0, clock=lambda: now["t"], sleep=fake_sleep)

    async def take(n):
        for _ in range(n):
            await bucket.acquire()

    run(take(4))
    assert waited and abs(sum(waited) - 1.0) < 1e-9  # 2 extra tokens at 2/s = 1 second


def test_pricing():
    usage = {"input_tokens": 1_000_000, "cached_tokens": 800_000, "cache_write_tokens": 0, "output_tokens": 100_000}
    cost, known = cost_usd("claude-opus-5-5", usage)
    assert known and cost == pytest.approx(0.2 * 4.0 + 0.8 * 0.20 + 0.1 * 20.0)
    assert cost_usd("mystery-model", usage) == (0.0, False)


def test_anthropic_message_conversion_merges_tool_results():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "two calls", "tool_calls": [
            {"id": "t1", "name": "a", "arguments": {"x": 1}}, {"id": "t2", "name": "b", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1", "is_error": False},
        {"role": "tool", "tool_call_id": "t2", "content": "", "is_error": True},
        {"role": "assistant", "content": "", "tool_calls": [], "provider_content": [
            {"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "done"}]},
    ]
    out = to_anthropic_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant"]
    assert [b["type"] for b in out[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert out[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "r1"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "(no output)", "is_error": True},
    ]
    assert out[3]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


def test_anthropic_client_with_fake_sdk():
    captured = {}
    message = SimpleNamespace(
        content=[{"type": "thinking", "thinking": "", "signature": "s"},
                 {"type": "text", "text": "Reading."},
                 {"type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "a"}}],
        stop_reason="tool_use", model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=900,
                              cache_creation_input_tokens=0),
    )

    class FakeMessages:
        def stream(self, **params):
            captured.update(params)
            return FakeStream(message)

    fake = SimpleNamespace(messages=None, beta=SimpleNamespace(messages=FakeMessages()))
    client = AnthropicClient(model="claude-opus-5-5", client=fake, effort="high")
    resp = run(client.complete(REQ))
    assert captured["fallbacks"] == "default" and captured["betas"] == ["server-side-fallback-2026-07-01"]
    assert captured["cache_control"] == {"type": "ephemeral"} and "temperature" not in captured
    assert captured["output_config"] == {"effort": "high"}
    assert captured["tools"][0]["input_schema"] == REQ.tools[0]["parameters"]
    assert resp.tool_calls[0]["arguments"] == {"path": "a"} and resp.content == "Reading."
    assert resp.usage["input_tokens"] == 1000 and resp.usage["cached_tokens"] == 900
    assert resp.provider_content[0]["type"] == "thinking"
    assert resp.usage["cost_usd"] == pytest.approx((100 * 4.0 + 900 * 0.2 + 20 * 20.0) / 1e6)


def test_cassette_record_and_replay(tmp_path):
    path = tmp_path / "c.jsonl"
    rec = RecordingModel(ScriptedModel([reply("one"), reply("two")]), path)
    req2 = ModelRequest(system="s", messages=[{"role": "user", "content": "other"}], tools=[])
    assert run(rec.complete(REQ)).content == "one"
    assert run(rec.complete(req2)).content == "two"
    replay = ReplayModel(path)
    assert run(replay.complete(req2)).content == "two"   # keyed by digest, not order
    assert run(replay.complete(REQ)).content == "one"
    changed = ModelRequest(system="s ", messages=req2.messages, tools=[])
    with pytest.raises(ModelError, match="cassette miss"):
        run(replay.complete(changed))
