"""项目上下文数据契约（第 2 步交付物）。

上位文档：``docs/project-context-contract.md``、``docs/project-navigation-contract.md``。

约定摘要：

* 项目以稳定 ID ``project_id`` 标识；前缀 ``project:`` 只出现在 ``context_id`` 中。
* ``context_id`` 形如 ``project:<projectId>``（项目）或 ``chat:<sessionId>``（普通对话）。
* 计划、运行、步骤、验证结果的读取契约都携带 ``project_id``；历史数据按契约回落到
  ``project:default``，不做数据迁移。
* 缺失 / 无效 / 无权限分别抛出可区分的 ``ProjectContextError``。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTEXT_TYPE_CHAT: Literal["chat"] = "chat"
CONTEXT_TYPE_PROJECT: Literal["project"] = "project"
CONTEXT_TYPES: tuple[str, ...] = (CONTEXT_TYPE_CHAT, CONTEXT_TYPE_PROJECT)
ContextType = Literal["chat", "project"]
DEFAULT_CONTEXT_TYPE: str = CONTEXT_TYPE_PROJECT
DEFAULT_PROJECT_ID = "default"
CONTEXT_ID_SEPARATOR = ":"

PROJECT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_ID_RE = re.compile(PROJECT_ID_PATTERN)


def _now() -> datetime:
    """统一使用 UTC，避免时区差异让契约漂移。"""
    return datetime.now(UTC)


class ProjectContextErrorCode(StrEnum):
    """可区分的项目上下文错误语义。"""

    MISSING_PROJECT_CONTEXT = "missing_project_context"
    INVALID_PROJECT_CONTEXT = "invalid_project_context"
    PROJECT_NOT_FOUND = "project_not_found"
    PROJECT_ACCESS_DENIED = "project_access_denied"


_HTTP_STATUS_BY_CODE: dict[ProjectContextErrorCode, int] = {
    ProjectContextErrorCode.MISSING_PROJECT_CONTEXT: 400,
    ProjectContextErrorCode.INVALID_PROJECT_CONTEXT: 400,
    ProjectContextErrorCode.PROJECT_NOT_FOUND: 404,
    ProjectContextErrorCode.PROJECT_ACCESS_DENIED: 403,
}


class ProjectContextError(Exception):
    """项目上下文错误；``code`` 与 ``status_code`` 供前端区分提示与 HTTP 语义。"""

    def __init__(
        self,
        code: ProjectContextErrorCode | str,
        message: str,
        *,
        project_id: str | None = None,
        context_id: str | None = None,
    ) -> None:
        self.code = ProjectContextErrorCode(code)
        self.message = message
        self.project_id = project_id
        self.context_id = context_id
        super().__init__(message)

    @property
    def status_code(self) -> int:
        return _HTTP_STATUS_BY_CODE[self.code]

    def to_dict(self) -> dict[str, Any]:
        """序列化为前端 / API 可直接使用的错误体。"""
        return {
            "code": self.code.value,
            "message": self.message,
            "status_code": self.status_code,
            "project_id": self.project_id,
            "context_id": self.context_id,
        }

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


def build_context_id(context_type: str, context_key: str) -> str:
    """拼接 ``<contextType>:<key>`` 形式的 context_id，并校验合法性。"""
    if context_type not in CONTEXT_TYPES:
        raise ProjectContextError(
            ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
            f"未知 context_type: {context_type!r}",
        )
    key = "" if context_key is None else str(context_key).strip()
    if not _ID_RE.match(key):
        raise ProjectContextError(
            ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
            f"非法 context key: {context_key!r}",
        )
    return f"{context_type}{CONTEXT_ID_SEPARATOR}{key}"


def parse_context_id(context_id: str | None) -> tuple[str, str]:
    """解析 context_id，返回 ``(context_type, context_key)``。"""
    if context_id is None or not str(context_id).strip():
        raise ProjectContextError(
            ProjectContextErrorCode.MISSING_PROJECT_CONTEXT,
            "缺少 context_id",
            context_id=None if context_id is None else str(context_id),
        )
    raw = str(context_id).strip()
    context_type, separator, key = raw.partition(CONTEXT_ID_SEPARATOR)
    if not separator or context_type not in CONTEXT_TYPES or not _ID_RE.match(key):
        raise ProjectContextError(
            ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
            f"非法 context_id: {raw!r}",
            context_id=raw,
        )
    return context_type, key


def project_context_id(project_id: str) -> str:
    """项目的 context_id：``project:<projectId>``。"""
    return build_context_id(CONTEXT_TYPE_PROJECT, project_id)


def chat_context_id(session_id: str) -> str:
    """普通对话的 context_id：``chat:<sessionId>``。"""
    return build_context_id(CONTEXT_TYPE_CHAT, session_id)


def project_id_from_context_id(context_id: str | None) -> str | None:
    """从 context_id 解析 project_id；普通对话上下文返回 ``None``。"""
    context_type, key = parse_context_id(context_id)
    return key if context_type == CONTEXT_TYPE_PROJECT else None


class ProjectStatus(StrEnum):
    """项目状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ProjectWorkspace(BaseModel):
    """项目与工作区的关联（一个项目绑定一个工作区）。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    root_path: str | None = None
    label: str | None = None
    bound_at: datetime | None = None

    @field_validator("workspace_id")
    @classmethod
    def _validate_workspace_id(cls, value: str) -> str:
        value = (value or "").strip()
        if not _ID_RE.match(value):
            raise ValueError(f"非法 workspace_id: {value!r}")
        return value


class Project(BaseModel):
    """项目数据契约：稳定 ID、名称、状态、工作区关联与最近活动。"""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    name: str
    status: ProjectStatus = ProjectStatus.ACTIVE
    workspace: ProjectWorkspace | None = None
    description: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    last_activity_at: datetime = Field(default_factory=_now)

    @field_validator("project_id")
    @classmethod
    def _validate_project_id(cls, value: str) -> str:
        value = (value or "").strip()
        if not _ID_RE.match(value):
            raise ValueError(f"非法 project_id: {value!r}")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value

    @property
    def context_id(self) -> str:
        return project_context_id(self.project_id)

    @property
    def is_active(self) -> bool:
        return self.status is ProjectStatus.ACTIVE


class ProjectContext(BaseModel):
    """已解析、可信的项目上下文；项目内部模块只接受该对象。"""

    model_config = ConfigDict(extra="forbid")

    context_id: str
    context_type: Literal["project"] = CONTEXT_TYPE_PROJECT
    project_id: str
    project_name: str | None = None
    project_status: ProjectStatus | None = None
    workspace_id: str | None = None
    is_default: bool = False

    @model_validator(mode="after")
    def _check_consistency(self) -> ProjectContext:
        expected = project_context_id(self.project_id)
        if self.context_id != expected:
            raise ValueError(
                f"context_id 与 project_id 不一致: {self.context_id!r} != {expected!r}"
            )
        return self


class ProjectContextResolver:
    """把外部传入的 project_id / context_id 解析为可信的 ProjectContext。"""

    def __init__(
        self,
        projects: Mapping[str, Project] | Iterable[Project] | None = None,
        *,
        allowed_project_ids: Iterable[str] | None = None,
    ) -> None:
        collected: dict[str, Project] = {}
        if isinstance(projects, Mapping):
            for key, project in projects.items():
                collected[project.project_id] = project
                collected.setdefault(str(key), project)
        elif projects is not None:
            for project in projects:
                collected[project.project_id] = project
        self._projects: dict[str, Project] = collected
        self._allowed: frozenset[str] | None = (
            None
            if allowed_project_ids is None
            else frozenset(str(item).strip() for item in allowed_project_ids)
        )

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(sorted({project.project_id for project in self._projects.values()}))

    def get_project(self, project_id: str) -> Project | None:
        return self._projects.get(str(project_id).strip())

    def resolve(
        self,
        context_id: str | None = None,
        *,
        project_id: str | None = None,
        required: bool = True,
    ) -> ProjectContext | None:
        """解析项目上下文；缺失 / 无效 / 不存在 / 无权限分别报错。"""
        if context_id is not None and str(context_id).strip():
            context_type, key = parse_context_id(context_id)
            if context_type != CONTEXT_TYPE_PROJECT:
                raise ProjectContextError(
                    ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
                    f"普通对话上下文不具备项目边界: {context_id!r}",
                    context_id=str(context_id),
                )
            if (
                project_id is not None
                and str(project_id).strip()
                and str(project_id).strip() != key
            ):
                raise ProjectContextError(
                    ProjectContextErrorCode.PROJECT_ACCESS_DENIED,
                    f"context_id 与 project_id 指向不同项目: {context_id!r} / {project_id!r}",
                    project_id=str(project_id).strip(),
                    context_id=str(context_id),
                )
            project_id = key

        if project_id is None or not str(project_id).strip():
            if not required:
                return None
            raise ProjectContextError(
                ProjectContextErrorCode.MISSING_PROJECT_CONTEXT,
                "缺少 project_id 或项目 context_id",
                context_id=None if context_id is None else str(context_id),
            )

        normalized = str(project_id).strip()
        if not _ID_RE.match(normalized):
            raise ProjectContextError(
                ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
                f"非法 project_id: {normalized!r}",
                project_id=normalized,
                context_id=None if context_id is None else str(context_id),
            )

        project = self._projects.get(normalized)
        if project is None:
            raise ProjectContextError(
                ProjectContextErrorCode.PROJECT_NOT_FOUND,
                f"项目不存在: {normalized}",
                project_id=normalized,
                context_id=project_context_id(normalized),
            )

        if self._allowed is not None and normalized not in self._allowed:
            raise ProjectContextError(
                ProjectContextErrorCode.PROJECT_ACCESS_DENIED,
                f"无权访问项目: {normalized}",
                project_id=normalized,
                context_id=project_context_id(normalized),
            )

        return ProjectContext(
            context_id=project_context_id(normalized),
            project_id=normalized,
            project_name=project.name,
            project_status=project.status,
            workspace_id=project.workspace.workspace_id if project.workspace else None,
            is_default=normalized == DEFAULT_PROJECT_ID,
        )


def project_id_from_record(payload: Mapping[str, Any]) -> str:
    """从计划 / 运行 / 步骤 / 验证结果的原始数据解析 project_id。

    优先级：显式 ``project_id`` → ``context_id`` 解析 → 历史数据回落 ``project:default``。
    普通对话上下文不会被当成项目数据读取。
    """
    if not isinstance(payload, Mapping):
        raise ProjectContextError(
            ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
            "记录必须是映射结构",
        )
    explicit = str(payload.get("project_id") or "").strip()
    if explicit:
        return explicit
    raw_context_id = payload.get("context_id") or payload.get("contextId")
    if raw_context_id:
        context_type, key = parse_context_id(raw_context_id)
        if context_type != CONTEXT_TYPE_PROJECT:
            raise ProjectContextError(
                ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
                f"该记录属于普通对话上下文，不能作为项目数据读取: {raw_context_id!r}",
                context_id=str(raw_context_id),
            )
        return key
    return DEFAULT_PROJECT_ID


class ProjectScopedRecord(BaseModel):
    """计划、运行、步骤、验证结果的最小公共读契约（都绑定 project_id）。"""

    model_config = ConfigDict(extra="allow")

    project_id: str
    context_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _inject_project_id(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        payload = dict(data)
        raw_context_id = payload.get("context_id") or payload.get("contextId")
        if raw_context_id is not None:
            payload["context_id"] = str(raw_context_id)
        payload["project_id"] = project_id_from_record(payload)
        return payload

    @field_validator("project_id")
    @classmethod
    def _validate_project_id(cls, value: str) -> str:
        value = (value or "").strip()
        if not _ID_RE.match(value):
            raise ValueError(f"非法 project_id: {value!r}")
        return value


class PlanReadContract(ProjectScopedRecord):
    """计划读取契约。"""

    plan_id: str
    status: str = "draft"
    phases: list[dict[str, Any]] = Field(default_factory=list)


class RunReadContract(ProjectScopedRecord):
    """运行读取契约；缺 ``context_type`` 的历史运行按项目归属。"""

    run_id: str
    status: str = "pending"
    context_type: str = CONTEXT_TYPE_PROJECT
    started_at: datetime | None = None
    updated_at: datetime | None = None


class StepReadContract(ProjectScopedRecord):
    """步骤读取契约。"""

    step_id: str
    run_id: str | None = None
    status: str = "pending"
    order: int | None = None


class VerificationResultReadContract(ProjectScopedRecord):
    """验证结果读取契约。"""

    verification_id: str
    run_id: str | None = None
    step_id: str | None = None
    passed: bool | None = None
    message: str | None = None


READ_CONTRACTS: dict[str, type[ProjectScopedRecord]] = {
    "plan": PlanReadContract,
    "run": RunReadContract,
    "step": StepReadContract,
    "verification": VerificationResultReadContract,
}


def read_project_record(kind: str, payload: Mapping[str, Any]) -> ProjectScopedRecord:
    """按类型读取项目内记录（计划 / 运行 / 步骤 / 验证结果）。"""
    contract = READ_CONTRACTS.get(str(kind))
    if contract is None:
        raise ValueError(f"未知的记录类型: {kind!r}")
    return contract.model_validate(dict(payload))


def ensure_same_project(
    record_project_id: str,
    requested_project_id: str,
    *,
    context_id: str | None = None,
) -> None:
    """读取项目内记录前的跨项目访问拦截。"""
    if str(record_project_id) != str(requested_project_id):
        raise ProjectContextError(
            ProjectContextErrorCode.PROJECT_ACCESS_DENIED,
            f"跨项目访问被拒绝: {requested_project_id!r} 无权读取 {record_project_id!r} 的数据",
            project_id=str(requested_project_id),
            context_id=context_id,
        )
