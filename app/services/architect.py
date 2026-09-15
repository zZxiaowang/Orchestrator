"""架构段：由 GPT 产出**纲领性架构**。

这一段只回答"做什么、怎么拆、怎么验收"，不写实现代码——实现交给执行段。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.core.errors import PlanParseError
from app.core.jsonx import extract_json_object
from app.core.relay import CallStats, RelayClient, unwrap_result
from app.schemas.plan import ArchitecturePlan

ARCHITECT_SYSTEM = """你是一名资深架构师，负责把需求转成**纲领性架构**，交给另一位工程师（执行段）落地。

输出要求（必须严格遵守）：
1. 只输出一个 JSON 对象，不要任何解释文字或 Markdown 代码块标记。
2. JSON 字段：
{
  "goal": "一句话目标",
  "summary": "纲领性说明：整体思路、关键取舍、为什么这样拆",
  "principles": ["设计原则/约束，3-6 条"],
  "components": [{"name": "组件名", "responsibility": "职责", "interfaces": ["对外接口/契约"]}],
  "steps": [
    {
      "id": 1,
      "title": "短标题",
      "goal": "这一步要达到什么",
      "deliverables": ["产出物：相对路径的文件或可交付内容"],
      "acceptance": ["可判定的验收条件"],
      "checks": [
        {"type": "file_exists", "path": "相对路径", "label": "给人看的说明"}
      ],
      "depends_on": [0]
    }
  ],
  "risks": ["主要风险与缓解方式"],
  "open_questions": ["需要提问澄清的点；没有就给空数组"]
}

拆分原则：
- 步骤数量控制在 3 到 __MAX_STEPS__ 之间，按执行顺序排列，id 从 1 连续编号。
- 每一步都必须能被独立执行、独立验收；不要把两件互不相关的事塞进同一步。
- 步骤面向"可交付的结果"，而不是"做了什么动作"。
- deliverables 用工作区内的相对路径；非文件产出（如结论、决策）用自然语言描述。
- acceptance 必须是客观可判定的条件，禁止"质量良好"这类无法验证的表述。
- checks 是**系统会自动执行**的客观检查，执行完会被逐条判定，不通过这一步就不算完成。
  只能从这六种里选：
  `file_exists`（path：文件必须存在）、
  `dir_exists`（path：目录必须存在）、
  `glob`（path：通配符，至少匹配一个文件）、
  `file_contains`（path + text：文件里必须出现该原文片段；**text 要短且稳定**，
  用中文关键词或函数名即可，例如「普通对话」「def main」；系统对 `project_id` 与
  `projectId` 这类标识符写法差异是宽容的，但**不要**拿一整句话当检查项）、
  `py_compile`（path：该 .py 文件必须能编译通过）、
  `py_import`（path：该 .py 文件必须能被导入；系统会在工作区里真的导入一次，
  因此需要用户开启「允许执行验证命令」，没开启时自动降级为语法编译检查）、
  `json_valid`（path：该文件必须是合法 JSON）。
  系统还会自动为 deliverables 里形如路径的产出补一条 file_exists，并为本步实际改过的
  `.py` 文件补一条 py_compile，所以**不需要**重复写它们。
  每步 1-3 条为宜，只写真正能证明"这一步做成了"的检查。
  绝对不要写命令类检查（pytest、npm 等）——系统不会执行命令。
- 不要写具体实现代码，也不要指定具体库版本；这些属于执行段的职责。
- 如果需求信息不足，先在 open_questions 里列出问题，同时给出**可执行的默认方案**，不要因此拒绝输出架构。

语言：与用户需求保持一致（默认中文）。"""


CHAT_SYSTEM = """你是一个本地桌面工具（架构-执行双模型编排器）的助手。
用户这次发来的是**问答 / 闲聊**，不需要产出或改动任何文件。

回答要求：
1. 直接、简洁地回答问题（中文，2-6 句；需要列举时用短列表）。
2. 不要输出 JSON，不要生成"纲领"，不要假装执行了任何操作，也不要声称改了文件。
3. 如果用户其实是想让你改代码 / 产出文件，用一句话提醒他：直接描述要做的目标即可，
   系统会按"纲领 → 人工确认 → 执行"的流程处理。
