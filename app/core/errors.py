"""统一错误类型。

原则：错误信息必须**可操作**——指出哪个字段/哪一步出问题以及怎么改，
而不是把底层异常直接抛给界面。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """应用级错误基类。"""

    code = "app_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigurationError(AppError):
    """配置缺失或互相矛盾。"""

    code = "configuration_error"


class RelayError(AppError):
    """中转网关调用失败（网络、鉴权、参数、限流）。"""

    code = "relay_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(details or {})
        if status_code is not None:
            payload["status_code"] = status_code
        if hint:
            payload["hint"] = hint
        super().__init__(message, details=payload)
        self.status_code = status_code
        self.hint = hint


class PlanParseError(AppError):
    """架构段返回内容无法解析成结构化纲领。"""

    code = "plan_parse_error"


class StepExecutionError(AppError):
    """执行段某一步失败（含落地文件失败）。"""

    code = "step_execution_error"


class WorkspaceError(AppError):
    """工作区路径非法或写入被拒绝。"""

    code = "workspace_error"


class NotFoundError(AppError):
    """请求的资源不存在。"""

    code = "not_found"
