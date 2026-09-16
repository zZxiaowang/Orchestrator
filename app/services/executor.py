"""执行段：由 DeepSeek V4 按纲领逐步落地。

约定：执行段**只能**做当前步骤要求的事，产出结构化文件改动与命令建议；
命令默认只展示不执行（由 ``ALLOW_COMMAND_EXECUTION`` 控制）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.core.jsonx import extract_json_object
from app.core.relay import CallStats, RelayClient, unwrap_result
from app.schemas.project import (
    DEFAULT_PROJECT_ID,
    ensure_same_project,
)
from app.schemas.step import StepOutput
from app.services.step_feedback import parse_error_block

EXECUTOR_SYSTEM = """你是一名执行工程师，负责把已经定稿的**纲领**落地成具体产出。

铁律：
1. 只执行当前步骤，不要提前做后面步骤的事，也不要修改架构决策。
2. 只输出一个 JSON 对象，不要解释文字，不要 Markdown 代码块标记。
3. 不臆造不存在的文件路径；改动必须能在给定工作区里落地。
4. **不要复述上下文、不要总结整份纲领**——上下文已按预算裁剪过，重复输出等于浪费额度。

JSON 结构：
{
  "summary": "这一步实际做了什么（1-3 句）",
  "handoff": "给下一步的接力说明（≤200 字）：改了哪些文件、定下了什么约定、还欠什么",
  "need_files": ["需要查看的文件路径（可含 * 通配，或「路径:起始行-结束行」）"],
  "need_reason": "为什么需要这些文件",
  "blocked": false,
  "block_reason": "若 blocked 为 true，说明缺什么信息或权限",
  "files": [
    {"path": "相对路径", "action": "create", "content": "完整文件内容"},
    {"path": "相对路径", "action": "update", "edits": [{"search": "原文片段", "replace": "新片段"}]}
  ],
  "commands": [{"cmd": "建议执行的命令", "why": "为什么需要它"}],
  "notes": ["给使用者的说明、后续注意事项"]
}

写法要求：
- **验证命令会被真的执行**：把"能证明这一步做对了"的命令放进 ``commands``
  （例如跑测试、跑 lint、跑界面自检）。用户在设置里开启命令执行、且命令命中白名单时，
  系统会在工作区内跑它；**失败的输出会回给你**，你可以据此在同一个步骤里继续修，
  改完把想重新验证的命令再写一次。不在白名单里的命令只会被展示，不会执行。
- **产出会被客观验收**：系统执行完本步会逐条检查这些客观条件（文件是否存在、
  是否包含指定内容、本步写过的 Python 文件能否编译 / 能否导入、JSON 是否合法）。
  检查不通过，这一步就不算完成，所以别只写一句"已完成"——该建的文件要真的建出来，
  该写的内容要真的写进去；**写代码时尤其要保证能编译**（缺引号、缩进错、语法错
  一律会被判不通过，而且会把失败原因回灌给你）。
- **上下文不足时先索取，不要猜**：如果缺少必读文件的内容，只输出
  `{"need_files": ["路径"], "need_reason": "原因"}`，系统会把文件内容补给你后再继续。
- `need_files` 要精准（1-3 个文件为宜），不要一次索取整个仓库，也不要重复索取
  已经给过的文件。大文件只给了「结构索引 + 头尾节选」，要看中间某一段就按结构索引
  里的行号索取，例如 `app/services/verify.py:120-200`。
- **新建文件**用 action="create" + content（给出完整内容，不要省略、不要用省略号）。
- **修改已有文件**优先用 action="update" + edits，search 必须是文件中真实存在的原文片段（含缩进），一次替换一处。
- 只有整文件重写才明显更安全时才用 content 全量覆盖。
- 确实无法完成（信息不足、依赖缺失）时，把 blocked 设为 true 并写清 block_reason，不要编造结果。
- 不要输出二进制内容；不要执行破坏性操作（删除仓库、格式化磁盘等）。

语言：summary/handoff/notes 用中文。"""


def build_step_messages(packet) -> list[dict[str, Any]]:
    """把已装配好的上下文包（见 ``services/context.py``）变成消息列表。

    顺序刻意保持"稳定内容在前、易变内容在后"，以便命中网关的前缀缓存。
    """
    return [
        {"role": "system", "content": packet.system},
        {"role": "user", "content": packet.user_content},
    ]


async def run_step(
    client: RelayClient,
    messages: Sequence[dict[str, Any]],
    *,
    model: str,
    on_token: Callable[[str], None] | None = None,
    stats: CallStats | None = None,
) -> tuple[StepOutput, str]:
    buffer: list[str] = []
    async for chunk in client.astream_with_fallback(messages, model=model, stats=stats):
        buffer.append(chunk)
        if on_token:
            on_token(chunk)
    raw = "".join(buffer)
    output = parse_step_output(raw)
    if output is None:
        if on_token:
            on_token("\n\n[执行段输出不是合法 JSON，正在强制重试…]\n")
        retry = list(messages)
        # 只给头尾片段：执行段的原始输出可能有几十 KB，整段回灌会让重试本身变得很慢
        retry.append({"role": "assistant", "content": parse_error_block(raw)})
        retry.append(
            {
                "role": "user",
                "content": "上面的输出不是合法 JSON。请只输出那一个 JSON 对象。",
            }
        )
        if stats is not None:
            stats.retry()
        result = unwrap_result(
            await client.acomplete(retry, model=model, json_mode=True, stats=stats)
        )
        raw = result.text
        output = parse_step_output(raw)
    if output is None:
        # 兜底：把整段输出当成说明文字，不落地任何文件，避免误写
        return (
            StepOutput(
                summary="执行段返回内容无法解析为 JSON，已保留原文供人工处理。",
                blocked=True,
                block_reason="输出不是合法 JSON 结构。",
                notes=[" ".join(raw.split())[:500]],
            ),
            raw,
        )
    return output, raw


def parse_step_output(text: str) -> StepOutput | None:
    data = extract_json_object(text)
    if data is None:
        return None
    return StepOutput.model_validate(data)


# --- 项目边界（第 4 步：执行段只在项目上下文中运行） ---
#: 执行产出上的项目字段名；老记录缺该字段时按契约回落到默认项目。
STEP_PROJECT_FIELD = "project_id"


def step_project_id(output: Any) -> str:
    """读取执行产出所属项目；缺字段时回落 project:default。"""

    raw: Any = None
    if isinstance(output, Mapping):
        raw = output.get(STEP_PROJECT_FIELD)
    else:
        raw = getattr(output, STEP_PROJECT_FIELD, None)
    value = str(raw or "").strip()
    return value or DEFAULT_PROJECT_ID


def ensure_step_project(
    output: Any, requested_project_id: str, *, context_id: str | None = None
) -> None:
    """校验执行产出归属；跨项目读取或在落盘前一律拒绝。"""

    ensure_same_project(step_project_id(output), requested_project_id, context_id=context_id)
