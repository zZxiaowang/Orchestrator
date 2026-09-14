"""意图分流：明显问答不进编排，真需求绝不因为分流而丢失。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.core.relay import RelayClient
from app.services.intent import CHAT, TASK, detect_intent, heuristic_intent, parse_intent


def _client(content: str) -> RelayClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 5},
            },
        )

    return RelayClient("https://relay.test/v1", "sk-test", transport=httpx.MockTransport(handler))


# ── 启发式（零成本，必须先挡住明显情形）─────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["你是哪个模型", "你是谁？", "你好", "在吗", "谢谢", "啥", "OK"],
)
def test_obvious_chat_is_classified_without_model(text: str):
    guess = heuristic_intent(text)
    assert guess is not None and guess.kind == CHAT, text


@pytest.mark.parametrize(
    "text",
    [
        "改一下左侧任务栏的折叠逻辑",
        "帮我把 README 里的安装步骤补全",
        "实现一个导出 CSV 的接口",
        "优化你本身的设计及用户功能操作",
    ],
)
def test_requirements_are_never_short_circuited_to_chat(text: str):
    guess = heuristic_intent(text)
    assert guess is None or guess.kind == TASK, text


def test_empty_task_is_a_task():
    guess = heuristic_intent("   ")
    assert guess is not None and guess.kind == TASK


# ── 模型分类 ────────────────────────────────────────────────────────────


def test_parse_intent_accepts_aliases():
    assert parse_intent('{"kind": "闲聊"}').kind == CHAT
    assert parse_intent('{"intent": "需求"}').kind == TASK
    assert parse_intent('```json\n{"type": "task"}\n```').kind == TASK


def test_parse_intent_rejects_unknown_shape():
    assert parse_intent("我觉得这是需求") is None
    assert parse_intent('{"kind": "maybe"}') is None


def test_ambiguous_request_goes_to_the_model():
    client = _client(json.dumps({"kind": "chat", "reason": "只是想问问"}, ensure_ascii=False))
    intent = asyncio.run(
        detect_intent(client, model="gpt-5", task="这个工具适合做数据分析吗？还是专心写代码比较好")
    )
    assert intent.kind == CHAT
    assert intent.source == "model"
    assert intent.reason == "只是想问问"


def test_model_call_failure_falls_back_to_task():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    client = RelayClient(
        "https://relay.test/v1", "sk-test", transport=httpx.MockTransport(handler), timeout=1
    )
    intent = asyncio.run(
        detect_intent(client, model="gpt-5", task="这个工具适合做数据分析吗？还是专心写代码比较好")
    )
    assert intent.kind == TASK
    assert intent.source == "fallback"


def test_unparseable_model_output_falls_back_to_task():
    client = _client("我觉得这算需求吧，说不好。")
    intent = asyncio.run(
        detect_intent(client, model="gpt-5", task="这个工具适合做数据分析吗？还是专心写代码比较好")
    )
    assert intent.kind == TASK
    assert intent.source == "fallback"
