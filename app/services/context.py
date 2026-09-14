"""执行段上下文装配：分层 + 预算裁剪 + 摘要化。

为什么不让模型自己压缩：

* 模型压缩是**付费且不可控**的（在长上下文里反复总结，token 反而更多）；
* 压缩结果不可复现，容易丢关键信息（路径、接口、未完成事项）。

这里改成"编排器做确定性裁剪"：

1. **稳定段在前**（系统提示 → 任务 → 纲领摘要 → 文件树），变量段在后
   （已完成交接日志 → 当前步骤 → 相关文件），对支持前缀缓存的网关更省钱；
2. 超预算时**按优先级丢**：先丢文件内容（并明确告诉模型"可用 need_files 索取"），
   再压文件树，最后把"最早的历史步骤"折成一行；
3. **当前步骤与纲领决策永不裁剪**——这是本步的硬需求。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.core.errors import AppError
from app.schemas.plan import ArchitecturePlan
from app.schemas.run import RunStep, StepStatus
from app.services.verify import effective_checks

OMIT_MARK = "…（此处省略 {count} 项，如确需查看请用 need_files 索取路径）"
FILE_TAIL_MARK = (
    "\n…（本文件较长，中间省略 {skipped} 字符；需要完整内容请用 need_files 索取 {path}）\n"
)


@dataclass
class ContextPacket:
    """一次执行段调用要发送的内容（已按预算装配好）。"""

    system: str
    brief: str
    current: str
    completed: str = ""
    files: str = ""
    omitted: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def user_content(self) -> str:
        # 顺序：稳定段 → 当前步骤 → 已完成交接 → 相关文件。
        # 当前步骤紧跟纲领之后，模型注意力落在"这一步要做什么"上；
        # 变化最频繁的内容放在最后，前面仍可命中网关的前缀缓存。
        parts = [self.brief, self.current, self.completed]
        if self.files:
            parts.append(self.files)
        return "\n\n".join(part for part in parts if part)

    @property
    def chars(self) -> int:
        return len(self.system) + len(self.user_content)


def plan_digest(plan: ArchitecturePlan, *, current_step_id: int, max_chars: int = 1200) -> str:
    """把整份纲领压成"够用"的摘要：决策 + 组件 + 其他步骤一行标题。

    完整纲领 JSON 往往上千字符，而单步执行其实只需要：为什么这么拆（原则/组件）+
    自己这一步 + 其他步骤的标题（避免重复劳动）。
    """
    lines: list[str] = [f"目标：{plan.goal.strip()}"]
    if plan.summary.strip():
        lines.append(f"思路：{plan.summary.strip()[:300]}")
    if plan.principles:
        lines.append("原则：" + "；".join(item[:60] for item in plan.principles[:5]))
    if plan.components:
        lines.append("组件：" + "；".join(component.name[:40] for component in plan.components[:8]))
    others = [f"{step.id}.{step.title}" for step in plan.steps if step.id != current_step_id]
    if others:
        lines.append("其他步骤（本次只做当前步骤）：" + " | ".join(others[:12]))
    text = "\n".join(lines)
    return clip(text, max_chars)


def clip(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def clip_head_tail(text: str, max_chars: int, *, path: str) -> str:
    """长文件取头尾：开头有 import/声明，结尾常有最近改动。

    返回值长度**严格不超过** ``max_chars``（省略标记也算在内），
    否则调用方按"预算刚好等于块大小"来装包时会整块放不下。
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    # 先按"数字最多"的情况估出标记长度，再分配头尾
    marker_len = len(FILE_TAIL_MARK.format(skipped=len(text), path=path))
    budget = max(0, max_chars - marker_len)
    head = int(budget * 0.6)
    tail = budget - head
    skipped = len(text) - head - tail
    marker = FILE_TAIL_MARK.format(skipped=skipped, path=path)
    result = text[:head] + marker + (text[-tail:] if tail else "")
    return result[:max_chars] if len(result) > max_chars else result


