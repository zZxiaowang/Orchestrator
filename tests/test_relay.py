"""中转客户端：协议、URL 回退、错误提示、参数降级。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from app.core.errors import RelayError
from app.core.relay import RelayClient

MESSAGES = [{"role": "user", "content": "你好"}]


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str = "https://relay.test",
    wire_api: str = "chat_completions",
) -> RelayClient:
    return RelayClient(
        base_url,
        "sk-test",
        wire_api=wire_api,
        transport=httpx.MockTransport(handler),
    )


def test_chat_completion_and_v1_fallback():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path != "/v1/chat/completions":
            return httpx.Response(404, json={"error": {"message": "no such path"}})
        body = json.loads(request.content)
        assert body["model"] == "gpt-5"
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={"model": "gpt-5", "choices": [{"message": {"content": "架构已就绪"}}]},
        )

    async def scenario() -> None:
        client = _client(handler)
        result = await client.acomplete(MESSAGES, model="gpt-5")
        assert result.text == "架构已就绪"
        assert seen == ["/chat/completions", "/v1/chat/completions"]
        # 探测结果被缓存：第二次直接命中 /v1
        await client.acomplete(MESSAGES, model="gpt-5")
        assert seen[-1] == "/v1/chat/completions"

    asyncio.run(scenario())


def test_auth_error_carries_actionable_hint():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    async def scenario() -> None:
        with pytest.raises(RelayError) as excinfo:
            await _client(handler).acomplete(MESSAGES, model="gpt-5")
        assert excinfo.value.status_code == 401
        assert "Key" in (excinfo.value.hint or "")

    asyncio.run(scenario())


def test_streaming_deltas_are_concatenated():
    def handler(_: httpx.Request) -> httpx.Response:
        parts = ["结", "构", "化"]
        body = "".join(
            f"data: {json.dumps({'choices': [{'delta': {'content': part}}]})}\n\n" for part in parts
        )
        payload = (body + "data: [DONE]\n\n").encode("utf-8")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    async def scenario() -> None:
        chunks = [chunk async for chunk in _client(handler).astream(MESSAGES, model="deepseek-v4")]
        assert "".join(chunks) == "结构化"

    asyncio.run(scenario())


def test_responses_wire_api_parses_output_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        return httpx.Response(200, json={"output_text": "来自 responses 协议"})

    async def scenario() -> None:
        client = _client(handler, base_url="https://relay.test/v1", wire_api="responses")
        result = await client.acomplete(MESSAGES, model="deepseek-v4")
        assert result.text == "来自 responses 协议"

    asyncio.run(scenario())


def test_responses_stream_does_not_duplicate_full_text_after_deltas():
    """Responses 协议：增量 + 最终全文同时出现时，不能重复拼接。"""

    def handler(_: httpx.Request) -> httpx.Response:
        events = [
            {"type": "response.output_text.delta", "delta": '{"goal":'},
            {"type": "response.output_text.delta", "delta": '"G"}'},
            {"type": "response.completed", "response": {"output_text": '{"goal":"G"}'}},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8")
        )

    async def scenario() -> None:
        client = _client(handler, base_url="https://relay.test/v1", wire_api="responses")
        chunks = [chunk async for chunk in client.astream(MESSAGES, model="gpt-5.6-sol")]
        assert "".join(chunks) == '{"goal":"G"}'

    asyncio.run(scenario())


def test_responses_stream_uses_completed_when_no_deltas():
    """只有最终事件、没有增量的网关也要能拿到内容。"""

    def handler(_: httpx.Request) -> httpx.Response:
        event = {"type": "response.completed", "response": {"output_text": "完整纲领"}}
        body = f"data: {json.dumps(event)}\n\n"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8")
        )

    async def scenario() -> None:
        client = _client(handler, base_url="https://relay.test/v1", wire_api="responses")
        chunks = [chunk async for chunk in client.astream(MESSAGES, model="gpt-5.6-sol")]
        assert "".join(chunks) == "完整纲领"

    asyncio.run(scenario())


def test_unsupported_parameter_is_dropped_and_retried():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "temperature" in body:
            return httpx.Response(
                400,
                json={
                    "error": {"message": "Unsupported parameter: 'temperature' is not supported"}
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def scenario() -> None:
        client = _client(handler, base_url="https://relay.test/v1")
        result = await client.acomplete(MESSAGES, model="gpt-5", temperature=0.2)
        assert result.text == "ok"
        assert len(calls) == 2
        assert "temperature" not in calls[1]

    asyncio.run(scenario())


def test_missing_credentials_raise_before_network():
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("不应发起网络请求")

    async def scenario() -> None:
        client = RelayClient("", "", transport=httpx.MockTransport(handler))
        with pytest.raises(RelayError) as excinfo:
            await client.acomplete(MESSAGES, model="gpt-5")
        assert "中转地址" in excinfo.value.message

    asyncio.run(scenario())


def test_stream_error_body_is_decoded_not_crashing():
    """错误体是 bytes 时不能抛 "expected str instance, bytes found"。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            headers={"content-type": "text/plain"},
            content=b"upstream relay temporarily unavailable",
        )

    async def scenario() -> None:
        client = _client(handler, base_url="https://relay.test/v1")
        with pytest.raises(RelayError) as excinfo:
            async for _ in client.astream(MESSAGES, model="gpt-5"):
                pass
        assert excinfo.value.status_code == 502
        assert "upstream relay temporarily unavailable" in excinfo.value.message

    asyncio.run(scenario())


