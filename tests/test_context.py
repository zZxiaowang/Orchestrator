"""上下文装配：分层、预算裁剪、摘要化（避免让模型自己压缩）。"""

from __future__ import annotations

from app.schemas.plan import ArchitecturePlan
from app.schemas.run import ChangedFile, RunStep, StepStatus
from app.services.context import (
    StepContextBuilder,
    clip,
    clip_head_tail,
    parse_file_request,
    plan_digest,
    slice_lines,
)


def _plan() -> ArchitecturePlan:
    return ArchitecturePlan.model_validate(
        {
            "goal": "把数据层从同步改成异步",
            "summary": "先加适配层，再逐模块替换调用点，最后清理旧接口。" * 6,
            "principles": ["先兼容后替换", "每步可回滚", "接口签名保持稳定"],
            "components": [
                {"name": "adapter", "responsibility": "兼容同步/异步调用"},
                {"name": "repository", "responsibility": "数据访问"},
            ],
            "steps": [
                {"id": 1, "title": "加适配层", "goal": "新增 adapter"},
                {"id": 2, "title": "替换调用点", "goal": "改调用"},
                {"id": 3, "title": "清理旧接口", "goal": "删除同步入口"},
            ],
        }
    )


def _steps() -> list[RunStep]:
    return [
        RunStep(
            id=1,
            title="加适配层",
            goal="新增 adapter",
            status=StepStatus.DONE,
            summary="新增了 adapter.py，暴露 async 调用",
            handoff="adapter.run_async 已可用；旧入口保留",
        ),
        RunStep(id=2, title="替换调用点", goal="改调用", deliverables=["src/service.py"]),
        RunStep(id=3, title="清理旧接口", goal="删除同步入口"),
    ]


def test_plan_digest_is_much_shorter_than_full_json():
    plan = _plan()
    full = plan.model_dump_json()
    digest = plan_digest(plan, current_step_id=2)
    assert len(digest) < len(full) / 2
    assert "目标：" in digest
    assert "其他步骤" in digest and "1.加适配层" in digest
    assert "2.替换调用点" not in digest
    # 每一步都要重发的固定成本：不重复架构决策（原则/组件/风险）。
    # 它们留给「纲领」面板和 plan.md，执行段不需要为了改一个文件再读一遍。
    assert "原则" not in digest and "组件" not in digest
    assert len(digest) < 400


def test_budget_drops_files_first_and_keeps_core_sections():
    """预算很小时先丢文件，并明确告知"可用 need_files 索取"。"""
    packet = StepContextBuilder(
        budget_chars=4000, file_max_chars=20000, tree_max_chars=500, log_max_chars=400
    ).build(
        system="S" * 100,
        task="改造数据层",
        plan=_plan(),
        step=_steps()[1],
        steps=_steps(),
        tree=[f"src/file_{index}.py" for index in range(200)],
        read_file=lambda path: "X" * 20000,
    )
    assert "## 当前步骤（第 2 步）" in packet.user_content
    assert "## 纲领摘要" in packet.user_content
    assert "need_files" in packet.user_content  # 省略提示
    assert len(packet.user_content) < 8000
    assert packet.omitted


def test_long_file_is_head_tail_trimmed():
    text = "HEAD" + "M" * 5000 + "TAIL"
    clipped = clip_head_tail(text, 400, path="src/big.py")
    assert clipped.startswith("HEAD")
    assert clipped.endswith("TAIL")
    assert len(clipped) <= 400  # 含省略标记在内也不得超限
    assert "src/big.py" in clipped


def test_completed_log_folds_old_steps_when_too_long():
    """步骤很多时：早期交接折成一行，最近的保留。"""

    steps = [
        RunStep(
            id=index,
            title=f"步骤{index}",
            goal="做点什么",
            status=StepStatus.DONE,
            summary="改了若干文件并验证通过" * 5,
        )
        for index in range(1, 9)
    ]
    steps.append(RunStep(id=9, title="当前步", goal="现在要做的事"))
    packet = StepContextBuilder(log_max_chars=300).build(
        system="S",
        task="T",
        plan=_plan(),
        step=steps[-1],
        steps=steps,
        tree=["a.py"],
        read_file=lambda path: "x",
    )
    assert "已合并" in packet.completed
    assert len(packet.completed) <= 400


def test_few_but_long_handoffs_do_not_crash():
    """回归：已完成步骤 ≤3 条、但每条交接都很长时，曾经直接 IndexError
    （folded = entries[:-3] 是空列表，却去取 folded[0]）——真实运行就是这样挂的。"""

    long_handoff = "改了 web/app.js 与 web/styles.css 的若干处；约定：视图层不再持有状态。" * 20
    steps = [
        RunStep(
            id=index,
            title=f"步骤{index}",
            goal="做点什么",
            status=StepStatus.DONE,
            summary=long_handoff,
            handoff=long_handoff,
        )
        for index in range(1, 3)  # 只有 2 条已完成
    ]
    current = RunStep(id=3, title="当前步", goal="现在要做的事")
    steps.append(current)

    packet = StepContextBuilder(log_max_chars=600).build(
        system="S",
        task="T",
        plan=_plan(),
        step=current,
        steps=steps,
        tree=["a.py"],
        read_file=lambda path: "x",
    )
    # 不崩，且长度受控；最近的交接要保留下来
    assert packet.completed
    assert len(packet.completed) <= 900
    assert "步骤2" in packet.completed