class StepContextBuilder:
    """按预算装配单步上下文。"""

    def __init__(
        self,
        *,
        budget_chars: int = 24000,
        file_max_chars: int = 6000,
        files_max_chars: int = 6000,
        tree_max_chars: int = 2000,
        log_max_chars: int = 1200,
        brief_max_chars: int = 4000,
        tree_limit: int = 120,
    ) -> None:
        self.budget_chars = max(4000, budget_chars)
        self.file_max_chars = max(400, file_max_chars)
        self.files_max_chars = max(400, files_max_chars)
        self.tree_max_chars = max(200, tree_max_chars)
        self.log_max_chars = max(200, log_max_chars)
        self.brief_max_chars = max(200, brief_max_chars)
        self.tree_limit = max(10, tree_limit)

    def build(
        self,
        *,
        system: str,
        task: str,
        plan: ArchitecturePlan | None,
        step: RunStep,
        steps: Sequence[RunStep],
        tree: Sequence[str],
        read_file,
        extra_paths: Iterable[str] = (),
        user_notes: Sequence[str] = (),
        brief_text: str = "",
    ) -> ContextPacket:
        omitted: list[str] = []
        remaining = self.budget_chars - len(system)

        # ── 稳定段：任务 + 纲领摘要 ──
        notes = [note.strip() for note in user_notes if note and note.strip()]
        notes_block = (
            "## 用户补充（优先遵循）\n" + "\n".join(f"- {clip(note, 400)}" for note in notes[-5:])
            if notes
            else ""
        )
        task_block = f"## 总需求\n{clip(task.strip(), 1200)}"
        brief_block = (
            f"## 背景资料（前期沟通简报）\n{clip(brief_text.strip(), self.brief_max_chars)}"
            if brief_text.strip()
            else ""
        )
        plan_block = (
            f"## 纲领摘要（不得更改其中决策）\n{plan_digest(plan, current_step_id=step.id)}"
            if plan is not None
            else ""
        )
        remaining -= len(task_block) + len(brief_block) + len(plan_block) + len(notes_block)

        # ── 稳定段：文件树（按预算给一部分） ──
        tree_text = "\n".join(list(tree)[: self.tree_limit]) or "（空工作区）"
        tree_block = (
            f"## 工作区文件\n{clip(tree_text, min(self.tree_max_chars, max(0, remaining // 3)))}"
        )
        if len(tree_text) > len(tree_block):
            omitted.append("部分工作区文件路径")
        remaining -= len(tree_block)

        # ── 变量段：已完成步骤的交接日志（超长先把最早的压成一行） ──
        log_block = f"## 已完成步骤\n{self._completed_log(steps)}"
        remaining -= len(log_block)

        # ── 变量段：当前步骤（永不裁剪） ──
        current_block = self._current_step(step)
        remaining -= len(current_block)

        # ── 变量段：相关文件内容（预算不够就先丢它） ──
        files_body = ""
        file_budget = min(remaining, self.files_max_chars)
        if file_budget > 500:
            files_body, file_omitted = self._pack_files(
                step=step,
                steps=steps,
                read_file=read_file,
                extra_paths=extra_paths,
                budget=file_budget,
            )
            omitted.extend(file_omitted)
        else:
            omitted.append("全部相关文件内容")

        sections: list[str] = []
        if files_body:
            sections.append(f"## 相关文件当前内容\n{files_body}")
        if omitted:
            marker = OMIT_MARK.format(count=len(omitted))
            sections.append(marker + "：" + "、".join(dict.fromkeys(omitted))[:400])
        files_block = "\n\n".join(sections)

        return ContextPacket(
            system=system,
            brief="\n\n".join(
                part for part in (task_block, brief_block, plan_block, tree_block) if part
            ),
            current="\n\n".join(part for part in (notes_block, current_block) if part),
            completed=log_block,
            files=files_block,
            omitted=omitted,
            stats={
                "budget": self.budget_chars,
                "task": len(task_block),
                "brief": len(brief_block),
                "plan": len(plan_block),
                "tree": len(tree_block),
                "completed": len(log_block),
                "current": len(current_block),
                "files": len(files_block),
            },
        )

    # ── 内部 ──

    def _current_step(self, step: RunStep) -> str:
        checks = effective_checks(step.checks, step.deliverables)
        check_line = ""
        if checks:
            items = "；".join((item.label or f"{item.type}: {item.path}") for item in checks)
            check_line = f"\n客观验收（不通过就不算完成）：{items}"
        return (
            f"## 当前步骤（第 {step.id} 步）\n"
            f"标题：{step.title}\n"
            f"目标：{step.goal}\n"
            f"交付物：{', '.join(step.deliverables) or '（未指定）'}\n"
            f"验收标准：{'；'.join(step.acceptance) or '（未指定）'}"
            f"{check_line}"
        )

    def _completed_log(self, steps: Sequence[RunStep]) -> str:
        done = [item for item in steps if item.status == StepStatus.DONE]
        if not done:
            return "（这是第一步）"
        entries = [
            f"{item.id}. {item.title} → {item.handoff or item.summary or '已完成'}" for item in done
        ]
        text = "\n".join(entries)
        if len(text) <= self.log_max_chars:
            return text
        # 超限：保留最近 3 条完整，其余折成一行
        keep = entries[-3:]
        folded = entries[:-3]
        head = f"（前 {len(folded)} 步已合并：{folded[0].split(' → ')[0]} … {folded[-1].split(' → ')[0]}）"
        text = "\n".join([head, *keep])
        return clip(text, self.log_max_chars)

    def _pack_files(
        self,
        *,
        step: RunStep,
        steps: Sequence[RunStep],
        read_file,
        extra_paths: Iterable[str],
        budget: int,
    ) -> tuple[str, list[str]]:
        candidates: list[str] = []
        candidates.extend(path for path in extra_paths if path)
        candidates.extend(
            item for item in step.deliverables if item and not item.startswith("http")
        )
        for previous in steps:
            if previous.status == StepStatus.DONE:
                candidates.extend(change.path for change in previous.files)

        # 先把"确实存在且读得到"的文件收齐，再**按文件数均分预算**。
        # 否则第一个大文件就会吃满预算，后面的文件全被省略——
        # 模型看不到待改文件的原文，只能反复"再要一次文件"，迟迟不产出改动。
        available: list[tuple[str, str]] = []
        for path in dict.fromkeys(candidates):
            try:
                available.append((path, read_file(path)))
            except AppError:
                continue
        if not available:
            return "", []

        share = max(400, budget // len(available))
        chunks: list[str] = []
        omitted: list[str] = []
        used = 0
        for path, content in available:
            overhead = len(path) + 20
            room = budget - used - overhead
            if room < 200:
                omitted.append(f"文件 {path}")
                continue
            per_file = min(self.file_max_chars, max(200, min(share, room)))
            piece = clip_head_tail(content, per_file, path=path)
            block = f"### {path}\n```\n{piece}\n```"
            if used + len(block) > budget:
                omitted.append(f"文件 {path}")
                continue
            chunks.append(block)
            used += len(block)
        return "\n\n".join(chunks), omitted
