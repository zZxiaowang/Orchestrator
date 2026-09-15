"""第 5 步验收：导航兼容迁移、上下文恢复、边界状态与项目隔离。

覆盖：旧架构 / 执行入口重定向进项目内模块、缺少项目定位时返回选择项目引导、
跨项目聚合入口不再支持、保留入口透传、项目上下文恢复、普通对话隔离、
跨项目读取拒绝，以及空项目 / 加载失败 / 运行中 / 失败 / 取消 / 重启中的页面状态。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.schemas.navigation import PROJECT_LIST_ROUTE, ProjectModule, project_module_context
from app.schemas.project import (
    CONTEXT_TYPE_PROJECT,
    DEFAULT_PROJECT_ID,
    ProjectContextError,
)
from app.services.navigation_migration import (
    LEGACY_PROJECT_MODULE_ROUTES,
    LEGACY_REMOVED_ROUTES,
    MigrationKind,
    ProjectViewState,
    derive_project_view_state,
    resolve_legacy_route,
)
from app.services.project_workspace import ProjectWorkspace


class _Run:
    """测试替身：只承载数据，不实现执行语义。"""

    def __init__(self, run_id: str, project_id: str, status: str = "executing") -> None:
        self.id = run_id
        self.run_id = run_id
        self.project_id = project_id
        self.status = status
        self.steps = [{"id": f"{run_id}-step-1", "project_id": project_id}]
        self.events = [{"type": "status", "status": status}]
        self.metrics = {"steps_total": 1}


class _Orchestrator:
    """测试替身：只提供读取入口，证明工作区做归属校验。"""

    def __init__(self, runs: dict[str, Any] | None = None) -> None:
        self.runs = dict(runs or {})

    def get_run(self, run_id: str) -> Any:
        return self.runs.get(run_id)


def _workspace(*runs: _Run) -> ProjectWorkspace:
    orchestrator = _Orchestrator({run.run_id: run for run in runs})
    return ProjectWorkspace(orchestrator=orchestrator)


# --- 旧链接迁移 ---


@pytest.mark.parametrize(
    ("route", "module"),
    (
        ("#/architecture", ProjectModule.ARCHITECTURE),
        ("#/design", ProjectModule.ARCHITECTURE),
        ("#/plan", ProjectModule.PLAN),
        ("#/plans", ProjectModule.PLAN),
        ("#/execution", ProjectModule.EXECUTION),
        ("#/execute", ProjectModule.EXECUTION),
        ("#/runs", ProjectModule.EXECUTION),
        ("#/verification", ProjectModule.VERIFICATION),
        ("#/verify", ProjectModule.VERIFICATION),
        ("#/logs", ProjectModule.LOGS),
        ("#/settings", ProjectModule.SETTINGS),
        ("#/overview", ProjectModule.OVERVIEW),
    ),
)
def test_legacy_project_entry_redirects_into_project_module(
    route: str, module: ProjectModule
) -> None:
    result = resolve_legacy_route(route, project_id="alpha")

    assert result.kind is MigrationKind.REDIRECT
    assert result.module is module
    assert result.project_id == "alpha"
    assert result.context_id == project_module_context(module, "alpha").context_id
    assert result.target_route == project_module_context(module, "alpha").route
    assert result.status_code == 302
    assert result.redirects is True
    assert result.requires_project_selection is False


def test_legacy_project_entry_accepts_scoped_context_id() -> None:
    result = resolve_legacy_route("#/execution", context_id="project:beta")

    assert result.kind is MigrationKind.REDIRECT
    assert result.project_id == "beta"
    assert "beta" in (result.target_route or "")
    assert project_module_context(ProjectModule.EXECUTION, "beta").route == result.target_route


def test_legacy_project_entry_without_project_asks_for_selection() -> None:
    result = resolve_legacy_route("#/architecture")

    assert result.kind is MigrationKind.NEEDS_PROJECT
    assert result.requires_project_selection is True
    assert result.target_route == PROJECT_LIST_ROUTE
    assert result.status_code == 302
    assert result.module is ProjectModule.ARCHITECTURE


def test_unmappable_legacy_link_never_falls_back_to_default_project() -> None:
    result = resolve_legacy_route("#/logs")

    assert result.kind is MigrationKind.NEEDS_PROJECT
    assert result.project_id is None
    assert result.project_id != DEFAULT_PROJECT_ID
    assert result.target_route == PROJECT_LIST_ROUTE


def test_chat_context_cannot_open_project_module() -> None:
    result = resolve_legacy_route("#/execution", project_id="alpha", context_id="chat:s-1")

    assert result.kind is MigrationKind.NEEDS_PROJECT
    assert result.project_id is None
    assert result.requires_project_selection is True
    assert result.target_route == PROJECT_LIST_ROUTE


@pytest.mark.parametrize("route", sorted(LEGACY_REMOVED_ROUTES))
def test_cross_project_aggregate_entries_are_removed(route: str) -> None:
    result = resolve_legacy_route(route, project_id="alpha")

    assert result.kind is MigrationKind.REMOVED
    assert result.status_code == 410
    assert result.target_route == PROJECT_LIST_ROUTE
    assert result.requires_project_selection is True
    assert result.project_id is None


@pytest.mark.parametrize(
    "route",
    (
        "#/chat",
        "#/chat/session-1",
        "#/projects",
        "#/projects/alpha/execution",
        "#/projects/alpha",
        "#",
    ),
)
def test_chat_and_project_entries_are_kept(route: str) -> None:
    result = resolve_legacy_route(route)

    assert result.kind is MigrationKind.KEEP
    assert result.status_code == 200
    assert result.module is None


def test_unknown_route_is_passed_through() -> None:
    result = resolve_legacy_route("#/totally-unknown")

    assert result.kind is MigrationKind.KEEP
    assert result.target_route == "#/totally-unknown"


def test_migration_table_only_contains_project_modules() -> None:
    assert LEGACY_PROJECT_MODULE_ROUTES
    for module in LEGACY_PROJECT_MODULE_ROUTES.values():
        assert isinstance(module, ProjectModule)
    assert not set(LEGACY_PROJECT_MODULE_ROUTES) & set(LEGACY_REMOVED_ROUTES)


# --- 项目上下文恢复与边界隔离 ---


def test_project_context_is_restored_from_scoped_context() -> None:
    result = resolve_legacy_route("#/architecture", context_id="project:beta")

    assert result.kind is MigrationKind.REDIRECT
    assert (
        result.context_id == project_module_context(ProjectModule.ARCHITECTURE, "beta").context_id
    )
    assert result.context_id.startswith(CONTEXT_TYPE_PROJECT)


def test_chat_context_cannot_read_project_run() -> None:
    workspace = _workspace(_Run("run-alpha", "alpha"))

    with pytest.raises(ProjectContextError):
        workspace.read_run("run-alpha", project_id="alpha", context_id="chat:s-1")


def test_cross_project_read_is_rejected() -> None:
    workspace = _workspace(_Run("run-alpha", "alpha"))

    with pytest.raises(ProjectContextError):
        workspace.read_run("run-alpha", project_id="beta")


# --- 页面状态覆盖 ---


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        ("executing", ProjectViewState.RUNNING),
        ("running", ProjectViewState.RUNNING),
        ("pending", ProjectViewState.RUNNING),
        ("failed", ProjectViewState.RUN_FAILED),
        ("error", ProjectViewState.RUN_FAILED),
        ("cancelled", ProjectViewState.RUN_CANCELLED),
        ("canceled", ProjectViewState.RUN_CANCELLED),
        ("restarting", ProjectViewState.RESTARTING),
        ("resuming", ProjectViewState.RESTARTING),
        ("completed", ProjectViewState.COMPLETED),
    ),
)
def test_project_view_state_covers_run_statuses(status: str, expected: ProjectViewState) -> None:
    assert derive_project_view_state(project={"id": "alpha"}, run={"status": status}) is expected


def test_project_view_state_for_empty_project() -> None:
    state = derive_project_view_state(project={"id": "alpha"}, run=None)

    assert state is ProjectViewState.EMPTY_PROJECT


def test_project_view_state_for_load_failure() -> None:
    state = derive_project_view_state(project=None, run=None, load_error="boom")

    assert state is ProjectViewState.LOAD_FAILED


def test_project_view_state_for_restart_in_progress() -> None:
    state = derive_project_view_state(
        project={"id": "alpha"}, run={"status": "executing"}, restarting=True
    )

    assert state is ProjectViewState.RESTARTING


def test_project_view_state_accepts_object_runs() -> None:
    assert (
        derive_project_view_state(project={"id": "alpha"}, run=_Run("run-1", "alpha", "failed"))
        is ProjectViewState.RUN_FAILED
    )
    assert (
        derive_project_view_state(project={"id": "alpha"}, run=_Run("run-2", "alpha", "executing"))
        is ProjectViewState.RUNNING
    )


def test_project_view_state_covers_every_required_state() -> None:
    required = {
        ProjectViewState.EMPTY_PROJECT,
        ProjectViewState.LOAD_FAILED,
        ProjectViewState.RUNNING,
        ProjectViewState.RUN_FAILED,
        ProjectViewState.RUN_CANCELLED,
        ProjectViewState.RESTARTING,
    }
    observed = {
        derive_project_view_state(project={"id": "alpha"}, run=None),
        derive_project_view_state(project=None, load_error="boom"),
        derive_project_view_state(project={"id": "alpha"}, run={"status": "executing"}),
        derive_project_view_state(project={"id": "alpha"}, run={"status": "failed"}),
        derive_project_view_state(project={"id": "alpha"}, run={"status": "cancelled"}),
        derive_project_view_state(project={"id": "alpha"}, run={"status": "restarting"}),
    }

    assert required <= observed
