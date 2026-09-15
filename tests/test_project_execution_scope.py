"""第 4 步验收：项目内执行边界 —— 取消 / 失败 / 完成 / 重启语义保持不变。

这一组用例只关心「工作区是否改写执行语义」，因此测试替身只记录调用与状态，
真正的状态机仍由既有 orchestrator 提供。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.schemas.project import ProjectContextError, ProjectContextErrorCode
from app.services.project_workspace import (
    ProjectWorkspace,
    ProjectWorkspaceUnavailableError,
    scope_error_code,
)


class FakeRun:
    def __init__(self, run_id: str, project_id: str, status: str) -> None:
        self.id = run_id
        self.run_id = run_id
        self.project_id = project_id
        self.status = status


class RecordingOrchestrator:
    """只记录调用与状态变更；不实现真实编排。"""

    def __init__(self, run: FakeRun) -> None:
        self.run = run
        self.calls: list[tuple[Any, ...]] = []
        self.mutations: list[str] = []

    def get_run(self, run_id: str) -> FakeRun | None:
        self.calls.append(("get_run", run_id))
        return self.run if self.run.id == run_id else None

    def start_run(self, *, project_id: str, context_id: str | None = None, **payload: Any) -> Any:
        self.calls.append(("start_run", project_id))
        self.mutations.append("start")
        return FakeRun("run-new", project_id, "planning")

    def cancel_run(self, run_id: str, *, project_id: str, context_id: str | None = None) -> Any:
        self.calls.append(("cancel_run", run_id, project_id))
        self.run.status = "cancelled"
        self.mutations.append("cancel")
        return self.run

    def restart_run(self, run_id: str, *, project_id: str, context_id: str | None = None) -> Any:
        self.calls.append(("restart_run", run_id, project_id))
        self.run.status = "executing"
        self.mutations.append("restart")
        return self.run


def test_cancel_keeps_existing_status_semantics() -> None:
    run = FakeRun("run-1", "alpha", "executing")
    orchestrator = RecordingOrchestrator(run)
    workspace = ProjectWorkspace(orchestrator=orchestrator)

    returned = workspace.cancel_run("run-1", project_id="alpha")

    assert returned.status == "cancelled"
    assert orchestrator.run.status == "cancelled"
    assert orchestrator.mutations == ["cancel"]


def test_restart_keeps_existing_status_semantics() -> None:
    run = FakeRun("run-1", "alpha", "cancelled")
    orchestrator = RecordingOrchestrator(run)
    workspace = ProjectWorkspace(orchestrator=orchestrator)

    returned = workspace.restart_run("run-1", project_id="alpha")

    assert returned.status == "executing"
    assert orchestrator.run.status == "executing"
    assert orchestrator.mutations == ["restart"]


@pytest.mark.parametrize("status", ["failed", "done", "cancelled"])
def test_read_run_preserves_recorded_status(status: str) -> None:
    run = FakeRun("run-1", "alpha", status)
    orchestrator = RecordingOrchestrator(run)
    workspace = ProjectWorkspace(orchestrator=orchestrator)

    assert workspace.read_run("run-1", project_id="alpha").status == status


def test_cross_project_control_is_rejected_without_mutation() -> None:
    run = FakeRun("run-1", "alpha", "executing")
    orchestrator = RecordingOrchestrator(run)
    workspace = ProjectWorkspace(orchestrator=orchestrator)

    with pytest.raises(ProjectContextError) as excinfo:
        workspace.cancel_run("run-1", project_id="beta")

    assert scope_error_code(excinfo.value) == ProjectContextErrorCode.PROJECT_ACCESS_DENIED.value
    assert run.status == "executing"
    assert orchestrator.mutations == []
    assert [call[0] for call in orchestrator.calls] == ["get_run"]


def test_chat_context_never_starts_project_execution() -> None:
    run = FakeRun("run-1", "alpha", "executing")
    orchestrator = RecordingOrchestrator(run)
    workspace = ProjectWorkspace(orchestrator=orchestrator)

    with pytest.raises(ProjectContextError):
        workspace.cancel_run("run-1", context_id="chat:session-9")

    assert orchestrator.calls == []
    assert orchestrator.mutations == []


def test_execution_requires_existing_service() -> None:
    workspace = ProjectWorkspace()
    with pytest.raises(ProjectWorkspaceUnavailableError):
        workspace.start_run(project_id="alpha")
    with pytest.raises(ProjectWorkspaceUnavailableError):
        workspace.restart_run("run-1", project_id="alpha")
