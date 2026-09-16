"""普通对话的长上下文管理：滚动窗口 + 累进摘要 + 超阈值开新会话。

为什么不在项目运行里也这么做：执行段的每步上下文必须是**确定性裁剪**（预算、文件树、
交接日志），要可复现、可审计；而普通对话的目标只是"接着聊"，允许用模型把历史压成
一段背景。两件事分开，语义不会打架。

三层结构（顺序固定，便于命中网关前缀缓存）::

    system(稳定) → 承接摘要(滚动更新) → 最近 K 轮原文 → 当前提问

关键取舍：

* **折叠是批量的**（默认每次 4 轮）：每轮都调一次摘要模型既贵又抖；
* **有水位线**：已经折进摘要的那几条不再重复摘要——历史只追加，所以
  "已折叠条数"就是一个稳定前缀长度。没有它，每一轮都会把同一批老历史重摘一遍，
  越聊越贵，摘要也越压越失真；
* **不够一批就先带着发**，宁可这一轮稍贵，也不要平白丢掉上下文；
* **摘要失败不阻断回答**：退化为确定性折叠（每条取前 200 字），并在提示里写明；
* **开新会话是摘要撑不住时的重启阀**（摘要累计 > 上限），不是主要省 token 手段——
  用户心智里的"一段对话"不该被悄悄切断，所以新会话必须带承接链、旧会话必须留着。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings

# 确定性折叠（摘要模型不可用）时，每条消息最多取多少字
FALLBACK_CHARS_PER_MESSAGE = 200


@dataclass
class ChatContextPlan:
    """一次回答要用到的上下文装配结果。"""

    #: 实际发出去的历史轮次（(role, content)，按时间顺序）
    history: list[tuple[str, str]] = field(default_factory=list)
    #: 合并后的承接摘要（可能来自上一轮）
    summary: str = ""
    #: 本次新折叠的**条数**（0 = 没有折叠）
    folded_now: int = 0
    #: 累计折叠条数（同时是"哪几条已经进摘要了"的水位线）
    total_folded: int = 0
    #: 本次被折叠掉的条数（供界面展开查看）
    overflow: list[tuple[str, str]] = field(default_factory=list)
    #: 摘要生成失败，用了确定性折叠
    degraded: bool = False
    #: 功能是否生效（关闭时为 False，行为与旧版一致）
    enabled: bool = True

    def sent_chars(self, question: str, *, system_chars: int = 0) -> int:
        total = system_chars + len(self.summary) + len(question)
        total += sum(len(content) for _, content in self.history)
        return total

    def as_event(self) -> dict[str, Any]:
        return {
            "folded_now": self.folded_now,
            "total_folded": self.total_folded,
            "summary_chars": len(self.summary),
            "degraded": self.degraded,
            "kept_turns": len(self.history) // 2,
            "summary_preview": self.summary[:200],
        }


def split_window(
    history: Sequence[tuple[str, str]],
    *,
    window_turns: int,
    window_chars: int,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """把历史切成 ``(最近窗口, 溢出部分)``；两段都按时间顺序。"""

    turns = max(1, int(window_turns))
    chars = max(200, int(window_chars))
    kept: list[tuple[str, str]] = []
    used = 0
    # 从最新往回取：先满足"最近 K 轮"，再满足字符预算
    for role, content in reversed(list(history)):
        if len(kept) >= turns * 2:
            break
        if kept and used + len(content) > chars:
            break
        kept.insert(0, (role, content))
        used += len(content)
    overflow = list(history)[: len(history) - len(kept)]
    return kept, overflow


def fallback_digest(previous: str, turns: Sequence[tuple[str, str]], *, limit: int = 300) -> str:
    """摘要模型不可用时的确定性折叠：每条截前 200 字，拼成一行。"""

    pieces: list[str] = []
    if previous.strip():
        pieces.append(previous.strip())
    for role, content in turns:
        speaker = "用户" if role == "user" else "助手"
        text = " ".join((content or "").split())[:FALLBACK_CHARS_PER_MESSAGE]
        if text:
            pieces.append(f"{speaker}：{text}")
    joined = " / ".join(pieces)
    if len(joined) <= limit:
        return joined
    return joined[: max(0, limit - 1)] + "…"


async def plan_chat_context(
    history: Sequence[tuple[str, str]],
    *,
    settings: Settings,
    previous_summary: str = "",
    total_folded: int = 0,
    summarize=None,
) -> ChatContextPlan:
    """装配本轮上下文：该折叠就折叠，不够一批就先带着发。

    ``summarize`` 是 ``async (previous, turns) -> str`` 的可注入函数（测试里用假的）。
    """

    pairs = [(role, content) for role, content in history if (content or "").strip()]
    if not getattr(settings, "chat_context_enabled", True):
        return ChatContextPlan(
            history=pairs,
            summary=previous_summary,
            total_folded=total_folded,
            enabled=False,
        )

    kept, overflow = split_window(
        pairs,
        window_turns=getattr(settings, "chat_window_turns", 12),
        window_chars=getattr(settings, "chat_window_chars", 6000),
    )
    # 水位线：``total_folded`` 是"已经被折进摘要的历史条数"。历史只追加、窗口只保留最新，
    # 所以它就是溢出部分的前缀长度——只摘前缀之后新增的那些。
    watermark = max(0, min(int(total_folded or 0), len(overflow)))
    pending = overflow[watermark:]
    batch = max(1, int(getattr(settings, "chat_fold_batch", 4)))
    if len(pending) < batch:
        # 不够一批就先带着发：宁可这一轮稍贵，也不平白丢上下文
        return ChatContextPlan(
            history=[*overflow, *kept],
            summary=previous_summary,
            total_folded=watermark,
            enabled=True,
        )

    summary = previous_summary
    degraded = False
    generated = ""
    if summarize is not None:
        try:
            generated = await summarize(previous_summary, pending)
        except Exception:  # noqa: BLE001 - 摘要失败绝不能阻断回答
            generated = ""
    if not generated:
        degraded = True
        summary = fallback_digest(previous_summary, pending)
    else:
        summary = generated

    return ChatContextPlan(
        history=kept,
        summary=summary,
        folded_now=len(pending),
        total_folded=watermark + len(pending),
        overflow=pending,
        degraded=degraded,
        enabled=True,
    )


def needs_new_session(summary: str, settings: Settings) -> bool:
    """摘要累计到一定长度就该重启一个会话：再压下去就是"摘要的摘要"，开始失真。"""

    if not getattr(settings, "chat_context_enabled", True):
        return False
    if not getattr(settings, "chat_auto_split", True):
        return False
    limit = max(200, int(getattr(settings, "chat_summary_max_chars", 2000)))
    return len(summary or "") > limit


def build_context_messages(
    *,
    system: str,
    brief: str,
    plan: ChatContextPlan,
    question: str,
) -> list[dict[str, Any]]:
    """把装配结果变成消息序列（system → 摘要 → 最近原文 → 当前提问）。"""

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    if brief.strip():
        messages.append({"role": "user", "content": f"（背景简报）\n{brief.strip()[:800]}"})
    if plan.summary.strip():
        messages.append(
            {
                "role": "user",
                "content": (
                    "（以下是这段对话更早内容的摘要，供你参考，不要复述它）\n"
                    f"{plan.summary.strip()}"
                ),
            }
        )
    for role, content in plan.history:
        messages.append(
            {"role": "assistant" if role == "assistant" else "user", "content": content}
        )
    messages.append({"role": "user", "content": question})
    return messages
