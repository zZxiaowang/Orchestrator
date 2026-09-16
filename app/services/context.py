"""执行段上下文装配：分层 + 预算裁剪 + 摘要化。

为什么不让模型自己压缩：

* 模型压缩是**付费且不可控**的（在长上下文里反复总结，token 反而更多）；
* 压缩结果不可复现，容易丢关键信息（路径、接口、未完成事项）。

这里改成"编排器做确定性裁剪"：

1. **稳定前缀逐字不变**（系统提示 → 任务 → 补充简报 → 纲领摘要），
   变量段全部后移（当前步骤 → 相关文件 → 已完成交接 → 文件树）。
   顺序一变，网关的前缀缓存就命中不了——所以文件树这类"每步都在变"的内容
   必须放在最后，绝不插在稳定段中间；
2. 超预算时**按优先级丢**：先丢文件内容（并明确告诉模型"可用 need_files 索取"），
   再压文件树，最后把"最早的历史步骤"折成一行；
3. **当前步骤与纲领决策永不裁剪**——这是本步的硬需求；
4. 文件内容不再"能塞就塞"：默认只给**结构索引 + 头尾节选**，全文走 need_files。
   自开发场景实测：两个大文件曾经占单步上下文的 84%（12k 字符），
   而模型通常只需要改其中一处。
"""

from __future__ import annotations

import ast
import re
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

#: 超过这个长度就不再整篇注入，改成"结构索引 + 头尾节选"
STRUCTURE_THRESHOLD = 1200
#: 结构索引最多列多少条符号
STRUCTURE_ENTRIES = 24
#: 结构索引的字符上限
STRUCTURE_MAX_CHARS = 420


@dataclass
class ContextPacket:
    """一次执行段调用要发送的内容（已按预算装配好）。"""

    system: str
    brief: str
    current: str
    completed: str = ""
    #: 按触发词挑出来的 skill 指令（项目内启用、按需注入）
    skills: str = ""
    #: 可用工具（MCP）：只给名字与一句说明，详细 schema 按需再问
    tools: str = ""
    files: str = ""
    #: 文件树（每步都会变，放在最后）
    tree: str = ""
    omitted: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def user_content(self) -> str:
        # 顺序：稳定前缀 → 当前步骤 → 相关文件 → 已完成交接 → 文件树。
        # 后三项每一步都会变（工作区在改），放在最后，前面的稳定前缀才能命中缓存。
        parts = [
            self.brief,
            self.current,
            self.skills,
            self.tools,
            self.files,
            self.completed,
            self.tree,
        ]
        return "\n\n".join(part for part in parts if part)

    @property
    def chars(self) -> int:
        return len(self.system) + len(self.user_content)


def plan_digest(plan: ArchitecturePlan, *, current_step_id: int, max_chars: int = 1200) -> str:
    """把整份纲领压成"够用"的摘要：目标 + 思路 + 其他步骤标题。

    刻意**不带** principles / components / risks —— 那些是给人看的架构决策，
    对"这一步怎么写代码"帮助很小，却是每一步都要重复付费的固定成本。
    """
    lines: list[str] = [f"目标：{plan.goal.strip()}"]
    if plan.summary.strip():
        lines.append(f"思路：{plan.summary.strip()[:220]}")
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


#: ``need_files`` 里的「路径:起始行-结束行」写法（行号从 1 开始，结束行可省略）
FILE_RANGE = re.compile(r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d*))?$")

#: 只写起始行（``app/x.py:120``）时默认往后带多少行
DEFAULT_RANGE_LINES = 120


def parse_file_request(raw: str) -> tuple[str, tuple[int, int] | None]:
    """解析执行段索要的文件：``app/x.py`` 或 ``app/x.py:120-200``。

    为什么需要行区间：大文件在上下文里只给「结构索引 + 头尾节选」，中间段看不到，
    而修改已有文件又要求 ``edits`` 里的 search 是**原文片段**——模型只能靠猜。
    有了区间写法，它能先看结构索引，再精确索取那一段。

    只有「冒号后确实是行号」才算区间，所以 ``C:/x.py`` 这类路径不受影响。
    """

    text = str(raw or "").strip()
    match = FILE_RANGE.match(text)
    if not match:
        return text, None
    start = int(match.group("start"))
    if start < 1:
        return text, None
    end_raw = match.group("end")
    end = int(end_raw) if end_raw else start + DEFAULT_RANGE_LINES - 1
    return match.group("path"), (start, max(start, end))