def test_stream_is_fallback_to_non_streaming_when_gateway_rejects_stream():
    """网关不支持 stream:true 时，自动退回一次性请求，流程不中断。"""
    seen: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(bool(body.get("stream")))
        if body.get("stream"):
            return httpx.Response(
                400, json={"error": {"message": "this model does not support stream"}}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "退化为一次性返回的纲领"}}]}
        )

    async def scenario() -> None:
        client = _client(handler, base_url="https://relay.test/v1")
        chunks = [chunk async for chunk in client.astream_with_fallback(MESSAGES, model="gpt-5")]
        assert "".join(chunks) == "退化为一次性返回的纲领"
        assert seen == [True, False]

    asyncio.run(scenario())


def test_partial_stream_failure_is_not_retried():
    """已经产出内容后再失败，必须抛出而不是重试，避免内容重复。"""
    calls: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(bool(body.get("stream")))
        if body.get("stream"):
            payload = (
                'data: {"choices":[{"delta":{"content":"前半段"}}]}\n\ndata: {broken json}\n\n'
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=payload.encode()
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "不应被调用"}}]})

    async def scenario() -> None:
        client = _client(handler, base_url="https://relay.test/v1")
        received: list[str] = []
        # 正常收完（坏的那一行会被跳过），不会触发非流式回退
        async for chunk in client.astream_with_fallback(MESSAGES, model="gpt-5"):
            received.append(chunk)
        assert "".join(received) == "前半段"
        assert calls == [True]

    asyncio.run(scenario())


def test_streaming_502_is_retried_then_succeeds():
    """网关偶发 502（例如 Cloudflare 回源失败）时，流式请求也要自动重试。"""
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                502,
                headers={"content-type": "application/json"},
                json={
                    "error": {
                        "message": "The origin web server returned an invalid or incomplete response",
                        "type": "bad_response_status_code",
                    }
                },
            )
        payload = 'data: {"choices":[{"delta":{"content":"重试成功"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=payload.encode()
        )

    async def scenario() -> None:
        client = _client(handler, base_url="https://relay.test/v1")
        chunks = [chunk async for chunk in client.astream(MESSAGES, model="gpt-5")]
        assert "".join(chunks) == "重试成功"
        assert len(calls) == 2

    asyncio.run(scenario())


def test_502_hint_mentions_upstream_and_retry():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={"error": {"message": "bad_response_status_code"}},
        )

    async def scenario() -> None:
        client = _client(handler, base_url="https://relay.test/v1")
        with pytest.raises(RelayError) as excinfo:
            await client.acomplete(MESSAGES, model="gpt-5")
        hint = excinfo.value.hint or ""
        assert "重试" in hint
        assert "切换" in hint

    asyncio.run(scenario())
