"""意图分流：先判断「这是需求还是问答」，再决定要不要进编排。

为什么必须有这一层（来自历史运行，不是假想）：

* 运行记录里有两条任务叫「你是哪个模型」，它们被当成需求走了完整编排，
  生成了纲领，还真的改了工作区文件；
* 编排是重流程：建运行目录、写工作区、两段模型各跑一遍。它只该处理
  「把某个东西做出来」的请求，不该处理「你是谁」。

策略（宁可多跑一次编排，也不能把真需求当闲聊丢掉）：

1. 高精度启发式先拦下明显的问候 / 身份询问 / 短确认，零成本、零延迟；
2. 拿不准时用架构段模型做一次极短的分类调用（只要一个 JSON）；
3. **分类失败一律按 task 处理**——这是 fail-open：代价是多跑一次编排，
   而不是用户的需求被静默吞掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.jsonx import extract_json_object
from app.core.relay import CallStats, RelayClient

CHAT = "chat"
TASK = "task"

INTENT_SYSTEM = """你是一个请求分流器。判断用户这句话属于哪一类：

- "chat"：闲聊、问候、询问你是谁 / 你用什么模型 / 你的能力，或者任何
  **不需要产出或改动文件**的提问；
- "task"：要让系统做出或修改东西——写代码、建文件、改配置、修问题、重构、
  写文档并落盘、跑一段流程等。

只输出一个 JSON 对象，不要解释文字，不要 Markdown 代码块：
{"kind": "chat 或 task", "reason": "不超过 20 字的理由"}

判断原则：只要用户表达了「把某个东西做出来 / 改掉」的意图，就是 task；
单纯提问、试探、打招呼、评价，都是 chat。拿不准时选 task。"""


@dataclass
class Intent:
    """分流结果。``source`` 说明是启发式判的还是模型判的，便于排查误判。"""

    kind: str
    reason: str = ""
    source: str = "model"

    @property
    def is_chat(self) -> bool:
        return self.kind == CHAT


#: 明确在问「你自己」或打招呼的说法（命中即判 chat，不再调用模型）
_CHAT_PATTERNS = (
    r"^(你好|您好|哈喽|嗨|hi|hello|hey|在吗|在不在|谢谢|感谢|早上好|晚上好)[!！。~\s]*$",
    r"(你是(谁|哪个模型|什么模型|什么版本|谁的模型)|你叫什么|你的模型|你用什么模型|你是什么)",
    r"^(啥|啊|哦|嗯|好|ok|OK|收到|知道了)[!！。~\s]*$",
)

#: 出现这些词基本可以确定要产出东西，直接当需求，不做模型分类
_TASK_HINTS = (
    "文件",
    "目录",
    "创建",
    "新建",
    "实现",
    "修改",
    "改成",
    "重构",
    "修复",
    "添加",
    "增加",
    "删除",
    "生成",
    "写一个",
    "写个",
    "脚本",
    "部署",
    "安装",
    "测试",
    "接口",
    "文档",
    "项目",
    "代码",
    "仓库",
    "打包",
    "数据库",
    "页面",
    "按钮",
    "功能",
    "优化",
)


def heuristic_intent(task: str) -> Intent | None:
    """零成本预判：能确定就直接给结论，拿不准返回 ``None`` 交给模型。"""

    text = " ".join((task or "").split())
    if not text:
        return Intent(TASK, "任务描述为空", source="heuristic")

    lowered = text.lower()
    if len(text) <= 40 and any(
        re.search(pattern, text, re.IGNORECASE) for pattern in _CHAT_PATTERNS
    ):
        return Intent(CHAT, "问候或询问助手自身", source="heuristic")

    # 短句 + 没有任何产出动作 + 明显是问句 → 问答
    is_question = text.endswith(("?", "？")) or text.startswith(
        ("为什么", "怎么", "能否", "可以吗", "是不是")
    )
    if len(text) <= 60 and is_question and not any(hint in lowered for hint in _TASK_HINTS):
        return Intent(CHAT, "短问句且不含产出动作", source="heuristic")

    return None


def build_intent_messages(task: str, *, brief: str = "") -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": INTENT_SYSTEM}]
    if brief.strip():
        messages.append(
            {"role": "user", "content": f"（本次会话的背景简报）\n{brief.strip()[:600]}"}
        )
    messages.append({"role": "user", "content": f"用户这句话是：\n{task}"})
    return messages


def parse_intent(text: str) -> Intent | None:
    """解析分类结果；形状不对时返回 ``None``（由调用方决定保守策略）。"""

    data = extract_json_object(text)
    if not isinstance(data, dict):
        return None
    raw = str(data.get("kind") or data.get("intent") or data.get("type") or "").strip().lower()
    if raw in ("chat", "闲聊", "问答", "question"):
        kind = CHAT
    elif raw in ("task", "需求", "任务", "coding"):
        kind = TASK
    else:
        return None
    reason = str(data.get("reason") or data.get("why") or "").strip()[:60]
    return Intent(kind, reason, source="model")


async def detect_intent(
    client: RelayClient,
    task: str,
    *,
    model: str,
    brief: str = "",
    stats: CallStats | None = None,
) -> Intent:
    """先启发式、再模型；模型这条路失败时按 task 兜底（绝不静默丢需求）。"""

    guess = heuristic_intent(task)
    if guess is not None:
        return guess

    messages = build_intent_messages(task, brief=brief)
    try:
        result = await client.acomplete(messages, model=model, json_mode=True, stats=stats)
    except Exception:  # noqa: BLE001 - 分流失败不能拦住真需求
        return Intent(TASK, "意图分类调用失败，按需求处理", source="fallback")
    parsed = parse_intent(result.text)
    if parsed is None:
        return Intent(TASK, "意图分类结果无法解析，按需求处理", source="fallback")
    return parsed