def slice_lines(
    content: str, span: tuple[int, int], *, max_chars: int, path: str = ""
) -> tuple[str, str]:
    """取指定行区间，返回 ``(原文, 说明)``。

    刻意**不加行号前缀**：``edits`` 的 search 必须与文件原文一致，
    加了前缀反而会让模型把前缀一起写进 search。行号放在说明里。
    """

    lines = content.splitlines()
    total = len(lines)
    start, end = span
    start = max(1, min(start, total) if total else 1)
    end = max(start, min(end, total) if total else start)
    body = "\n".join(lines[start - 1 : end])
    note = f"{path or '文件'} 的第 {start}-{end} 行（本文件共 {total} 行）"
    if len(body) > max_chars:
        body = clip(body, max_chars)
        note += "；这一段太长，只给了前一部分，可以再要更小的区间"
    return body, note


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
        tree_full: bool = True,
        skills_section: str = "",
        tools_section: str = "",
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

        # ── 能力段：按触发词挑出来的 skill 指令（项目内启用、按需注入，别白烧上下文）──
        skills_block = ""
        if skills_section.strip():
            skills_block = clip(skills_section.strip(), min(2400, max(0, remaining)))
            remaining -= len(skills_block)

        # ── 能力段：可用工具（只给名字 + 一句说明，别把 schema 全塞进来）──
        tools_block = ""
        if tools_section.strip():
            tools_block = clip(tools_section.strip(), min(1200, max(0, remaining)))
            remaining -= len(tools_block)

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
            # 索取方式只说一次（每个文件都说一遍纯属浪费 token）：
            # 长文件给「结构索引 + 头尾节选」，中间段按行号索取。
            sections.append(
                "## 相关文件当前内容\n（长文件给「结构索引 + 头尾节选」；"
                "要看中间某段，用 need_files 索取 `路径:起始行-结束行`，行号见结构索引）\n"
                + files_body
            )
        if omitted:
            marker = OMIT_MARK.format(count=len(omitted))
            sections.append(marker + "：" + "、".join(dict.fromkeys(omitted))[:400])
        files_block = "\n\n".join(sections)

        # ── 变量段：已完成步骤的交接日志（超长先把最早的压成一行） ──
        log_block = f"## 已完成步骤\n{self._completed_log(steps)}"
        remaining -= len(log_block)

        # ── 变量段：文件树（每步都在变，放最后；后续步骤只给一小段） ──
        entries = list(tree)[: self.tree_limit] if tree_full else list(tree)[:20]
        tree_text = "\n".join(entries) or "（空工作区）"
        tree_cap = (
            min(self.tree_max_chars, max(0, remaining))
            if tree_full
            else min(600, max(0, remaining))
        )
        tree_block = f"## 工作区文件\n{clip(tree_text, tree_cap)}"
        if not tree_full:
            tree_block += "\n（只列前 20 项；需要别的文件请用 need_files 索取）"
        if len(tree_text) > tree_cap:
            omitted.append("部分工作区文件路径")

        return ContextPacket(
            system=system,
            # 稳定前缀：这几块在一次运行里逐字不变，顺序也不要动（网关前缀缓存）
            brief="\n\n".join(part for part in (task_block, brief_block, plan_block) if part),
            current="\n\n".join(part for part in (notes_block, current_block) if part),
            completed=log_block,
            skills=skills_block,
            tools=tools_block,
            files=files_block,
            tree=tree_block,
            omitted=omitted,
            stats={
                "budget": self.budget_chars,
                "task": len(task_block),
                "brief": len(brief_block),
                "plan": len(plan_block),
                "tree": len(tree_block),
                "completed": len(log_block),
                "skills": len(skills_block),
                "tools": len(tools_block),
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
        if not folded:
            # 已完成步骤本来就不多（≤3 条），但每条交接都很长：
            # 这时没有"更早的步骤"可折叠（原来直接取 folded[0] 会 IndexError），
            # 改成从最近往回留，直到用完预算，并注明省略了几条。
            kept: list[str] = []
            used = 0
            for entry in reversed(entries):
                if used + len(entry) + 1 > self.log_max_chars:
                    break
                kept.insert(0, entry)
                used += len(entry) + 1
            dropped = len(entries) - len(kept)
            if not kept:
                # 连一条完整的都放不下：至少给最近一条的截断版（它最有参考价值）
                return clip(entries[-1], self.log_max_chars)
            prefix = f"（更早的 {dropped} 条交接已省略）\n" if dropped else ""
            return prefix + "\n".join(kept)
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
        # 刻意**不**再打包"前面步骤改过的文件"：那些内容下一步多半用不到，
        # 却因为"能塞就塞"把单步上下文顶到上万字符（自开发场景实测占 84%）。
        # 需要时模型可以自己用 need_files 索取，或在 handoff 里说明。

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
            block = self._file_block(path, content, per_file)
            if used + len(block) > budget:
                omitted.append(f"文件 {path}")
                continue
            chunks.append(block)
            used += len(block)
        return "\n\n".join(chunks), omitted

    def _file_block(self, path: str, content: str, per_file: int) -> str:
        """长文件给"结构索引 + 头尾节选"，短文件给全文。

        索引让模型知道"文件里有哪些函数/段落、大概在哪"，需要细节时再按路径索取全文；
        比直接塞 6k 字符的节选更有用，也更便宜。
        """

        if len(content) <= STRUCTURE_THRESHOLD:
            return f"### {path}\n```\n{content}\n```"

        index = structure_index(path, content)
        # 索引占一部分预算，剩下给头尾节选
        index_cap = min(STRUCTURE_MAX_CHARS, max(120, per_file // 3))
        if len(index) > index_cap:
            index = clip(index, index_cap)
        excerpt = clip_head_tail(content, max(200, per_file - len(index) - 40), path=path)
        parts = [f"### {path}（结构索引）\n```\n{index}\n```"]
        parts.append(
            f"### {path}（节选：开头与结尾）\n```\n{excerpt}\n```\n"
            f"需要完整内容或某段代码，请用 need_files 索取 `{path}`。"
        )
        return "\n\n".join(parts)


def structure_index(path: str, content: str) -> str:
    """给文件列一份"有什么、在第几行"的索引（尽量短、尽量准）。

    * ``.py``：用 ast 取顶层函数/类与方法名（解析失败就退化成正则）；
    * ``.js/.ts/.mjs/.jsx``：函数、类、常见 const/箭头函数，以及 ``/* ── 段落 ── */`` 分隔；
    * ``.css``：顶层选择器；
    * 其他：只给前几行（当作"文件开头"提示）。
    """

    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    lines = content.splitlines()
    entries: list[str] = []

    if suffix == "py":
        try:
            tree = ast.parse(content)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(node, ast.ClassDef) else "def"
                    entries.append(f"L{node.lineno} {kind} {node.name}")
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, "module", None) or ",".join(
                        alias.name for alias in node.names[:3]
                    )
                    entries.append(f"L{node.lineno} import {module}")
        except SyntaxError:
            entries = _regex_index(lines, suffix)
    elif suffix in ("js", "mjs", "cjs", "ts", "jsx", "tsx"):
        entries = _regex_index(lines, suffix)
    elif suffix == "css":
        for number, line in enumerate(lines, start=1):
            text = line.strip()
            if text.endswith("{") and not text.startswith(("@", "}", ".")) and ":" not in text[:20]:
                continue
            if text.endswith("{") and text and not line.startswith((" ", "\t")):
                entries.append(f"L{number} {text[:-1].strip()[:60]}")
    else:
        entries = [f"L{n} {line.strip()[:80]}" for n, line in enumerate(lines[:6], start=1)]

    if not entries:
        entries = [f"L{n} {line.strip()[:80]}" for n, line in enumerate(lines[:6], start=1)]
    return "\n".join(entries[:STRUCTURE_ENTRIES])


_JS_PATTERNS = (
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function"
    ),
)
_JS_SECTION = re.compile(r"^\s*/\*\s*[─\-=]{2,}\s*(.+?)\s*[─\-=]{2,}\s*\*/")


def _regex_index(lines: list[str], suffix: str) -> list[str]:
    entries: list[str] = []
    for number, line in enumerate(lines, start=1):
        section = _JS_SECTION.match(line)
        if section:
            entries.append(f"L{number} ── {section.group(1)[:50]}")
            continue
        for pattern in _JS_PATTERNS:
            match = pattern.match(line)
            if match:
                entries.append(f"L{number} {match.group(1)}")
                break
    return entries
