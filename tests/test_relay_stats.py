"""中转调用遥测（CallStats）：指标能不能准，取决于这一层记不记、记得对不对。"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx

from app.core.relay import CallStats, RelayClient

MESSAGES = [{"role": "user", "content": "hi"}]


def _sse(chunks: list[dict]) -> str:
    lines = [f"data: {json.dumps(item, ensure_ascii=False)}\n\n" for item in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def test_streaming_requests_include_usage_and_records_it():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                [
                    {"choices": [{"delta": {"content": "你"}}]},
                    {"choices": [{"delta": {"content": "好"}}]},
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
                    },
                ]
            ).encode("utf-8"),
        )

    client = RelayClient("https://relay.test/v1", "sk-test", transport=httpx.MockTransport(handler))
    stats = CallStats()

    async def run() -> str:
        return "".join(
            [
                chunk
                async for chunk in client.astream_with_fallback(
                    MESSAGES, model="gpt-5", stats=stats
                )
            ]
        )

    assert asyncio.run(run()) == "你好"
    assert bodies[0]["stream_options"] == {"include_usage": True}
    assert stats.calls == 1
    assert stats.attempts == 1
    assert stats.retries == 0
    assert stats.streamed is True
    assert stats.usage == {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}


def test_gateway_rejecting_stream_options_is_retried_without_it():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append(body)
        if "stream_options" in body:
            return httpx.Response(
                400, json={"error": {"message": "unknown parameter: stream_options"}}
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse([{"choices": [{"delta": {"content": "ok"}}]}]).encode("utf-8"),
        )

    client = RelayClient("https://relay.test/v1", "sk-test", transport=httpx.MockTransport(handler))
    stats = CallStats()

    async def run() -> str:
        return "".join(
            [
                chunk
                async for chunk in client.astream_with_fallback(
                    MESSAGES, model="gpt-5", stats=stats
                )
            ]
        )

    assert asyncio.run(run()) == "ok"
    assert len(seen) == 2 and "stream_options" in seen[0] and "stream_options" not in seen[1]
    assert stats.calls == 1
    # 网关不认参数导致的重发是**传输层重试**，要记进账本
    assert stats.attempts == 2
    assert stats.usage == {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}


def test_transport_retry_counts_attempts_not_calls():
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(502, json={"error": {"message": "bad gateway"}})
        return httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
        )

    client = RelayClient(
        "https://relay.test/v1", "sk-test", transport=httpx.MockTransport(handler), timeout=5
    )
    stats = CallStats()
    result = asyncio.run(client.acomplete(MESSAGES, model="gpt-5", stats=stats))

    assert result.text == "ok"
    assert result.attempts == 2
    assert stats.calls == 1
    assert stats.attempts == 2
    assert stats.usage["total_tokens"] == 6


def test_stream_fallback_to_sync_counts_a_second_call():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("stream"):
            return httpx.Response(400, json={"error": {"message": "stream unsupported"}})
        return httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "choices": [{"message": {"role": "assistant", "content": "sync"}}],
                "usage": {"total_tokens": 8},
            },
        )

    client = RelayClient("https://relay.test/v1", "sk-test", transport=httpx.MockTransport(handler))
    stats = CallStats()

    async def run() -> str:
        return "".join(
            [
                chunk
                async for chunk in client.astream_with_fallback(
                    MESSAGES, model="gpt-5", stats=stats
                )
            ]
        )

    assert asyncio.run(run()) == "sync"
    assert stats.calls == 2
    assert stats.fell_back_to_sync is True
    assert stats.usage["total_tokens"] == 8


def test_failed_call_still_counts_attempt_and_duration():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid token"}})

    client = RelayClient(
        "https://relay.test/v1", "sk-test", transport=httpx.MockTransport(handler), timeout=5
    )
    stats = CallStats()
    with contextlib.suppress(Exception):
        asyncio.run(client.acomplete(MESSAGES, model="gpt-5", stats=stats))
    assert stats.calls == 1
    assert stats.attempts == 1
    assert stats.usage == {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
