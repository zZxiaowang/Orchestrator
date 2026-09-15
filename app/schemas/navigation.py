"""导航数据契约（第 2 步交付物）。

上位文档：``docs/project-context-contract.md``、``docs/project-navigation-contract.md``。

一级导航只表达用户任务入口：普通对话（``chat``）与项目（``projects``）。架构、计划、执行、
步骤、验证、日志、设置只作为「项目」下的二级模块存在，普通对话上下文不允许访问。
"""

from __future__ import annotations

from enum import Enum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.project import (
    CONTEXT_TYPE_CHAT,
    CONTEXT_TYPE_PROJECT,
    DEFAULT_CONTEXT_TYPE,
    DEFAULT_PROJECT_ID,
    ProjectContextError,
    ProjectContextErrorCode,
    project_context_id,
    project_id_from_context_id,
)

CHAT_CONTEXT_TYPE: Literal["chat"] = CONTEXT_TYPE_CHAT
PROJECT_CONTEXT_TYPE: Literal["project"] = CONTEXT_TYPE_PROJECT


class NavEntry(StrEnum):
    """一级导航入口，有且只有两个。"""

    CHAT = "chat"
    PROJECTS = "projects"


class ProjectModule(StrEnum):
    """项目内部二级模块，等价于「项目专属动作」集合。"""

    OVERVIEW = "overview"
    ARCHITECTURE = "architecture"
    PLAN = "plan"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    LOGS = "logs"
    SETTINGS = "settings"


PROJECT_MODULES: tuple[ProjectModule, ...] = tuple(ProjectModule)
PROJECT_ONLY_ACTIONS: frozenset[str] = frozenset(module.value for module in ProjectModule)
CHAT_FORBIDDEN_ACTIONS: frozenset[str] = PROJECT_ONLY_ACTIONS

PROJECT_LIST_ROUTE = "#/projects"
CHAT_ROUTES: tuple[str, ...] = ("#/chat", "#/chat/{session_id}")
DEFAULT_ROUTE = PROJECT_LIST_ROUTE

PROJECT_MODULE_ROUTES: dict[ProjectModule, str] = {
    ProjectModule.OVERVIEW: "#/projects/{project_id}",
    ProjectModule.ARCHITECTURE: "#/projects/{project_id}/architecture",
    ProjectModule.PLAN: "#/projects/{project_id}/plan",
    ProjectModule.EXECUTION: "#/projects/{project_id}/execution",
    ProjectModule.VERIFICATION: "#/projects/{project_id}/verification",
    ProjectModule.LOGS: "#/projects/{project_id}/logs",
    ProjectModule.SETTINGS: "#/projects/{project_id}/settings",
}


