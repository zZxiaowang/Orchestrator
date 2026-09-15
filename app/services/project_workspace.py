"""项目工作区：项目上下文下的架构、执行、步骤、验证与重启入口（第 4 步交付物）。

设计边界：

* 不复制执行逻辑：工作区只解析 project_id、校验记录归属，把既有服务的结果原样透传；
  架构生成、运行编排、步骤执行、客观验收与重启仍由 app.services.orchestrator、
  app.services.executor、app.services.verify 提供。
* 写 / 控制必须显式落在项目上：创建计划、启动运行、取消、重启、发起验证都必须带
  project_id 或 project:<projectId> 形式的 context_id，缺失即报 missing_project_context。
* 读允许历史回落：读取计划 / 运行 / 步骤 / 事件 / 验证结果 / 指标时缺 project_id，
  先解析 context_id，仍没有才按契约回落到 project:default。
* 普通对话不碰项目运行：chat:<sessionId> 上下文下的创建、控制、读取项目运行一律被拒绝。
* 跨项目拒绝发生在写之前：控制运行前先校验归属，拒绝时目标项目数据不被修改。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.schemas.project import (
    CONTEXT_ID_SEPARATOR,
    CONTEXT_TYPE_CHAT,
    CONTEXT_TYPE_PROJECT,
    CONTEXT_TYPES,
    DEFAULT_PROJECT_ID,
    PROJECT_ID_PATTERN,
    ProjectContextError,
    ProjectContextErrorCode,
    ensure_same_project,
)

__all__ = [
    "ORCHESTRATOR_CANCEL_METHODS",
    "ORCHESTRATOR_EVENTS_METHODS",
    "ORCHESTRATOR_GET_PLAN_METHODS",
    "ORCHESTRATOR_GET_RUN_METHODS",
    "ORCHESTRATOR_METRICS_METHODS",
    "ORCHESTRATOR_PLAN_METHODS",
    "ORCHESTRATOR_RESTART_METHODS",
    "ORCHESTRATOR_START_METHODS",
    "ORCHESTRATOR_STEPS_METHODS",
    "ORCHESTRATOR_VERIFY_METHODS",
    "STORE_GET_METHODS",
    "VERIFICATION_READ_METHODS",
    "ProjectScope",
    "ProjectWorkspace",
    "ProjectWorkspaceUnavailableError",
    "ensure_record_in_scope",
    "parse_context_id",
    "project_context_id",
    "read_scope",
    "record_project_id",
    "scope_error_code",
    "write_scope",
]

_PROJECT_ID_RE = re.compile(PROJECT_ID_PATTERN)

#: 既有编排服务的候选方法名（兼容不同命名，本模块只探测不新增实现）。
ORCHESTRATOR_PLAN_METHODS: tuple[str, ...] = ("create_plan", "generate_plan", "start_plan", "plan")
ORCHESTRATOR_START_METHODS: tuple[str, ...] = (
    "start_run",
    "create_run",
    "start_execution",
    "start",
)
ORCHESTRATOR_CANCEL_METHODS: tuple[str, ...] = ("cancel_run", "cancel")
ORCHESTRATOR_RESTART_METHODS: tuple[str, ...] = (
    "restart_run",
    "restart",
    "retry_run",
    "resume_run",
)
ORCHESTRATOR_GET_PLAN_METHODS: tuple[str, ...] = ("get_plan", "load_plan", "find_plan", "read_plan")
ORCHESTRATOR_GET_RUN_METHODS: tuple[str, ...] = ("get_run", "load_run", "find_run", "read_run")
ORCHESTRATOR_STEPS_METHODS: tuple[str, ...] = ("list_steps", "get_steps", "steps")
ORCHESTRATOR_EVENTS_METHODS: tuple[str, ...] = ("list_events", "get_events", "events", "history")
ORCHESTRATOR_METRICS_METHODS: tuple[str, ...] = ("metrics", "get_metrics", "run_metrics")
ORCHESTRATOR_VERIFY_METHODS: tuple[str, ...] = (
    "request_verification",
    "verify_step",
    "verify_run",
    "verify",
)
VERIFICATION_READ_METHODS: tuple[str, ...] = (
    "get_verification",
    "read_verification",
    "verification",
)
STORE_GET_METHODS: tuple[str, ...] = ("get", "load", "find", "read")

_KNOWN_ERROR_CODES: tuple[str, ...] = tuple(code.value for code in ProjectContextErrorCode)


class ProjectWorkspaceUnavailableError(RuntimeError):
    """未绑定既有执行服务时抛出：工作区本身不提供任何执行能力。"""


@dataclass(frozen=True)
class ProjectScope:
    """一次请求所属的项目边界。"""

    project_id: str
    context_type: str
    context_id: str

    @property
    def is_chat(self) -> bool:
        return self.context_type == CONTEXT_TYPE_CHAT


def scope_error_code(exc: BaseException) -> str:
    """从 ProjectContextError 中取出稳定的错误码字符串（兼容字段命名差异）。"""

    for attribute in ("code", "error_code", "kind", "reason"):
        value = getattr(exc, attribute, None)
        if isinstance(value, ProjectContextErrorCode):
            return value.value
        if isinstance(value, str) and value in _KNOWN_ERROR_CODES:
            return value
    text = str(exc)
    for code in _KNOWN_ERROR_CODES:
        if code in text:
            return code
    return ""


def _raise(
    code: ProjectContextErrorCode,
    message: str,
    *,
    project_id: str | None = None,
    context_id: str | None = None,
) -> None:
    extra: dict[str, str] = {}
    if project_id is not None:
        extra["project_id"] = str(project_id)
    if context_id is not None:
        extra["context_id"] = str(context_id)
    raise ProjectContextError(code, message, **extra)


def project_context_id(project_id: str) -> str:
    """把 project_id 规范成 context_id。"""

    return f"{CONTEXT_TYPE_PROJECT}{CONTEXT_ID_SEPARATOR}{project_id}"


def parse_context_id(context_id: str) -> tuple[str, str]:
    """拆解 context_id；格式错误或未知类型一律报 invalid_project_context。"""

    raw = str(context_id or "").strip()
    if not raw:
        _raise(
            ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
            "context_id 不能为空",
            context_id=context_id,
        )
    context_type, separator, value = raw.partition(CONTEXT_ID_SEPARATOR)
    if not separator or not value:
        _raise(
            ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
            f"context_id 应形如 project:<projectId> 或 chat:<sessionId>，收到 {raw!r}",
            context_id=raw,
        )
    if context_type not in CONTEXT_TYPES:
        _raise(
            ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
            f"未知的 context 类型: {context_type!r}",
            context_id=raw,
        )
    return context_type, value


def _validate_project_id(value: str, context_id: str | None) -> str:
    candidate = str(value or "").strip()
    if not candidate or not _PROJECT_ID_RE.match(candidate):
        _raise(
            ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
            f"非法的 project_id: {candidate!r}",
            project_id=candidate,
            context_id=context_id,
        )
    return candidate


def read_scope(project_id: str | None = None, context_id: str | None = None) -> ProjectScope:
    """读操作的边界：普通对话不读项目数据；缺上下文时回落 project:default。"""

    if context_id:
        context_type, value = parse_context_id(context_id)
        if context_type == CONTEXT_TYPE_CHAT:
            _raise(
                ProjectContextErrorCode.PROJECT_ACCESS_DENIED,
                "普通对话不读取项目运行数据",
                project_id=project_id or DEFAULT_PROJECT_ID,
                context_id=context_id,
            )
        value = _validate_project_id(value, context_id)
        if project_id and str(project_id) != value:
            _raise(
                ProjectContextErrorCode.PROJECT_ACCESS_DENIED,
                f"请求项目 {project_id!r} 与上下文项目 {value!r} 不一致",
                project_id=str(project_id),
                context_id=context_id,
            )
        return ProjectScope(
            project_id=value,
            context_type=CONTEXT_TYPE_PROJECT,
            context_id=project_context_id(value),
        )
    if project_id:
        value = _validate_project_id(project_id, context_id)
        return ProjectScope(
            project_id=value,
            context_type=CONTEXT_TYPE_PROJECT,
            context_id=project_context_id(value),
        )
    return ProjectScope(
        project_id=DEFAULT_PROJECT_ID,
        context_type=CONTEXT_TYPE_PROJECT,
        context_id=project_context_id(DEFAULT_PROJECT_ID),
    )


def write_scope(project_id: str | None = None, context_id: str | None = None) -> ProjectScope:
    """写 / 控制操作的边界：必须显式落在某个项目上，普通对话一律拒绝。"""

    if context_id:
        context_type, value = parse_context_id(context_id)
        if context_type == CONTEXT_TYPE_CHAT:
            _raise(
                ProjectContextErrorCode.PROJECT_ACCESS_DENIED,
                "普通对话不承载项目运行：该操作只允许在项目上下文中执行",
                project_id=project_id or DEFAULT_PROJECT_ID,
                context_id=context_id,
            )
        value = _validate_project_id(value, context_id)
        if project_id and str(project_id) != value:
            _raise(
                ProjectContextErrorCode.PROJECT_ACCESS_DENIED,
                f"请求项目 {project_id!r} 与上下文项目 {value!r} 不一致",
                project_id=str(project_id),
                context_id=context_id,
            )
        return ProjectScope(
            project_id=value,
            context_type=CONTEXT_TYPE_PROJECT,
            context_id=project_context_id(value),
        )
    if not str(project_id or "").strip():
        _raise(
            ProjectContextErrorCode.MISSING_PROJECT_CONTEXT,
            "缺少项目上下文：项目内的架构与执行操作必须携带 project_id 或 project:<projectId> 的 context_id",
        )
    value = _validate_project_id(project_id, context_id)
    return ProjectScope(
        project_id=value, context_type=CONTEXT_TYPE_PROJECT, context_id=project_context_id(value)
    )


def record_project_id(record: Any) -> str:
    """读取记录所属项目；历史记录缺字段时按契约回落 project:default。"""

    raw: Any = None
    if isinstance(record, Mapping):
        raw = record.get("project_id")
    else:
        raw = getattr(record, "project_id", None)
    if raw is None and hasattr(record, "model_dump"):
        try:
            dumped = record.model_dump()
        except Exception:  # noqa: BLE001 - 老记录可能不允许导出
            dumped = None
        if isinstance(dumped, Mapping):
            raw = dumped.get("project_id")
    value = str(raw or "").strip()
    return value or DEFAULT_PROJECT_ID


def ensure_record_in_scope(record: Any, scope: ProjectScope) -> None:
    """校验记录归属；跨项目读取或控制一律在动作前拒绝。"""

    ensure_same_project(record_project_id(record), scope.project_id, context_id=scope.context_id)


def _require_record(record: Any, scope: ProjectScope, label: str) -> Any:
    if record is None:
        _raise(
            ProjectContextErrorCode.PROJECT_NOT_FOUND,
            f"{label} 不存在",
            project_id=scope.project_id,
            context_id=scope.context_id,
        )
    ensure_record_in_scope(record, scope)
    return record


def _first_method(target: Any, names: Sequence[str]) -> Callable[..., Any] | None:
    if target is None:
        return None
    for name in names:
        candidate = getattr(target, name, None)
        if callable(candidate):
            return candidate
    return None


def _method(target: Any, names: Sequence[str], action: str) -> Callable[..., Any]:
    if target is None:
        raise ProjectWorkspaceUnavailableError(
            f"{action}需要既有的执行服务；项目工作区只做边界转发，不实现执行逻辑"
        )
    method = _first_method(target, names)
    if method is None:
        joined = "、".join(names)
        raise ProjectWorkspaceUnavailableError(
            f"{action}：{type(target).__name__} 上没有找到可用实现（候选：{joined}）"
        )
    return method


def _accepts(func: Callable[..., Any], name: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == name:
            return True
    return False


def _invoke(func: Callable[..., Any], project_id: str, context_id: str, /, **kwargs: Any) -> Any:
    """调用既有服务，并在其接受时把项目边界参数带过去。"""

    if _accepts(func, "project_id"):
        kwargs["project_id"] = project_id
    if _accepts(func, "context_id"):
        kwargs["context_id"] = context_id
    return func(**kwargs)


class ProjectWorkspace:
    """项目上下文下的架构 / 执行 / 验证入口（薄适配层）。"""

    def __init__(
        self,
        *,
        orchestrator: Any = None,
        verifier: Any = None,
        executor: Any = None,
        workspace: Any = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._verifier = verifier
        self._executor = executor
        self._workspace = workspace

    # --- 边界解析 ---

    def read_scope(
        self, project_id: str | None = None, context_id: str | None = None
    ) -> ProjectScope:
        return read_scope(project_id, context_id)

    def write_scope(
        self, project_id: str | None = None, context_id: str | None = None
    ) -> ProjectScope:
        return write_scope(project_id, context_id)

    # --- 架构计划 ---

    def create_plan(
        self, *, project_id: str | None = None, context_id: str | None = None, **payload: Any
    ) -> Any:
        scope = write_scope(project_id, context_id)
        method = _method(self._orchestrator, ORCHESTRATOR_PLAN_METHODS, "创建架构计划")
        return _invoke(method, scope.project_id, scope.context_id, **payload)

    def read_plan(
        self, plan_id: str, *, project_id: str | None = None, context_id: str | None = None
    ) -> Any:
        scope = read_scope(project_id, context_id)
        method = _method(self._orchestrator, ORCHESTRATOR_GET_PLAN_METHODS, "读取架构计划")
        record = _invoke(method, scope.project_id, scope.context_id, plan_id=plan_id)
        return _require_record(record, scope, f"架构计划 {plan_id!r}")

    # --- 执行运行 ---

    def start_run(
        self, *, project_id: str | None = None, context_id: str | None = None, **payload: Any
    ) -> Any:
        scope = write_scope(project_id, context_id)
        method = _method(self._orchestrator, ORCHESTRATOR_START_METHODS, "启动运行")
        return _invoke(method, scope.project_id, scope.context_id, **payload)

    def read_run(
        self, run_id: str, *, project_id: str | None = None, context_id: str | None = None
    ) -> Any:
        scope = read_scope(project_id, context_id)
        return _require_record(self._load_run(run_id), scope, f"运行 {run_id!r}")

    def cancel_run(
        self,
        run_id: str,
        *,
        project_id: str | None = None,
        context_id: str | None = None,
        **payload: Any,
    ) -> Any:
        scope = write_scope(project_id, context_id)
        self.read_run(run_id, project_id=scope.project_id, context_id=scope.context_id)
        method = _method(self._orchestrator, ORCHESTRATOR_CANCEL_METHODS, "取消运行")
        return _invoke(method, scope.project_id, scope.context_id, run_id=run_id, **payload)

    def restart_run(
        self,
        run_id: str,
        *,
        project_id: str | None = None,
        context_id: str | None = None,
        **payload: Any,
    ) -> Any:
        scope = write_scope(project_id, context_id)
        self.read_run(run_id, project_id=scope.project_id, context_id=scope.context_id)
        method = _method(self._orchestrator, ORCHESTRATOR_RESTART_METHODS, "重启运行")
        return _invoke(method, scope.project_id, scope.context_id, run_id=run_id, **payload)

    # --- 项目页读模型 ---

    def read_steps(
        self, run_id: str, *, project_id: str | None = None, context_id: str | None = None
    ) -> tuple[Any, ...]:
        scope = read_scope(project_id, context_id)
        method = _first_method(self._orchestrator, ORCHESTRATOR_STEPS_METHODS)
        if method is not None:
            steps = _invoke(method, scope.project_id, scope.context_id, run_id=run_id)
            return tuple(steps or ())
        run = self.read_run(run_id, project_id=scope.project_id, context_id=scope.context_id)
        return tuple(getattr(run, "steps", ()) or ())

    def read_events(
        self, run_id: str, *, project_id: str | None = None, context_id: str | None = None
    ) -> tuple[Any, ...]:
        scope = read_scope(project_id, context_id)
        self.read_run(run_id, project_id=scope.project_id, context_id=scope.context_id)
        method = _first_method(self._orchestrator, ORCHESTRATOR_EVENTS_METHODS)
        if method is None:
            method = _first_method(
                getattr(self._orchestrator, "bus", None), ORCHESTRATOR_EVENTS_METHODS
            )
        if method is None:
            return ()
        events = _invoke(method, scope.project_id, scope.context_id, run_id=run_id)
        return tuple(events or ())

    def read_verification(
        self, run_id: str, *, project_id: str | None = None, context_id: str | None = None
    ) -> Any:
        scope = read_scope(project_id, context_id)
        run = self.read_run(run_id, project_id=scope.project_id, context_id=scope.context_id)
        for target in (self._verifier, self._orchestrator):
            method = _first_method(target, VERIFICATION_READ_METHODS)
            if method is None:
                continue
            record = _invoke(method, scope.project_id, scope.context_id, run_id=run_id)
            if record is not None:
                return record
        return getattr(run, "verification", None)

    def read_metrics(
        self, run_id: str, *, project_id: str | None = None, context_id: str | None = None
    ) -> Any:
        scope = read_scope(project_id, context_id)
        run = self.read_run(run_id, project_id=scope.project_id, context_id=scope.context_id)
        method = _first_method(self._orchestrator, ORCHESTRATOR_METRICS_METHODS)
        if method is not None:
            metrics = _invoke(method, scope.project_id, scope.context_id, run_id=run_id)
            if metrics is not None:
                return metrics
        return getattr(run, "metrics", None)

    # --- 验证请求 ---

    def request_verification(
        self,
        *,
        project_id: str | None = None,
        context_id: str | None = None,
        **payload: Any,
    ) -> Any:
        scope = write_scope(project_id, context_id)
        target = self._verifier if self._verifier is not None else self._orchestrator
        method = _method(target, ORCHESTRATOR_VERIFY_METHODS, "发起步骤验证")
        return _invoke(method, scope.project_id, scope.context_id, **payload)

    # --- 内部 ---

    def _load_run(self, run_id: str) -> Any:
        if self._orchestrator is None:
            raise ProjectWorkspaceUnavailableError(
                "读取运行需要既有的编排服务；项目工作区不复制执行逻辑"
            )
        candidates = (
            (self._orchestrator, ORCHESTRATOR_GET_RUN_METHODS),
            (getattr(self._orchestrator, "store", None), STORE_GET_METHODS),
        )
        for target, names in candidates:
            method = _first_method(target, names)
            if method is None:
                continue
            try:
                record = method(run_id)
            except TypeError:
                continue
            if record is not None:
                return record
        return None
