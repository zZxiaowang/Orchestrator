"""普通对话的长上下文管理：滚动窗口、批量折叠、摘要失败退化、超阈值开新会话。

这一层是"省 token"的核心，所以每条规则都要钉住：窗口是按轮数+字符双限、折叠是批量的、
摘要失败绝不能阻断回答、不开新会话时必须把溢出的也带上（不能平白丢上下文）。
"""

from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.services.chat_context import (
    build_context_messages,
    fallback_digest,
    needs_new_session,
    plan_chat_context,
    split_window,
)


def _settings(**overrides) -> Settings:
    base = {
        "relay_base_url": "https://relay.test/v1",
        "relay_api_key": "sk-test-1234567890",
    }
    base.update(overrides)
    return Settings(**base)


def _history(turns: int, *, chars: int = 20) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for index in range(turns):
        out.append(("user", f"问题{index}" + "x" * chars))
        out.append(("assistant", f"回答{index}" + "y" * chars))
    return out


def test_split_window_keeps_recent_turns_within_both_limits():
    history = _history(10, chars=10)
    kept, overflow = split_window(history, window_turns=3, window_chars=10000)
    assert len(kept) == 6  # 3 轮 = 6 条
    assert kept == history[-6:]
    assert overflow == history[:-6]

    # 字符上限更紧时按字符截（从最新往回取）
    tight, overflow2 = split_window(history, window_turns=10, window_chars=60)
    assert len(tight) < len(history)
    assert overflow2 == history[: len(history) - len(tight)]
    assert all(item in history for item in tight)


def test_fallback_digest_is_deterministic_and_bounded():
    turns = [("user", "x" * 500), ("assistant", "y" * 500)]
    digest = fallback_digest("旧摘要", turns, limit=120)
    assert len(digest) <= 120
    assert digest.startswith("旧摘要")
    assert digest == fallback_digest("旧摘要", turns, limit=120)


def test_plan_folds_a_batch_and_reports_it():
    """够一批就折叠：摘要来自模型，历史只剩窗口内的原文。"""

    calls: list[tuple[str, int]] = []

    async def summarize(previous: str, turns):
        calls.append((previous, len(turns)))
        return f"合并摘要（长度{len(previous)}+{len(turns)}）"

    history = _history(8, chars=10)  # 16 条 = 8 轮
    plan = asyncio.run(
        plan_chat_context(
            history,
            settings=_settings(chat_window_turns=4, chat_fold_batch=4, chat_window_chars=10000),
            previous_summary="旧摘要",
            total_folded=2,
            summarize=summarize,
        )
    )
    assert calls == [("旧摘要", 8)]  # 8 条溢出 = 4 轮，正好一批
    assert plan.folded_now == 8
    assert plan.total_folded == 10
    assert plan.summary.startswith("合并摘要")
    assert len(plan.history) == 8  # 只发窗口内的 4 轮
    assert plan.overflow == history[:8]
    assert plan.degraded is False


def test_plan_keeps_everything_when_batch_is_not_reached():
    """不够一批就先带着发：宁可这一轮稍贵，也不平白丢上下文。"""

    async def summarize(previous: str, turns):  # pragma: no cover - 不该被调用
        raise AssertionError("不到一批不该调摘要模型")

    history = _history(5, chars=10)  # 10 条
    plan = asyncio.run(
        plan_chat_context(
            history,
            settings=_settings(chat_window_turns=4, chat_fold_batch=6, chat_window_chars=10000),
            summarize=summarize,
        )
    )
    assert plan.folded_now == 0
    assert plan.history == history  # 一条都没丢
    assert plan.summary == ""


def test_plan_degrades_when_summary_model_fails():
    """摘要失败绝不阻断回答：退化成确定性折叠，并标记 degraded。"""

    async def broken(previous: str, turns):
        raise RuntimeError("网关 502")

    plan = asyncio.run(
        plan_chat_context(
            _history(6, chars=10),
            settings=_settings(chat_window_turns=2, chat_fold_batch=2, chat_window_chars=10000),
            previous_summary="旧摘要",
            summarize=broken,
        )
    )
    assert plan.degraded is True
    assert plan.summary.startswith("旧摘要")
    assert plan.folded_now > 0
    assert len(plan.history) == 4


def test_disabled_feature_sends_full_history():
    """关掉总开关 = 回到旧行为（全部历史原样发），不做任何折叠。"""

    history = _history(20, chars=10)
    plan = asyncio.run(
        plan_chat_context(
            history,
            settings=_settings(chat_context_enabled=False),
            previous_summary="不该用到",
        )
    )
    assert plan.enabled is False
    assert plan.history == history
    assert plan.folded_now == 0
    assert plan.summary == "不该用到"


def test_needs_new_session_follows_summary_size_and_switch():
    settings = _settings(chat_summary_max_chars=500)
    assert needs_new_session("x" * 400, settings) is False
    assert needs_new_session("x" * 600, settings) is True
    # 关掉自动开新会话：只折叠，不切
    assert (
        needs_new_session("x" * 600, _settings(chat_summary_max_chars=500, chat_auto_split=False))
        is False
    )
    # 关掉总开关：什么都不做
    assert (
        needs_new_session(
            "x" * 600, _settings(chat_summary_max_chars=500, chat_context_enabled=False)
        )
        is False
    )


def test_context_messages_order_is_stable_for_prefix_cache():
    """顺序固定：system → 摘要 → 最近原文 → 当前提问（便于命中前缀缓存）。"""

    plan = asyncio.run(
        plan_chat_context(
            _history(1, chars=5),
            settings=_settings(chat_window_turns=4, chat_fold_batch=4, chat_window_chars=10000),
            previous_summary="这是摘要",
        )
    )
    messages = build_context_messages(system="SYS", brief="BR", plan=plan, question="现在的问题")
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[1]["role"] == "user" and "BR" in messages[1]["content"]
    assert "这是摘要" in messages[2]["content"]
    assert messages[-1] == {"role": "user", "content": "现在的问题"}


def test_sent_chars_counts_everything_that_goes_out():
    plan = asyncio.run(
        plan_chat_context(
            _history(1, chars=3),
            settings=_settings(chat_window_turns=4, chat_fold_batch=4, chat_window_chars=10000),
            previous_summary="摘要",
        )
    )
    sent = plan.sent_chars("问题", system_chars=10)
    assert sent == 10 + len("摘要") + sum(len(c) for _, c in plan.history) + len("问题")