4. 不知道就直说不知道，不要编造本工具的功能。"""


async def run_chat(
    client: RelayClient,
    task: str,
    *,
    model: str,
    brief: str = "",
    on_token: Callable[[str], None] | None = None,
    stats: CallStats | None = None,
) -> str:
    """判定为问答时直接回答：不生成纲领、不建步骤、不碰工作区。"""

    messages: list[dict[str, Any]] = [{"role": "system", "content": CHAT_SYSTEM}]
    if brief.strip():
        messages.append({"role": "user", "content": f"（背景简报）\n{brief.strip()[:800]}"})
    messages.append({"role": "user", "content": task})

    buffer: list[str] = []
    async for chunk in client.astream_with_fallback(messages, model=model, stats=stats):
        buffer.append(chunk)
        if on_token:
            on_token(chunk)
    return "".join(buffer)


def build_architect_messages(
    task: str,
    *,
    max_steps: int,
    feedback: str | None = None,
    context: str | None = None,
    previous_plan: str | None = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": ARCHITECT_SYSTEM.replace("__MAX_STEPS__", str(max_steps)),
        }
    ]
    if context:
        messages.append(
            {
                "role": "user",
                "content": f"以下是当前项目/工作区的客观情况，作为架构约束参考：\n{context}",
            }
        )
    messages.append({"role": "user", "content": f"需求：\n{task}"})
    if previous_plan:
        messages.append({"role": "assistant", "content": previous_plan})
        messages.append(
            {
                "role": "user",
                "content": (
                    "请根据下面的反馈修订纲领，仍然只输出 JSON 对象：\n"
                    f"{feedback or '请优化拆分粒度与验收标准。'}"
                ),
            }
        )
    return messages


def build_continue_messages(
    task: str,
    *,
    max_steps: int,
    done_log: str,
    instruction: str,
    context: str | None = None,
) -> list[dict[str, Any]]:
    """多轮续聊：在**已完成的运行**上追加新步骤，而不是重写整份纲领。

    关键在于告诉架构段"哪些事已经做完了"，否则它会重复规划（并浪费执行段的额度）。
    """

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": ARCHITECT_SYSTEM.replace("__MAX_STEPS__", str(max_steps)),
        }
    ]
    if context:
        messages.append(
            {
                "role": "user",
                "content": f"以下是当前项目/工作区的客观情况，作为架构约束参考：\n{context}",
            }
        )
    messages.append({"role": "user", "content": f"原始需求：\n{task}"})
    messages.append(
        {
            "role": "user",
            "content": (
                "这条任务此前已经执行过的步骤（**不要重复它们**）：\n"
                f"{done_log or '（还没有完成的步骤）'}"
            ),
        }
    )
    messages.append(
        {
            "role": "user",
            "content": (
                "用户现在追加了新的要求：\n"
                f"{instruction}\n\n"
                "请只输出**新增**的步骤（steps 里的 id 从 1 开始重新编号，系统会接着排），"
                "不要重复已经完成的步骤，也不要重写既有决策；"
                "仍然只输出那一个 JSON 对象。"
            ),
        }
    )
    return messages


async def run_architect(
    client: RelayClient,
    messages: Sequence[dict[str, Any]],
    *,
    model: str,
    on_token: Callable[[str], None] | None = None,
    stats: CallStats | None = None,
) -> tuple[ArchitecturePlan, str]:
    """流式获取架构段输出，并解析为纲领。"""
    buffer: list[str] = []
    async for chunk in client.astream_with_fallback(messages, model=model, stats=stats):
        buffer.append(chunk)
        if on_token:
            on_token(chunk)
    raw = "".join(buffer)

    try:
        return parse_plan(raw, max_steps=0), raw
    except PlanParseError:
        if not on_token:
            raise
    return await _retry_as_json(
        client, messages, model=model, on_token=on_token, raw=raw, stats=stats
    )


async def _retry_as_json(
    client: RelayClient,
    messages: Sequence[dict[str, Any]],
    *,
    model: str,
    on_token: Callable[[str], None] | None,
    raw: str,
    stats: CallStats | None = None,
) -> tuple[ArchitecturePlan, str]:
    """首次流式输出不是合法 JSON 时，改用 JSON 模式重试一次。"""
    retry_messages = list(messages)
    if raw.strip():
        retry_messages.append({"role": "assistant", "content": raw})
    retry_messages.append(
        {
            "role": "user",
            "content": "上面的输出不是合法 JSON。请只输出那一个 JSON 对象，不要任何其他字符。",
        }
    )
    if on_token:
        on_token("\n\n[架构段输出无法解析为 JSON，正在强制重试…]\n")
    if stats is not None:
        stats.retry()
    result = unwrap_result(
        await client.acomplete(retry_messages, model=model, json_mode=True, stats=stats)
    )
    return parse_plan(result.text, max_steps=0), result.text


def parse_plan(text: str, *, max_steps: int = 0) -> ArchitecturePlan:
    data = extract_json_object(text)
    if data is None:
        raise PlanParseError(
            "架构段返回内容无法解析为 JSON 纲领。",
            details={"excerpt": " ".join(text.split())[:300]},
        )
    plan = ArchitecturePlan.model_validate(data)
    if not plan.steps:
        fallback_goal = plan.goal or plan.summary or "按纲领执行"
        plan.steps = [_single_step(fallback_goal, plan.summary or plan.goal or fallback_goal)]
    if max_steps and len(plan.steps) > max_steps:
        plan.open_questions.append(
            f"架构段给出 {len(plan.steps)} 步，超过上限，已截断为 {max_steps} 步执行。"
        )
        plan.steps = plan.steps[:max_steps]
    return plan.normalize()


def _single_step(goal: str, summary: str):
    from app.schemas.plan import PlanStep

    return PlanStep(id=1, title=goal[:40], goal=goal, deliverables=[], acceptance=[summary or goal])
