"""补轮/重试时回灌给模型的内容：**只给摘要与片段，不塞整段原始输出**。

真实教训：执行段一步的原始输出可以有 20–50 KB（模型把整个文件写进 JSON 里）。
以前补轮、命令修正、JSON 重试都把这整段 raw 原样拼回提示词，第二轮输入轻松到 5 万字符
（约 2–3 万 token），网关处理输入就要几分钟——用户体感就是"卡住了"。

现在的策略：

* **结构化摘要**：summary / handoff / notes / 拟改文件清单（几行字）；
* **必要时给头尾片段**：JSON 解析失败这类必须看到原文的场景，只给头 1500 + 尾 500 字符，
  并明确告诉模型"可以重新索取文件或直接重写"；
* 需要文件原文的场景走 ``need_files``（有独立预算），不走"把 raw 再发一遍"。
"""

from __future__ import annotations

from app.schemas.step import StepOutput


def step_digest(step) -> str:  # noqa: ANN001 - 避免与 schemas 循环导入
    """从步骤记录本身生成摘要（补轮/命令修正时 ``output`` 已不在作用域里）。"""

    lines: list[str] = []
    summary = " ".join((getattr(step, "summary", "") or "").split())
    if summary:
        lines.append(f"- 你上一轮的说明：{summary[:400]}")
    handoff = " ".join((getattr(step, "handoff", "") or "").split())
    if handoff and handoff != summary:
        lines.append(f"- 交接说明：{handoff[:400]}")
    files = list(getattr(step, "files", []) or [])
    if files:
        names = "、".join(
            f"{item.path}（{item.action}，+{item.additions}/-{item.deletions}）" for item in files[:8]
        )
        lines.append(f"- 你上一轮提交的文件改动：{names}")
    else:
        lines.append("- 你上一轮没有提交文件改动")
    notes = list(getattr(step, "notes", []) or [])
    if notes:
        lines.append("- 备注：" + "；".join(str(note)[:80] for note in notes[:3]))
    return "\n".join(lines) if lines else "（上一轮没有可用的产出记录）"

#: 回灌 raw 时默认保留的头尾字符数
RAW_HEAD_CHARS = 1500
RAW_TAIL_CHARS = 500


def clip_raw(raw: str, *, head: int = RAW_HEAD_CHARS, tail: int = RAW_TAIL_CHARS) -> str:
    """把原始输出裁成头尾片段（中间省略），保证回灌体积可控。"""

    text = raw or ""
    if len(text) <= head + tail:
        return text
    skipped = len(text) - head - tail
    return f"{text[:head]}\n…（中间省略 {skipped} 字符）…\n{text[-tail:]}"


def output_digest(output: StepOutput | None, *, limit: int = 400) -> str:
    """把上一轮产出压成几行摘要（补轮时先给这个，不给原始 JSON）。"""

    if output is None:
        return "（上一轮没有可用的产出记录）"
    lines: list[str] = []
    summary = " ".join((output.summary or "").split())
    if summary:
        lines.append(f"- 你上一轮的说明：{summary[:limit]}")
    handoff = " ".join((output.handoff or "").split())
    if handoff and handoff != summary:
        lines.append(f"- 交接说明：{handoff[:limit]}")
    if output.files:
        names = "、".join(item.path for item in output.files[:8] if item.path)
        lines.append(f"- 你上一轮提交的文件改动：{names}")
    else:
        lines.append("- 你上一轮没有提交文件改动")
    if output.notes:
        lines.append("- 备注：" + "；".join(note[:80] for note in output.notes[:3]))
    return "\n".join(lines) if lines else "（上一轮没有可用的产出记录）"


def retry_block(
    *,
    output: StepOutput | None,
    raw: str,
    reason: str,
    instruction: str,
    raw_limit_head: int = RAW_HEAD_CHARS,
    raw_limit_tail: int = RAW_TAIL_CHARS,
) -> str:
    """补轮/重试时统一使用的回灌块：摘要在前，必要的原文片段在后。"""

    return "\n\n".join(
        part
        for part in (
            f"## {reason}",
            output_digest(output),
            "（你上一轮的完整输出太长，这里只给头尾片段；需要文件原文请用 need_files 索取）",
            "```\n" + clip_raw(raw, head=raw_limit_head, tail=raw_limit_tail) + "\n```",
            instruction,
        )
        if part
    )


def parse_error_block(raw: str, *, head: int = RAW_HEAD_CHARS, tail: int = RAW_TAIL_CHARS) -> str:
    """JSON 解析失败时的回灌：给头尾片段 + 明确要求只输出那一个 JSON。"""

    return (
        "上面的输出不是合法 JSON。以下是它的头尾片段"
        "（中间部分已省略，不需要复原它，直接重新输出那一个 JSON 对象即可）：\n"
        f"```\n{clip_raw(raw, head=head, tail=tail)}\n```"
    )
