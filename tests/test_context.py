"""上下文装配：分层、预算裁剪、摘要化（避免让模型自己压缩）。"""

from __future__ import annotations

from app.schemas.plan import ArchitecturePlan
from app.schemas.run import RunStep, StepStatus
from app.services.context import (
    StepContextBuilder,
    clip,
    clip_head_tail,
    plan_digest,
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
    assert "adapter" in digest
    assert "其他步骤" in digest and "1.加适配层" in digest
    assert "2.替换调用点" not in digest


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


def test_clip_helper():
    assert clip("abc", 10) == "abc"
    assert clip("abcdef", 3).startswith("abc")


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
        budget_chars=24000, files_max_chars=12000, file_max_chars=6000
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
    # 均分：单个文件不超过总预算的 1/3 太多
    assert packet.stats["files"] <= 12000 + 200


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
