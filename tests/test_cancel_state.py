"""取消运行时的状态收敛：不能留下"已取消但步骤仍在执行"的悬挂状态。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.config import Settings
from app.schemas.run import Run, RunStatus, RunStep, StepStatus
from app.services.events import EventBus
from app.services.orchestrator import Orchestrator
from app.services.storage import RunStore


def test_cancel_returns_running_step_to_pending(tmp_path: Path):
    store = RunStore(tmp_path / "runs")
    run = Run(
        id="20260101-000000-abcd",
        title="t",
        task="t",
        status=RunStatus.EXECUTING,
        steps=[
            RunStep(id=1, title="a", status=StepStatus.DONE, summary="done"),
            RunStep(id=2, title="b", status=StepStatus.RUNNING, summary="进行中"),
            RunStep(id=3, title="c", status=StepStatus.PENDING),
        ],
    )
    store.save(run)
    orchestrator = Orchestrator(store, EventBus(), settings_provider=lambda: Settings())

    cancelled = orchestrator.cancel(run.id)

    assert cancelled.status == RunStatus.CANCELLED
    assert [step.status for step in cancelled.steps] == [
        StepStatus.DONE,
        StepStatus.PENDING,
        StepStatus.PENDING,
    ]
    # 已完成的那一步不受影响
    assert cancelled.steps[0].summary == "done"
    # 事件总线拿得到状态事件（界面据此刷新）
    assert any(
        event["type"] == "status"
        for event in asyncio.run(_collect_status(orchestrator.bus, run.id))
    )


def test_recover_interrupted_runs_on_startup(tmp_path: Path):
    """进程被杀后重启：不能留下"永远在执行中"的运行。"""
    store = RunStore(tmp_path / "runs")
    interrupted = Run(
        id="20260101-000000-aaaa",
        title="被打断的运行",
        task="t",
        status=RunStatus.EXECUTING,
        steps=[
            RunStep(id=1, title="done", status=StepStatus.DONE, summary="完成"),
            RunStep(id=2, title="running", status=StepStatus.RUNNING, summary="写了一半"),
            RunStep(id=3, title="pending", status=StepStatus.PENDING),
        ],
    )
    finished = Run(
        id="20260101-000000-bbbb",
        title="正常结束",
        task="t",
        status=RunStatus.DONE,
        steps=[RunStep(id=1, title="a", status=StepStatus.DONE)],
    )
    store.save(interrupted)
    store.save(finished)

    orchestrator = Orchestrator(store, EventBus(), settings_provider=lambda: Settings())
    assert orchestrator.recover_interrupted() == 1

    recovered = store.load(interrupted.id)
    assert recovered.status == RunStatus.PAUSED
    assert recovered.error["code"] == "interrupted"
    assert [step.status for step in recovered.steps] == [
        StepStatus.DONE,
        StepStatus.PENDING,
        StepStatus.PENDING,
    ]
    assert recovered.steps[1].summary == ""
    # 已完成的运行不受影响
    assert store.load(finished.id).status == RunStatus.DONE
    # 幂等：再跑一次不会重复处理
    assert orchestrator.recover_interrupted() == 0


async def _collect_status(bus: EventBus, run_id: str) -> list[dict]:
    collected: list[dict] = []
    async for event in bus.subscribe(run_id):
        collected.append(event)
        if event["type"] == "status":
            break
    return collected
