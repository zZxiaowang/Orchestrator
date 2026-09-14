"""测试夹具：一个可控的假中转网关（OpenAI 兼容）。"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import httpx
import pytest


def plan_payload() -> dict[str, Any]:
    return {
        "goal": "为示例项目建立可验证的骨架",
        "summary": "先立目录与契约，再补落地文件，最后验收。",
        "principles": ["先契约后实现", "每步可验收"],
        "components": [
            {"name": "core", "responsibility": "核心逻辑", "interfaces": ["run()"]},
        ],
        "steps": [
            {
                "id": 1,
                "title": "建立骨架",
                "goal": "创建核心目录与说明文件",
                "deliverables": ["steps/step-1.md"],
                "acceptance": ["文件存在且内容非空"],
                "depends_on": [],
            },
            {
                "id": 2,
                "title": "补充验收",
                "goal": "创建验收清单",
                "deliverables": ["steps/step-2.md"],
                "acceptance": ["清单列出至少一条可判定条件"],
                "depends_on": [1],
            },
        ],
    }


class FakeRelay:
    """模拟中转：按 system prompt 区分架构段与执行段。"""

    def __init__(self, *, fence_plan: bool = False, garbage_first_stream: bool = False) -> None:
        self.need_files_once = False
        self.block_first_executor = False
        self.always_need_files = False
        #: 意图分流的结果（"task" / "chat"）；真实模型判断，测试里固定
        self.intent_kind = "task"
        self.intent_calls = 0
        #: 注入到纲领第一步的客观检查项（用于验收测试）
        self.plan_checks: list[dict[str, Any]] | None = None
        #: 命令执行闭环："" / "fix-after-failure"（先失败，拿到报错后修好）/ "always-fail"
        self.command_flow = ""
        self.python = sys.executable
        self.requests: list[dict[str, Any]] = []
        self.fence_plan = fence_plan
        self.garbage_first_stream = garbage_first_stream
        self.stream_calls = 0
        self.executor_calls = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/models"):
                return httpx.Response(
                    200,
                    json={"data": [{"id": "gpt-5"}, {"id": "deepseek-v4"}]},
                )
            return httpx.Response(405, json={"error": {"message": "method not allowed"}})

        if request.method != "POST":
            return httpx.Response(405, json={"error": {"message": "method not allowed"}})

        if not path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"message": f"unknown path {path}"}})

        body = json.loads(request.content.decode("utf-8"))
        self.requests.append({"path": path, "body": body, "headers": dict(request.headers)})
        system = body["messages"][0]["content"]

        # 意图分流是独立的一类调用：不算执行段调用，也不占用执行段的"第一次"语义，
        # 否则 need_files / blocked 这类只作用于首个执行段调用的开关会被分流吃掉。
        if "请求分流器" in system:
            self.intent_calls += 1
            return httpx.Response(
                200,
                json={
                    "model": body.get("model", "fake"),
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {"kind": self.intent_kind, "reason": "测试分流"},
                                    ensure_ascii=False,
                                ),
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
                },
            )

        if "本地桌面工具（架构-执行双模型编排器）的助手" in system:
            # 问答分支：直接给一段回答，不产出文件
            text = f"这是对「{body['messages'][-1]['content'][:20]}」的直接回答。"
            if body.get("stream"):
                self.stream_calls += 1
                return _sse_response(text)
            return httpx.Response(
                200,
                json={
                    "model": body.get("model", "fake"),
                    "choices": [{"message": {"role": "assistant", "content": text}}],
                    "usage": {"total_tokens": 21},
                },
            )

        content = (
            self._architect_text(body) if "资深架构师" in system else self._executor_text(body)
        )

        if body.get("stream"):
            self.stream_calls += 1
            if self.garbage_first_stream and self.stream_calls == 1:
                content = "我先说说思路，不打算给 JSON。"
            return _sse_response(content)

        return httpx.Response(
            200,
            json={
                "model": body.get("model", "fake"),
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 42},
            },
        )

    def _architect_text(self, body: dict[str, Any] | None = None) -> str:
        # 续聊（多轮）走的是同一段架构提示词，用最后一条用户消息里的标记区分：
        # 这时只应该返回**新增**步骤，而不是重发整份纲领。
        if body is not None and "请只输出**新增**的步骤" in body["messages"][-1]["content"]:
            addition = {
                "goal": "追加：继续改进",
                "summary": "只输出新增步骤。",
                "steps": [
                    {
                        "id": 1,
                        "title": "追加步骤",
                        "goal": "按追加要求再改一处",
                        "deliverables": ["steps/step-3.md"],
                        "acceptance": ["文件存在"],
                        "depends_on": [],
                    }
                ],
            }
            payload = json.dumps(addition, ensure_ascii=False)
            return f"```json\n{payload}\n```" if self.fence_plan else payload
        plan = plan_payload()
        if self.plan_checks is not None:
            plan["steps"][0]["checks"] = self.plan_checks
        payload = json.dumps(plan, ensure_ascii=False)
        return f"```json\n{payload}\n```" if self.fence_plan else payload

    def _executor_text(self, body: dict[str, Any]) -> str:
        user = body["messages"][-1]["content"]
        self.executor_calls += 1
        if self.always_need_files:
            return json.dumps(
                {"need_files": ["src/app.py"], "need_reason": "还要再看看"},
                ensure_ascii=False,
            )
        if self.block_first_executor and self.executor_calls == 1:
            return json.dumps(
                {
                    "summary": "信息不足",
                    "blocked": True,
                    "block_reason": "工作区为空，无法盘点现有代码；请给出目录或改为从零新建",
                },
                ensure_ascii=False,
            )
        if self.need_files_once and self.executor_calls == 1:
            return json.dumps(
                {
                    "need_files": ["src/app.py"],
                    "need_reason": "需要查看现有实现才能改",
                },
                ensure_ascii=False,
            )
        # 只认"当前步骤"标题，避免匹配到历史交接文字里的"第 N 步"
        match = re.search(r"##\s*当前步骤（第\s*(\d+)\s*步）", user)
        if match is None:
            match = re.search(r"第\s*(\d+)\s*步", user)
        step_id = int(match.group(1)) if match else 1
        payload = {
            "summary": f"完成第 {step_id} 步，产出对应文件。",
            "blocked": False,
            "files": [
                {
                    "path": f"steps/step-{step_id}.md",
                    "action": "create",
                    "content": f"# 第 {step_id} 步\n\n由执行段生成。\n",
                }
            ],
            "commands": [{"cmd": "echo ok", "why": "验证环境"}],
            "notes": [f"第 {step_id} 步完成"],
        }
        if self.command_flow:
            # 第二轮（用户消息里带着"系统已经执行过这些命令"）才给出能通过的命令，
            # 用来验证"跑失败 → 回灌报错 → 继续修 → 复验通过"这条闭环。
            repaired = "系统已经执行过这些命令" in user
            if self.command_flow == "fix-after-failure" and repaired:
                payload["summary"] = f"第 {step_id} 步：按报错修正后复验通过。"
                payload["commands"] = [{"cmd": f"{self.python} -m pytest --version", "why": "复验"}]
                payload["notes"] = ["按报错改好了"]
            else:
                payload["commands"] = [
                    {"cmd": f"{self.python} -m pytest __no_such_tests_dir__", "why": "验证"}
                ]
        return json.dumps(payload, ensure_ascii=False)


def _sse_response(content: str) -> httpx.Response:
    chunks = [content[i : i + 32] for i in range(0, len(content), 32)] or [content]
    lines = []
    for chunk in chunks:
        payload = json.dumps({"choices": [{"delta": {"content": chunk}}]}, ensure_ascii=False)
        lines.append(f"data: {payload}\n\n")
    # 真实的 OpenAI 兼容网关在 stream_options.include_usage 时，
    # 会在最后一个数据块里带上 usage（choices 为空）；这里保持同样的形状，
    # 否则指标测试就测不到"流式也能记账"这条路径。
    lines.append(
        "data: "
        + json.dumps(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )
    lines.append("data: [DONE]\n\n")
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(lines).encode("utf-8"),
    )


@pytest.fixture()
def fake_relay() -> FakeRelay:
    return FakeRelay()
