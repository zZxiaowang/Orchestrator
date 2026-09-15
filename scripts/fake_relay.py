"""本地假中转（OpenAI 兼容），用于离线演示与自检。

它不调用任何真实模型：架构段返回固定纲领，执行段按步骤产出 markdown 文件。
用途：在没有真实中转 Key 的情况下验证整条链路（界面、事件流、落盘）。

运行：
    python -m scripts.fake_relay       # 默认监听 127.0.0.1:8799
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Fake Relay", version="0.1.0")

#: 每个流式分片之间的间隔（秒）。默认沿用原值；调大可以复现"长时间流式"的界面压力。
CHUNK_DELAY_SECONDS = float(os.environ.get("FAKE_RELAY_CHUNK_DELAY", "0.02") or 0.02)
CHUNK_SIZE = int(os.environ.get("FAKE_RELAY_CHUNK_SIZE", "40") or 40)

PLAN = {
    "goal": "演示：为示例项目建立可验证的骨架",
    "summary": "先定义目录与契约，再落地文件，最后给出验收方式。",
    "principles": ["先契约后实现", "每步可独立验收", "不引入未验证的外部依赖"],
    "components": [
        {"name": "docs", "responsibility": "承载纲领与验收说明", "interfaces": ["plan.md"]},
        {
            "name": "steps",
            "responsibility": "按步骤落地可检查的产出",
            "interfaces": ["steps/step-N.md"],
        },
    ],
    "steps": [
        {
            "id": 1,
            "title": "建立目录与说明",
            "goal": "创建 steps 目录并写入首篇说明",
            "deliverables": ["steps/step-1.md"],
            "acceptance": ["文件存在且包含标题"],
            "depends_on": [],
        },
        {
            "id": 2,
            "title": "补充验收清单",
            "goal": "为每一步写明可判定的验收条件",
            "deliverables": ["steps/step-2.md"],
            "acceptance": ["清单至少包含一条可判定条件"],
            "depends_on": [1],
        },
        {
            "id": 3,
            "title": "汇总说明",
            "goal": "把前两步的产出一句话汇总",
            "deliverables": ["steps/step-3.md"],
            "acceptance": ["汇总文件提到前两步"],
            "depends_on": [1, 2],
        },
    ],
    "risks": ["假中转只做链路验证，不代表真实模型质量"],
    "open_questions": [],
}


def _architect_text() -> str:
    return f"```json\n{json.dumps(PLAN, ensure_ascii=False, indent=2)}\n```"


def _continue_text() -> str:
    """续聊只返回**新增**步骤（与真实架构段的约定一致）。"""

    addition = {
        "goal": "追加：继续改进",
        "summary": "只输出新增步骤。",
        "steps": [
            {
                "id": 1,
                "title": "追加步骤",
                "goal": "按追加要求再补一份产出",
                "deliverables": ["steps/step-4.md"],
                "acceptance": ["文件存在"],
                "depends_on": [],
            }
        ],
    }
    return f"```json\n{json.dumps(addition, ensure_ascii=False, indent=2)}\n```"


def _executor_text(body: dict[str, Any]) -> str:
    user = body["messages"][-1]["content"]
    match = re.search(r"第\s*(\d+)\s*步", user)
    step_id = int(match.group(1)) if match else 1
    payload = {
        "summary": f"完成第 {step_id} 步：产出 steps/step-{step_id}.md。",
        "blocked": False,
        "files": [
            {
                "path": f"steps/step-{step_id}.md",
                "action": "create",
                "content": (
                    f"# 第 {step_id} 步\n\n"
                    f"由本地假中转生成，用于验证「架构 → 执行」链路。\n\n"
                    f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                ),
            }
        ],
        "commands": [{"cmd": "python -m pytest -q", "why": "跑通测试确认产物可用"}],
        "notes": [f"第 {step_id} 步已完成，可继续下一步。"],
    }
    return json.dumps(payload, ensure_ascii=False)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    system = body["messages"][0]["content"]
    is_architect = "资深架构师" in system
    if is_architect and "请只输出**新增**的步骤" in body["messages"][-1]["content"]:
        content = _continue_text()
    else:
        content = _architect_text() if is_architect else _executor_text(body)
    model = body.get("model", "fake-model")

    if not body.get("stream"):
        return JSONResponse(
            {
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    def stream():
        for index in range(0, len(content), CHUNK_SIZE):
            chunk = content[index : index + CHUNK_SIZE]
            payload = json.dumps({"choices": [{"delta": {"content": chunk}}]}, ensure_ascii=False)
            yield f"data: {payload}\n\n"
            time.sleep(CHUNK_DELAY_SECONDS)
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "gpt-5"}, {"id": "deepseek-v4"}]}


def main() -> None:
    import uvicorn

    uvicorn.run("scripts.fake_relay:app", host="127.0.0.1", port=8799, reload=False)


if __name__ == "__main__":
    main()
