"""重做 / 继续 / 恢复这一步时，必须把上一轮的派生状态清干净。

回归背景：``retry_step`` / ``resume`` / ``recover_interrupted`` 曾经各自手写一份
"清哪些字段"，于是留下了自相矛盾的记录——``files`` 清空了、``verification``
还留着（界面上「0 个文件却有 8 条验收」），``started_at``/``finished_at`` 也残留
上一轮的时间。现在统一走 :meth:`RunStep.reset_for_rerun`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import Settings
from app.schemas.plan import CheckResult
from app.schemas.run import (
    ChangedFile,
    CommandRun,
    Run,
    RunStatus,
    RunStep,
    StepStatus,
)
from app.services.events import EventBus
from app.services.orchestrator import Orchestrator
from app.services.storage import RunStore


def _dirty_step(step_id: int, status: StepStatus) -> RunStep:
    """造一个"跑过一轮"的步骤：每个派生字段都有值，才能验证真的被清掉。"""

    started = datetime.now(UTC) - timedelta(minutes=5)
    return RunStep(
        id=step_id,
        title=f"第 {step_id} 步",
        goal="写点东西",
        deliverables=["src/a.py"],
        checks=[],
        verification=[CheckResult(type="file_exists", path="src/a.py", label="文件存在", ok=True)],
        status=status,
        summary="上一轮的总结",
        handoff="上一轮的接力说明",
        notes=["上一轮的备注"],
        commands=[{"cmd": "pytest -q", "why": "跑测试"}],
        command_results=[CommandRun(cmd="pytest -q", ok=False, exit_code=1, error="失败")],
        files=[ChangedFile(path="src/a.py", action="create", additions=3)],
        context_chars=12345,
        retries=2,
        fetched_files=["src/a.py"],
        skills_used=["code-review"],
        tool_results=[{"tool": "read_file", "ok": True, "content": "x"}],
        git_snapshot={"backend": "git", "files": {"src/a.py": "hash"}},
        error="上一轮的错误",
        started_at=started,
        finished_at=started + timedelta(seconds=30),
    )


def _assert_clean(step: RunStep) -> None:
    assert step.status == StepStatus.PENDING
    assert step.error == ""
    assert step.summary == ""
    assert step.handoff == ""
    assert step.notes == []
    assert step.commands == []
    assert step.command_results == []
    assert step.files == []
    assert step.verification == []
    assert step.fetched_files == []
    assert step.skills_used == []
    assert step.tool_results == []
    assert step.context_chars == 0
    assert step.retries == 0
    assert step.git_snapshot == {}
    assert step.started_at is None
    assert step.finished_at is None


def test_reset_for_rerun_clears_every_derived_field():
    step = _dirty_step(1, StepStatus.FAILED)

    step.reset_for_rerun()

    _assert_clean(step)


def _orchestrator(tmp_path: Path) -> tuple[Orchestrator, RunStore]:
    store = RunStore(tmp_path / "runs")
    orchestrator = Orchestrator(store, EventBus(), settings_provider=lambda: Settings())
    # 只验证"清理"，这里不真的去跑执行段（否则会因为没有中转配置而立刻失败）
    orchestrator.start_execution = lambda run_id: None  # type: ignore[method-assign]
    return orchestrator, store


def test_retry_step_clears_previous_verification(tmp_path: Path):
    orchestrator, store = _orchestrator(tmp_path)
    run = Run(
        id="20260101-000000-aaaa",
        task="t",
        status=RunStatus.DONE,
        steps=[_dirty_step(1, StepStatus.DONE), RunStep(id=2, title="第二步")],
    )
    store.save(run)

    updated = orchestrator.retry_step(run.id, 1)

    _assert_clean(updated.steps[0])
    # 没被重做的步骤保持原样
    assert updated.steps[1].status == StepStatus.PENDING
    assert updated.stop_after_step == 1


def test_resume_clears_stale_step_state(tmp_path: Path):
    """补充信息继续时，"这一步没产出却有验收"的记录不能再出现。"""

    orchestrator, store = _orchestrator(tmp_path)
    run = Run(
        id="20260101-000000-bbbb",
        task="t",
        status=RunStatus.BLOCKED,
        error={"code": "step_failed", "message": "第 1 步被阻塞"},
        steps=[_dirty_step(1, StepStatus.BLOCKED), _dirty_step(2, StepStatus.FAILED)],
    )
    store.save(run)

    resumed = orchestrator.resume(run.id, note="这是补充信息")

    assert resumed.status == RunStatus.EXECUTING
    assert resumed.error is None
    for step in resumed.steps:
        _assert_clean(step)
    assert "这是补充信息" in resumed.user_notes


def test_recover_interrupted_clears_partial_step(tmp_path: Path):
    orchestrator, store = _orchestrator(tmp_path)
    run = Run(
        id="20260101-000000-cccc",
        task="t",
        status=RunStatus.EXECUTING,
        steps=[
            RunStep(id=1, title="done", status=StepStatus.DONE, summary="完成"),
            _dirty_step(2, StepStatus.RUNNING),
        ],
    )
    store.save(run)

    assert orchestrator.recover_interrupted() == 1

    recovered = store.load(run.id)
    assert recovered.status == RunStatus.PAUSED
    _assert_clean(recovered.steps[1])
    # 已完成的那一步不动
    assert recovered.steps[0].status == StepStatus.DONE
    assert recovered.steps[0].summary == "完成"
