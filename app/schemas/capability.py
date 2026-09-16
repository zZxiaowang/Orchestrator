"""能力契约：skill / MCP / （旧）插件统一成同一种可安装、可启用的能力。

为什么要有这一层：现在插件是一种存储、MCP 又会是一种、skill 还是一种。如果每种形态都
自己一套安装 / 启用 / 作用域 / 审计，导航、设置、前端每加一种就要再改一遍——这正是
"项目模块清单改四处"的老问题放大版。

约定：

* ``kind`` 决定能力形态（skill = 指令包；mcp = 工具服务器；plugin = 旧声明式插件）；
* ``scope`` 决定生效范围：全局安装、可按项目启用（项目之间不共享启用状态）；
* ``permissions`` 只做**声明与展示**，真正的放行由执行层（命令白名单 / 首次确认）决定；
* ``meta`` 放各形态自己的字段（skill 的来源与入口、mcp 的传输与命令、plugin 的贡献点）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 能力 ID：小写字母/数字，允许 . _ -（和插件 ID 规则一致）
CAPABILITY_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_ID_RE = re.compile(CAPABILITY_ID_PATTERN)


def _now() -> datetime:
    return datetime.now(UTC)


class CapabilityKind(StrEnum):
    """能力形态。``plugin`` 是历史形态，保留兼容但不再投入新功能。"""

    SKILL = "skill"
    MCP = "mcp"
    PLUGIN = "plugin"


class CapabilityScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"


class CapabilitySource(BaseModel):
    """能力从哪来：本地目录 / HTTPS 清单 / GitHub / 内置预设。"""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["local", "https", "github", "builtin"] = "local"
    location: str = ""
    record_id: str = ""


class Capability(BaseModel):
    """一条已安装能力。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: CapabilityKind = CapabilityKind.SKILL
    name: str = ""
    description: str = ""
    version: str = ""
    source: CapabilitySource | None = None
    enabled: bool = True
    scope: CapabilityScope = CapabilityScope.GLOBAL
    #: scope=project 时生效的项目；全局能力留空
    project_id: str = ""
    #: 声明式权限（展示与审计用）：instructions / scripts / network / filesystem / tools …
    permissions: list[str] = Field(default_factory=list)
    #: 各形态自己的字段
    meta: dict[str, Any] = Field(default_factory=dict)
    installed_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    last_used_at: datetime | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        text = (value or "").strip().lower()
        if not _ID_RE.match(text):
            raise ValueError(f"非法能力 ID：{value!r}（只允许小写字母、数字、. _ -）")
        return text

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return " ".join((value or "").split())[:120]

    @property
    def kind_value(self) -> str:
        return self.kind.value

    def is_active_for(self, project_id: str = "") -> bool:
        """这条能力对某个项目是否生效：全局的要启用，项目的还要项目匹配。"""

        if not self.enabled:
            return False
        if self.scope is CapabilityScope.GLOBAL:
            return True
        return bool(project_id) and self.project_id == project_id

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name or self.id,
            "description": self.description,
            "version": self.version,
            "source": self.source.model_dump(mode="json") if self.source else None,
            "enabled": self.enabled,
            "scope": self.scope.value,
            "project_id": self.project_id,
            "permissions": list(self.permissions),
            "meta": dict(self.meta),
            "installed_at": self.installed_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }


class ToolCallRequest(BaseModel):
    """一次工具调用的请求（应用层协议：模型输出它，应用执行后回灌结果）。

    走应用层而不是依赖网关的 function-calling：中转不一定支持 ``tools`` 字段，
    而这个协议只要求模型能输出 JSON——和现有 ``need_files`` / ``commands`` 同一套做法。
    """

    model_config = ConfigDict(extra="ignore")

    capability_id: str = ""
    tool: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