def test_clip_helper():
    assert clip("abc", 10) == "abc"
    assert clip("abcdef", 3).startswith("abc")


def test_parse_file_request_understands_line_ranges():
    """``路径:起始行-结束行`` 是给大文件用的：只看那一段，而不是又给一遍头尾。"""

    assert parse_file_request("app/services/verify.py") == ("app/services/verify.py", None)
    assert parse_file_request("app/services/verify.py:120-200") == (
        "app/services/verify.py",
        (120, 200),
    )
    # 只写起始行：默认往后带一段
    path, span = parse_file_request("app/x.py:10")
    assert path == "app/x.py"
    assert span is not None and span[0] == 10 and span[1] > 10
    # 盘符路径不会被误判成区间
    assert parse_file_request("C:/work/x.py") == ("C:/work/x.py", None)
    assert parse_file_request("app/x.py:0-5") == ("app/x.py:0-5", None)


def test_slice_lines_gives_exact_original_text():
    """行区间给的是**原文**（不带行号前缀），edits 的 search 才能命中。"""

    content = "line1\nline2\nline3\nline4\n"

    body, note = slice_lines(content, (2, 3), max_chars=100, path="a.txt")

    assert body == "line2\nline3"
    assert "第 2-3 行" in note and "共 4 行" in note


def test_slice_lines_clamps_out_of_range_requests():
    body, note = slice_lines("a\nb\nc", (2, 99), max_chars=100, path="a.txt")

    assert body == "b\nc"
    assert "第 2-3 行" in note


def test_files_share_budget_equally_instead_of_first_come_first_served():
    """多个待改文件时，预算要均分：不能第一个文件吃满、其余全被省略。"""
    files = {
        "app/services/orchestrator.py": "O" * 30000,
        "app/api/routes.py": "R" * 20000,
        "app/core/relay.py": "L" * 20000,
    }
    step = RunStep(
        id=3,
        title="接线",
        goal="把指标接进去",
        deliverables=list(files),
    )
    packet = StepContextBuilder(
        # 新格式每个文件多一段"结构索引 + 索取提示"，这里给足预算以验证"均分"本身
        budget_chars=30000,
        files_max_chars=15000,
        file_max_chars=4000,
    ).build(
        system="S",
        task="接线",
        plan=_plan(),
        step=step,
        steps=[step],
        tree=list(files),
        read_file=lambda path: files[path],
    )
    # 三个文件都要出现，且都没被省略
    for path in files:
        assert f"### {path}" in packet.files
    assert "全部相关文件内容" not in packet.files
    assert not [item for item in packet.omitted if item.startswith("文件 ")]
    # 均分：没有任何一个文件吃掉全部预算
    assert packet.stats["files"] <= 12000 + 400


def test_long_files_get_structure_index_instead_of_full_text():
    """长文件不再整篇塞进来：给"结构索引 + 头尾节选"，并告诉模型可索取全文。"""

    content = "import os\n\n\ndef alpha():\n    return 1\n\n\nclass Beta:\n    pass\n" + "X" * 5000
    packet = StepContextBuilder(
        budget_chars=24000, files_max_chars=3000, file_max_chars=2400
    ).build(
        system="S",
        task="改 big.py",
        plan=_plan(),
        step=RunStep(id=2, title="改", goal="改", deliverables=["src/big.py"]),
        steps=[],
        tree=["src/big.py"],
        read_file=lambda path: content,
    )
    assert "（结构索引）" in packet.files
    assert "def alpha" in packet.files and "class Beta" in packet.files
    assert "节选" in packet.files
    assert "need_files" in packet.files  # 明确告诉模型怎么拿全文
    # 索引 + 节选必须显著小于文件本身
    assert packet.stats["files"] < len(content)


def test_previous_step_files_are_not_packed_anymore():
    """不再打包"前面步骤改过的文件"：那是 84% 上下文的来源，而多半用不上。"""

    done = RunStep(
        id=1,
        title="改 relay",
        goal="改",
        status=StepStatus.DONE,
        summary="改了 relay.py",
        handoff="relay.py 的 CallStats 增加 output_chars",
    )
    done.files.append(ChangedFile(path="app/core/relay.py", action="update"))
    step = RunStep(id=2, title="改编排器", goal="改", deliverables=["app/services/orchestrator.py"])
    packet = StepContextBuilder(budget_chars=24000, files_max_chars=3000).build(
        system="S",
        task="改",
        plan=_plan(),
        step=step,
        steps=[done, step],
        tree=["app/core/relay.py", "app/services/orchestrator.py"],
        read_file=lambda path: "Y" * 3000,
    )
    assert "app/services/orchestrator.py" in packet.files
    assert "app/core/relay.py" not in packet.files
    # 但它的交接信息仍然在（靠 handoff 传，而不是靠重发文件内容）
    assert "relay.py 的 CallStats" in packet.user_content


def test_brief_is_included_and_clipped():
    """运行级简报要作为稳定背景进入上下文，并且受上限约束。"""
    packet = StepContextBuilder(brief_max_chars=300).build(
        system="S",
        task="T",
        plan=_plan(),
        step=_steps()[1],
        steps=_steps(),
        tree=["a.py"],
        read_file=lambda path: "x",
        brief_text="前期沟通：" + "要点。" * 500,
    )
    assert "## 背景资料（前期沟通简报）" in packet.user_content
    assert packet.stats["brief"] <= 320