class NavigationEntry(BaseModel):
    """一级入口描述：普通对话与项目互不包含对方的模块。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: NavEntry
    label: str
    context_type: Literal["chat", "project"]
    route_template: str
    requires_project: bool = False
    modules: tuple[ProjectModule, ...] = ()

    @field_validator("route_template")
    @classmethod
    def _validate_route_template(cls, value: str) -> str:
        value = (value or "").strip()
        if not value.startswith("#/"):
            raise ValueError(f"路由必须以 '#/' 开头: {value!r}")
        return value

    def build_route(self, project_id: str | None = None, session_id: str | None = None) -> str:
        route = self.route_template
        if "{project_id}" in route:
            if not project_id or not str(project_id).strip():
                raise ProjectContextError(
                    ProjectContextErrorCode.MISSING_PROJECT_CONTEXT,
                    f"入口 {self.entry.value} 需要 project_id",
                )
            route = route.replace("{project_id}", str(project_id).strip())
        if "{session_id}" in route:
            if not session_id or not str(session_id).strip():
                raise ProjectContextError(
                    ProjectContextErrorCode.MISSING_PROJECT_CONTEXT,
                    f"入口 {self.entry.value} 需要 session_id",
                )
            route = route.replace("{session_id}", str(session_id).strip())
        return route


PRIMARY_NAVIGATION: tuple[NavigationEntry, ...] = (
    NavigationEntry(
        entry=NavEntry.CHAT,
        label="普通对话",
        context_type=CHAT_CONTEXT_TYPE,
        route_template="#/chat",
        requires_project=False,
        modules=(),
    ),
    NavigationEntry(
        entry=NavEntry.PROJECTS,
        label="项目",
        context_type=PROJECT_CONTEXT_TYPE,
        route_template=PROJECT_LIST_ROUTE,
        requires_project=False,
        modules=PROJECT_MODULES,
    ),
)


def primary_entries() -> tuple[NavigationEntry, ...]:
    """左侧栏一级入口（顺序固定：普通对话 → 项目）。"""
    return PRIMARY_NAVIGATION


def entry_for(entry: NavEntry | str) -> NavigationEntry:
    key = entry if isinstance(entry, NavEntry) else NavEntry(str(entry))
    for item in PRIMARY_NAVIGATION:
        if item.entry is key:
            return item
    raise ProjectContextError(
        ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
        f"未知一级入口: {entry!r}",
    )


def build_project_route(module: ProjectModule | str, project_id: str) -> str:
    """构造项目内模块路由；缺 project_id 时按「缺失」语义报错。"""
    module_key = module if isinstance(module, ProjectModule) else ProjectModule(str(module))
    normalized = "" if project_id is None else str(project_id).strip()
    if not normalized:
        raise ProjectContextError(
            ProjectContextErrorCode.MISSING_PROJECT_CONTEXT,
            f"项目模块 {module_key.value} 的路由需要 project_id",
        )
    return PROJECT_MODULE_ROUTES[module_key].format(project_id=normalized)


def default_project_route(module: ProjectModule | str = ProjectModule.OVERVIEW) -> str:
    """历史数据没有项目上下文时的回落路由（不迁移数据，仅做展示归位）。"""
    return build_project_route(module, DEFAULT_PROJECT_ID)


def module_from_route(route: str) -> ProjectModule | None:
    """从路由解析项目二级模块；普通对话路由报 invalid，项目列表返回 None。"""
    raw = "" if route is None else str(route).strip()
    if raw.startswith("#/chat"):
        raise ProjectContextError(
            ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
            f"普通对话路由不属于项目模块: {raw!r}",
        )
    if not raw.startswith(PROJECT_LIST_ROUTE):
        return None
    segments = [segment for segment in raw[len(PROJECT_LIST_ROUTE) :].split("/") if segment]
    if not segments:
        return None
    if len(segments) == 1:
        return ProjectModule.OVERVIEW
    try:
        return ProjectModule(segments[1])
    except ValueError:
        return None


def route_project_id(route: str) -> str | None:
    """从项目内路由解析 project_id（``#/projects/<projectId>/...``）。"""
    raw = "" if route is None else str(route).strip()
    if not raw.startswith(PROJECT_LIST_ROUTE):
        return None
    segments = [segment for segment in raw[len(PROJECT_LIST_ROUTE) :].split("/") if segment]
    if not segments:
        return None
    candidate = segments[0]
    if candidate.startswith("project:"):
        return project_id_from_context_id(candidate)
    return candidate


def action_allowed(context_type: str, action: str) -> bool:
    """判断某动作是否允许出现在给定上下文类型下。"""
    key = action.value if isinstance(action, Enum) else str(action)
    if context_type == CHAT_CONTEXT_TYPE:
        return key not in PROJECT_ONLY_ACTIONS
    if context_type == PROJECT_CONTEXT_TYPE:
        return True
    raise ProjectContextError(
        ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
        f"未知 context_type: {context_type!r}",
    )


class ProjectModuleContext(BaseModel):
    """项目内模块的数据上下文：始终携带 ``project:<projectId>``。"""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    module: ProjectModule
    context_id: str
    context_type: Literal["project"] = PROJECT_CONTEXT_TYPE
    route: str

    @classmethod
    def build(cls, module: ProjectModule | str, project_id: str) -> ProjectModuleContext:
        module_key = module if isinstance(module, ProjectModule) else ProjectModule(str(module))
        normalized = "" if project_id is None else str(project_id).strip()
        if not normalized:
            raise ProjectContextError(
                ProjectContextErrorCode.MISSING_PROJECT_CONTEXT,
                "项目模块上下文需要 project_id",
            )
        return cls(
            project_id=normalized,
            module=module_key,
            context_id=project_context_id(normalized),
            route=build_project_route(module_key, normalized),
        )


def project_module_context(module: ProjectModule | str, project_id: str) -> ProjectModuleContext:
    return ProjectModuleContext.build(module, project_id)


class SidebarNavigation(BaseModel):
    """左侧栏契约：一级入口 + 默认落点 + 默认上下文类型。"""

    model_config = ConfigDict(extra="forbid")

    entries: tuple[NavigationEntry, ...] = Field(default_factory=lambda: PRIMARY_NAVIGATION)
    default_route: str = DEFAULT_ROUTE
    default_context_type: str = DEFAULT_CONTEXT_TYPE


SIDEBAR_NAVIGATION = SidebarNavigation()
