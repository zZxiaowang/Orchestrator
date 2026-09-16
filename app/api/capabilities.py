"""能力接口：统一管理 skill / MCP / （旧）插件。

这一支 router 是"路由按资源拆分"的第一块（``routes.py`` 只做 include），
以后 skill 安装、MCP server 管理都长在这里，不再往 ``routes.py`` 里堆。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.capabilities.registry import CapabilityRegistry
from app.core.errors import AppError

#: 挂到 ``app/api/routes.py`` 的主 router 上（那边已经有 ``/api/v1`` 前缀），
#: 所以这里只写资源路径，避免出现 ``/api/v1/api/v1/...``
router = APIRouter()

# 支持的形态；界面据此渲染能力中心的分页（新形态只在这里加一项）
CAPABILITY_KINDS = (
    {"id": "skill", "label": "Skills", "hint": "指令包：按需注入执行段上下文"},
    {"id": "mcp", "label": "MCP", "hint": "工具服务器：模型可请求调用，应用层执行后回灌"},
    {"id": "plugin", "label": "插件（legacy）", "hint": "旧的声明式插件，只保留兼容"},
)


class CapabilityScopePatch(BaseModel):
    enabled: bool = Field(..., description="启用 / 停用")


def _registry(request: Request) -> CapabilityRegistry:
    registry = getattr(request.app.state, "capability_registry", None)
    if registry is None:  # pragma: no cover - 兜底：测试里直接挂 app 时补一个
        from pathlib import Path

        from app.core.config import DATA_DIR

        registry = CapabilityRegistry(Path(DATA_DIR) / "capabilities")
        request.app.state.capability_registry = registry
    return registry


def _sync_plugins(request: Request) -> None:
    """把已装插件镜像进能力层（幂等）；插件仓库仍是它的权威存储。

    每次读能力清单前同步一次：这样"装了插件马上能在能力中心看到"，
    不需要重启进程。
    """

    plugin_store = getattr(request.app.state, "plugin_store", None)
    if plugin_store is None:  # pragma: no cover - 兜底
        return
    try:
        _registry(request).sync_plugins(plugin_store.list())
    except Exception:  # noqa: BLE001 - 镜像失败不该挡住能力列表
        return


@router.get("/capabilities")
async def list_capabilities(
    request: Request,
    kind: str | None = None,
    project_id: str = "",
    include_disabled: bool = True,
) -> dict[str, Any]:
    """能力清单：能力中心与执行段注入都用它。"""

    registry = _registry(request)
    _sync_plugins(request)
    items = registry.list(kind=kind, project_id=project_id, include_disabled=include_disabled)
    return {
        "capabilities": [item.describe() for item in items],
        "kinds": [dict(item) for item in CAPABILITY_KINDS],
        "counts": {
            info["id"]: sum(1 for item in items if item.kind.value == info["id"])
            for info in CAPABILITY_KINDS
        },
    }


@router.get("/capabilities/audit")
async def capability_audit(request: Request, limit: int = 100) -> dict[str, Any]:
    return {"events": _registry(request).read_audit(limit=max(1, min(limit, 500)))}


@router.post("/capabilities/{capability_id}/enable")
async def enable_capability(capability_id: str, request: Request) -> dict[str, Any]:
    return {"capability": _registry(request).set_enabled(capability_id, True).describe()}


@router.post("/capabilities/{capability_id}/disable")
async def disable_capability(capability_id: str, request: Request) -> dict[str, Any]:
    return {"capability": _registry(request).set_enabled(capability_id, False).describe()}


@router.delete("/capabilities/{capability_id}")
async def uninstall_capability(capability_id: str, request: Request) -> dict[str, Any]:
    registry = _registry(request)
    capability = registry.require(capability_id)
    if capability.kind.value == "plugin":
        raise AppError(
            "插件请到「插件市场」里卸载：能力层只做镜像，不替它删数据。",
            code="capability_managed_elsewhere",
        )
    registry.remove(capability_id)
    return {"deleted": capability_id}
