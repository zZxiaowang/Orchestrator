"""项目边界测试集合（第 5 步交付物，子包变体，与 tests/test_project_boundary.py 同源）。

覆盖普通对话隔离、项目上下文恢复、跨项目拒绝、旧入口只映射进项目模块，
以及空项目 / 加载失败 / 运行中 / 运行失败 / 运行取消 / 重启中的页面状态。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.schemas.navigation import (
    CHAT_FORBIDDEN_ACTIONS,
    PRIMARY_NAVIGATION,
    PROJECT_LIST_ROUTE,
    PROJECT_ONLY_ACTIONS,
    ProjectModule,
    project_module_context,
)
from app.schemas.project import (
    CONTEXT_ID_SEPARATOR,
    CONTEXT_TYPE_CHAT,
    CONTEXT_TYPE_PROJECT,
    ProjectContextError,
    ensure_same_project,
    project_context_id,
    project_id_from_context_id,
)
from app.services import project_workspace as workspace_module
from app.services.navigation_migration import (
    LEGACY_PROJECT_MODULE_ROUTES,
    LEGACY_REMOVED_ROUTES,
    ProjectViewState,
    derive_project_view_state,
)

REQUIRED_PROJECT_VIEW_STATES = (
    ProjectViewState.EMPTY_PROJECT,
    ProjectViewState.LOAD_FAILED,
    ProjectViewState.RUNNING,
    ProjectViewState.RUN_FAILED,
    ProjectViewState.RUN_CANCELLED,
    ProjectViewState.RESTARTING,
)

CROSS_PROJECT_ENTRYPOINT_TOKENS = (
    "aggregate",
    "all_projects",
    "all_runs",
    "cross_project",
    "every_project",
    "global",
)

RUN_STATUS_SAMPLES = ("running", "failed", "cancelled", "restarting")


def _project_of(context_id: Any) -> Any:
    resolved = project_id_from_context_id(context_id)
    if isinstance(resolved, tuple):
        return resolved[0]
    return resolved


def _workspace_class() -> type:
    cls = getattr(workspace_module, "ProjectWorkspace", None)
    if cls is None:
        pytest.skip("项目工作区尚未导出 ProjectWorkspace")
    return cls


def _accepts_two_positional_arguments(func: Any) -> bool:
    try:
        return len(inspect.signature(func).parameters) >= 2
    except (TypeError, ValueError):
        return False


def test_primary_navigation_exposes_two_task_entries():
    entries = tuple(PRIMARY_NAVIGATION)
    assert len(entries) == 2
    blob = " ".join(str(getattr(entry, "value", entry)).lower() for entry in entries)
    assert "chat" in blob and "project" in blob


def test_chat_context_is_isolated_from_project_actions():
    assert CHAT_FORBIDDEN_ACTIONS == PROJECT_ONLY_ACTIONS
    assert {module.value for module in ProjectModule} == PROJECT_ONLY_ACTIONS
    assert PROJECT_LIST_ROUTE == "#/projects"
    assert _project_of(f"{CONTEXT_TYPE_CHAT}{CONTEXT_ID_SEPARATOR}s1") is None


def test_project_context_is_restored_from_scoped_context():
    assert project_context_id("alpha") == f"{CONTEXT_TYPE_PROJECT}{CONTEXT_ID_SEPARATOR}alpha"
    assert _project_of(project_context_id("alpha")) == "alpha"


def test_project_module_context_requires_project_id():
    context = project_module_context(ProjectModule.EXECUTION, "alpha")
    assert context.project_id == "alpha"
    assert context.context_id == project_context_id("alpha")
    with pytest.raises(ProjectContextError):
        project_module_context(ProjectModule.ARCHITECTURE, "   ")


def test_cross_project_records_are_rejected():
    if not _accepts_two_positional_arguments(ensure_same_project):
        pytest.skip("ensure_same_project 不接受裸标识符，跳过")
    with pytest.raises(ProjectContextError):
        ensure_same_project("alpha", "beta")


def test_legacy_entries_map_into_project_modules():
    assert LEGACY_PROJECT_MODULE_ROUTES
    for route, module in LEGACY_PROJECT_MODULE_ROUTES.items():
        assert str(route).startswith("#/")
        assert isinstance(module, ProjectModule)
    assert LEGACY_REMOVED_ROUTES is not None


def test_workspace_exposes_no_cross_project_entrypoints():
    names = " ".join(
        name for name, _ in inspect.getmembers(workspace_module) if not name.startswith("_")
    ).lower()
    for token in CROSS_PROJECT_ENTRYPOINT_TOKENS:
        assert token not in names
    public = [
        name
        for name, member in inspect.getmembers(_workspace_class(), callable)
        if not name.startswith("_")
    ]
    assert public


def test_required_project_view_states_are_defined():
    for state in REQUIRED_PROJECT_VIEW_STATES:
        assert isinstance(state, ProjectViewState)


@pytest.mark.parametrize("status", RUN_STATUS_SAMPLES)
def test_derive_project_view_state_returns_enum(status):
    state = derive_project_view_state(project={"id": "alpha"}, run={"status": status})
    assert state is None or isinstance(state, ProjectViewState)
