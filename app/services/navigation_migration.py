"""导航兼容迁移层（第 5 步交付物）。

职责：

* 旧的一级「架构 / 计划 / 执行 / 验证 / 日志 / 设置」链接重定向到项目内对应模块；
* 缺少项目定位信息时不静默落到默认项目，而是明确返回「需要选择项目」的引导；
* 旧的跨项目聚合链接明确标记为不再支持（410），并引导到项目列表；
* 为项目页面推导状态（空项目 / 加载失败 / 运行中 / 运行失败 / 取消 / 重启中）。

本层只做路由与状态翻译：不复制编排、执行、验证逻辑，也不修改既有服务。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.schemas.navigation import (
    DEFAULT_ROUTE,
    PROJECT_LIST_ROUTE,
    ProjectModule,
    project_module_context,
)
from app.schemas.project import (
    CONTEXT_ID_SEPARATOR,
    CONTEXT_TYPE_CHAT,
    CONTEXT_TYPE_PROJECT,
)

__all__ = [
    "LEGACY_PROJECT_MODULE_ROUTES",
    "LEGACY_REMOVED_ROUTES",
    "LegacyRouteResolution",
    "MigrationKind",
    "ProjectViewState",
    "derive_project_view_state",
    "resolve_legacy_route",
]


class MigrationKind(StrEnum):
    """旧链接的处理方式。"""

    KEEP = "keep"
    REDIRECT = "redirect"
    NEEDS_PROJECT = "needs_project"
    REMOVED = "removed"


class ProjectViewState(StrEnum):
    """项目页面状态：左侧栏与页面边界共用的展示态。"""

    EMPTY_PROJECT = "empty_project"
    LOAD_FAILED = "load_failed"
    RUNNING = "running"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RESTARTING = "restarting"
    COMPLETED = "completed"


LEGACY_PROJECT_MODULE_ROUTES: Mapping[str, ProjectModule] = {
    "#/overview": ProjectModule.OVERVIEW,
    "#/architecture": ProjectModule.ARCHITECTURE,
    "#/arch": ProjectModule.ARCHITECTURE,
    "#/design": ProjectModule.ARCHITECTURE,
    "#/plan": ProjectModule.PLAN,
    "#/plans": ProjectModule.PLAN,
    "#/execution": ProjectModule.EXECUTION,
    "#/execute": ProjectModule.EXECUTION,
    "#/run": ProjectModule.EXECUTION,
    "#/runs": ProjectModule.EXECUTION,
    "#/verification": ProjectModule.VERIFICATION,
    "#/verify": ProjectModule.VERIFICATION,
    "#/logs": ProjectModule.LOGS,
    "#/settings": ProjectModule.SETTINGS,
}
"""旧的全局工程入口：全部下沉为项目内模块。"""

LEGACY_REMOVED_ROUTES: frozenset[str] = frozenset(
    {
        "#/architecture/all",
        "#/plans/all",
        "#/execution/all",
        "#/runs/all",
        "#/verification/all",
        "#/logs/all",
    }
)
"""旧的跨项目聚合入口：与项目边界隔离冲突，不再支持。"""

KEPT_ROUTE_PREFIXES: tuple[str, ...] = ("#/chat", PROJECT_LIST_ROUTE)
"""保留入口前缀：普通对话与项目列表 / 项目内部路由。"""

_RUNNING_STATUSES = frozenset(
    {"executing", "running", "pending", "queued", "in_progress", "planning"}
)
_FAILED_STATUSES = frozenset({"failed", "error", "errored"})
_CANCELLED_STATUSES = frozenset({"cancelled", "canceled", "aborted"})
_RESTARTING_STATUSES = frozenset({"restarting", "resuming"})
_COMPLETED_STATUSES = frozenset({"completed", "succeeded", "done", "finished"})


@dataclass(frozen=True)
class LegacyRouteResolution:
    """一条旧链接的迁移结论。"""

    route: str
    kind: MigrationKind
    status_code: int
    target_route: str | None = None
    module: ProjectModule | None = None
    project_id: str | None = None
    context_id: str | None = None
    requires_project_selection: bool = False
    message: str = ""

    @property
    def redirects(self) -> bool:
        return self.kind in (MigrationKind.REDIRECT, MigrationKind.NEEDS_PROJECT)


def resolve_legacy_route(
    route: str | None,
    *,
    project_id: str | None = None,
    context_id: str | None = None,
) -> LegacyRouteResolution:
    """把旧入口翻译成新信息架构下的落点。

    优先级：保留路由 > 不再支持 > 项目模块路由 > 未知路由透传。
    项目定位只认显式 ``project_id`` 或 ``project:<projectId>`` 形式的 ``context_id``；
    两者都拿不到时返回「需要选择项目」引导，绝不静默落到默认项目。
    """

    normalized = _normalize_route(route)
    explicit_project = _normalize_project_id(project_id)
    context_project, context_is_chat = _split_context(context_id)

    if _is_kept_route(normalized):
        return LegacyRouteResolution(
            route=normalized,
            kind=MigrationKind.KEEP,
            status_code=200,
            target_route=normalized or DEFAULT_ROUTE,
            project_id=explicit_project or context_project,
            context_id=context_id,
            message="保留入口：普通对话与项目路由原样生效",
        )

    if normalized in LEGACY_REMOVED_ROUTES:
        return LegacyRouteResolution(
            route=normalized,
            kind=MigrationKind.REMOVED,
            status_code=410,
            target_route=PROJECT_LIST_ROUTE,
            requires_project_selection=True,
            message="跨项目聚合入口不再支持：请进入具体项目查看对应模块",
        )

    module = LEGACY_PROJECT_MODULE_ROUTES.get(normalized)
    if module is None:
        return LegacyRouteResolution(
            route=normalized,
            kind=MigrationKind.KEEP,
            status_code=200,
            target_route=normalized,
            project_id=explicit_project or context_project,
            context_id=context_id,
            message="未注册的路由按原样保留",
        )

    if context_is_chat:
        return LegacyRouteResolution(
            route=normalized,
            kind=MigrationKind.NEEDS_PROJECT,
            status_code=302,
            target_route=PROJECT_LIST_ROUTE,
            module=module,
            project_id=None,
            context_id=context_id,
            requires_project_selection=True,
            message="普通对话不承载项目模块：请先选择项目",
        )

    resolved_project = explicit_project or context_project
    if resolved_project is None:
        return LegacyRouteResolution(
            route=normalized,
            kind=MigrationKind.NEEDS_PROJECT,
            status_code=302,
            target_route=PROJECT_LIST_ROUTE,
            module=module,
            project_id=None,
            context_id=context_id,
            requires_project_selection=True,
            message="该入口已归入项目：请先选择项目",
        )

    scoped = project_module_context(module, resolved_project)
    return LegacyRouteResolution(
        route=normalized,
        kind=MigrationKind.REDIRECT,
        status_code=302,
        target_route=scoped.route,
        module=module,
        project_id=resolved_project,
        context_id=scoped.context_id,
        message="已迁移到项目内模块",
    )


def derive_project_view_state(
    *,
    project: Any = None,
    run: Any = None,
    load_error: Any = None,
    restarting: bool = False,
) -> ProjectViewState:
    """推导项目页面状态，覆盖空项目 / 加载失败 / 运行中 / 失败 / 取消 / 重启中。"""

    if load_error:
        return ProjectViewState.LOAD_FAILED
    if restarting:
        return ProjectViewState.RESTARTING

    status = _status_of(run)
    if status in _RESTARTING_STATUSES:
        return ProjectViewState.RESTARTING
    if status in _FAILED_STATUSES:
        return ProjectViewState.RUN_FAILED
    if status in _CANCELLED_STATUSES:
        return ProjectViewState.RUN_CANCELLED
    if status in _RUNNING_STATUSES:
        return ProjectViewState.RUNNING
    if status in _COMPLETED_STATUSES:
        return ProjectViewState.COMPLETED
    return ProjectViewState.EMPTY_PROJECT


# --- 内部 ---


def _normalize_route(route: str | None) -> str:
    if not route:
        return ""
    normalized = str(route).strip()
    while len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def _normalize_project_id(project_id: str | None) -> str | None:
    if project_id is None:
        return None
    value = str(project_id).strip()
    return value or None


def _split_context(context_id: str | None) -> tuple[str | None, bool]:
    """返回 (项目 id, 是否普通对话上下文)。"""

    if not context_id:
        return None, False
    prefix, separator, remainder = str(context_id).partition(CONTEXT_ID_SEPARATOR)
    if not separator:
        return None, False
    value = remainder.strip()
    if prefix == CONTEXT_TYPE_PROJECT:
        return (value or None), False
    if prefix == CONTEXT_TYPE_CHAT:
        return None, True
    return None, False


def _is_kept_route(normalized: str) -> bool:
    if not normalized or normalized in {"#", "#/"}:
        return True
    for prefix in KEPT_ROUTE_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def _status_of(run: Any) -> str | None:
    if run is None:
        return None
    status = run.get("status") if isinstance(run, Mapping) else getattr(run, "status", None)
    if status is None:
        return None
    return str(status).strip().lower() or None
