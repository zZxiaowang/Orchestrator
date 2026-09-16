"""能力接口：统一管理 skill / MCP / （旧）插件。

这一支 router 是"路由按资源拆分"的第一块（``routes.py`` 只做 include），
以后 skill 安装、MCP server 管理都长在这里，不再往 ``routes.py`` 里堆。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.capabilities import mcp as mcp_module
from app.capabilities.registry import CapabilityRegistry, resolve_callable_mcp
from app.capabilities.skills import (
    install_from_github,
    install_from_zip_url,
    install_local,
    read_skill_body,
)
from app.core.errors import AppError
from app.schemas.capability import Capability, CapabilityKind, CapabilityScope, CapabilitySource

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


class InstallSkillRequest(BaseModel):
    """装一个 skill：本地目录 / GitHub / zip 地址。"""

    source: Literal["local", "github", "zip"] = "local"
    location: str = Field(..., description="本地目录、owner/repo#ref[/子目录]，或 zip 地址")
    scope: Literal["global", "project"] = "global"
    project_id: str = ""
    enabled: bool = True


class SkillScopeRequest(BaseModel):
    scope: Literal["global", "project"] = "global"
    project_id: str = ""


class McpServerRequest(BaseModel):
    """新增一个 MCP 服务器：用预设，或自己填传输方式。"""

    preset_id: str = Field("", description="预设 id；填了就按预设填命令，其余字段可覆盖")
    name: str = ""
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    scope: Literal["global", "project"] = "global"
    project_id: str = ""
    enabled: bool = False
    trusted: bool = False


class McpCallRequest(BaseModel):
    tool: str = Field(..., description="工具名")
    arguments: dict[str, Any] = Field(default_factory=dict)


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


def _download_transport(request: Request) -> Any:
    """下载技能包用的 httpx transport：测试里注入假 transport，生产用默认。"""

    return getattr(request.app.state, "capability_transport", None)


@router.post("/capabilities/skills/install", status_code=201)
async def install_skill(payload: InstallSkillRequest, request: Request) -> dict[str, Any]:
    """安装 skill：本地目录 / GitHub（owner/repo#ref[/子目录]）/ zip 地址。

    安装只做"复制 + 资格校验"，**不执行任何脚本**；带 ``scripts/`` 的技能会在
    permissions 里登记，真正执行仍走命令白名单 + 每次确认。
    """

    registry = _registry(request)
    scope = CapabilityScope(payload.scope)
    common = {
        "root": registry.root,
        "scope": scope,
        "project_id": payload.project_id,
        "enabled": payload.enabled,
    }
    if payload.source == "local":
        path = Path(payload.location).expanduser()
        if not path.is_absolute():
            # 相对路径按"技能来源目录"解析，避免受进程 cwd 影响产生歧义
            path = (Path(registry.root).parent.parent / payload.location).resolve()
        capability = install_local(path, **common)
    elif payload.source == "github":
        capability = await install_from_github(
            payload.location, transport=_download_transport(request), **common
        )
    else:
        capability = await install_from_zip_url(
            payload.location, transport=_download_transport(request), **common
        )
    stored = registry.upsert(capability)
    return {"capability": stored.describe(), "installed": True}


@router.get("/capabilities/{capability_id}/body")
async def capability_body(capability_id: str, request: Request) -> dict[str, Any]:
    """读回 skill 正文（界面预览用；注入执行段走的是同一份内容）。"""

    capability = _registry(request).require(capability_id)
    if capability.kind is not CapabilityKind.SKILL:
        raise AppError("只有 skill 有正文可以查看。", code="capability_has_no_body")
    body = read_skill_body(Path(str(capability.meta.get("path") or "")))
    return {"id": capability.id, "name": capability.name, "body": body}


@router.post("/capabilities/{capability_id}/scope")
async def set_capability_scope(
    capability_id: str, payload: SkillScopeRequest, request: Request
) -> dict[str, Any]:
    """改能力作用域：全局启用，或只在某个项目里生效（项目之间互不影响）。"""

    registry = _registry(request)
    capability = registry.require(capability_id)
    scope = CapabilityScope(payload.scope)
    if scope is CapabilityScope.PROJECT and not payload.project_id.strip():
        raise AppError("按项目生效时必须给出 project_id。", code="invalid_request")
    updated = capability.model_copy(
        update={
            "scope": scope,
            "project_id": payload.project_id.strip() if scope is CapabilityScope.PROJECT else "",
        }
    )
    return {"capability": registry.upsert(updated, record="scope").describe()}


# ── MCP：预设 / 新增服务器 / 信任确认 / 工具清单 / 手动调用 ──


def _resolve_mcp(registry: CapabilityRegistry, capability_id: str, *, project_id: str = ""):
    """取一个**可调用**的 MCP 服务器（三道闸门见 ``resolve_callable_mcp``）。"""

    return resolve_callable_mcp(registry, capability_id, project_id=project_id)


@router.get("/capabilities/mcp/presets")
async def mcp_presets() -> dict[str, Any]:
    """常用 MCP 服务器预设（只给启动方式，不含密钥；装了默认不启用）。"""

    return {"presets": [dict(item) for item in mcp_module.MCP_PRESETS]}


@router.post("/capabilities/mcp/servers", status_code=201)
async def add_mcp_server(payload: McpServerRequest, request: Request) -> dict[str, Any]:
    """新增 MCP 服务器：默认 **不启用、未确认信任**（会跑代码的能力不该默认放开）。"""

    registry = _registry(request)
    preset = mcp_module.preset_by_id(payload.preset_id) if payload.preset_id else None
    if payload.preset_id and preset is None:
        raise AppError(f"没有这个预设：{payload.preset_id}", code="mcp_preset_not_found")

    meta: dict[str, Any] = {
        "transport": payload.transport if not preset else preset["transport"],
        "command": payload.command or (preset or {}).get("command", ""),
        "args": payload.args or list((preset or {}).get("args") or []),
        "env": dict(payload.env or {}),
        "cwd": payload.cwd,
        "url": payload.url,
        "headers": dict(payload.headers or {}),
        "trusted": bool(payload.trusted),
        "tools": [],
        "tools_fetched_at": "",
        "preset_id": payload.preset_id,
    }
    if payload.preset_id == "demo" and not payload.command and not payload.args:
        # 示例服务器是仓库自带脚本：用绝对路径 + 当前解释器，避免受 cwd 影响
        meta["command"], meta["args"] = _demo_server_launcher()
    if meta["transport"] == "stdio" and not meta["command"]:
        raise AppError("stdio 传输必须填 command。", code="invalid_mcp_server")
    if meta["transport"] == "http" and not meta["url"]:
        raise AppError("HTTP 传输必须填 url。", code="invalid_mcp_server")

    name = payload.name or (preset or {}).get("name") or meta["command"] or meta["url"]
    capability_id = f"mcp.{_slug(name)}"
    capability = Capability(
        id=capability_id,
        kind=CapabilityKind.MCP,
        name=str(name)[:80],
        description=str((preset or {}).get("description") or ""),
        enabled=payload.enabled,
        scope=CapabilityScope(payload.scope),
        project_id=payload.project_id if payload.scope == "project" else "",
        permissions=["tools", "network" if meta["transport"] == "http" else "process"],
        source=CapabilitySource(
            kind="builtin" if preset else "local",
            location=f"preset:{payload.preset_id}" if preset else (meta["command"] or meta["url"]),
        ),
        meta=meta,
    )
    stored = registry.upsert(capability)
    return {"capability": stored.describe()}


def _slug(value: str) -> str:
    import re

    text = re.sub(r"[^a-z0-9._-]+", "-", str(value).strip().lower()).strip("-._")
    text = text[:48].strip("-._")
    if text and re.match(r"^[a-z0-9]", text):
        return text
    import hashlib

    return "server-" + hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:8]


def _demo_server_launcher() -> tuple[str, list[str]]:
    """示例 MCP 服务器的启动命令：绝对路径 + 可用解释器。

    打包版里 ``sys.executable`` 是 exe 自己（不是 Python 解释器），所以冻结时退回 PATH 上的
    ``python``，找不到就报一条能看懂的错。
    """

    import sys

    from app.core.config import RESOURCE_ROOT

    script = Path(RESOURCE_ROOT) / "scripts" / "demo_mcp_server.py"
    if not script.is_file():
        raise AppError(
            "示例 MCP 服务器脚本不在（打包版需要把 scripts/demo_mcp_server.py 一起打进去）。",
            code="mcp_demo_missing",
        )
    if not getattr(sys, "frozen", False):
        return sys.executable, [str(script)]
    return "python", [str(script)]


@router.post("/capabilities/{capability_id}/trust")
async def trust_mcp_server(capability_id: str, request: Request) -> dict[str, Any]:
    """首次确认：明确告诉用户"它会以本机权限跑代码"，确认后才允许调用工具。"""

    registry = _registry(request)
    capability = registry.require(capability_id)
    if capability.kind is not CapabilityKind.MCP:
        raise AppError("只有 MCP 服务器需要确认信任。", code="not_an_mcp_server")
    updated = capability.model_copy(update={"meta": {**capability.meta, "trusted": True}})
    return {"capability": registry.upsert(updated, record="trust").describe()}


@router.get("/capabilities/{capability_id}/mcp/tools")
async def mcp_tools(capability_id: str, request: Request, refresh: bool = False) -> dict[str, Any]:
    """列出服务器的工具（默认吃缓存；``refresh=true`` 强制重取）。"""

    registry = _registry(request)
    capability = _resolve_mcp(registry, capability_id)
    cached = capability.meta.get("tools") or []
    fetched_at = str(capability.meta.get("tools_fetched_at") or "")
    if cached and not refresh and _cache_fresh(fetched_at):
        return {"tools": cached, "cached": True, "fetched_at": fetched_at}
    try:
        tools = await mcp_module.list_tools(capability, transport=_download_transport(request))
    except AppError as exc:
        return {
            "tools": cached,
            "cached": bool(cached),
            "error": exc.message,
            "fetched_at": fetched_at,
        }
    payload = [item.describe() for item in tools]
    updated = capability.model_copy(
        update={
            "meta": {
                **capability.meta,
                "tools": payload,
                "tools_fetched_at": _now_iso(),
            }
        }
    )
    stored = registry.upsert(updated, record="mcp_tools")
    return {
        "tools": payload,
        "cached": False,
        "fetched_at": stored.meta.get("tools_fetched_at", ""),
    }


@router.post("/capabilities/{capability_id}/mcp/call")
async def mcp_call(capability_id: str, payload: McpCallRequest, request: Request) -> dict[str, Any]:
    """手动调用一个工具（和模型自主调用走同一条闸门与同一条执行路径）。"""

    registry = _registry(request)
    capability = _resolve_mcp(
        registry, capability_id, project_id=request.query_params.get("project_id", "")
    )
    result = await mcp_module.call_tool(
        capability,
        payload.tool,
        payload.arguments,
        transport=_download_transport(request),
    )
    registry.touch(capability_id)
    registry.audit(
        {
            "event": "mcp_call",
            "id": capability_id,
            "tool": payload.tool,
            "ok": result.ok,
            "duration_ms": result.duration_ms,
            "manual": True,
        }
    )
    return {"result": result.as_dict(), "capability": capability.describe()}


def _cache_fresh(fetched_at: str, *, ttl_seconds: int = 600) -> bool:
    from datetime import UTC, datetime

    if not fetched_at:
        return False
    try:
        moment = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return (datetime.now(UTC) - moment).total_seconds() < ttl_seconds


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


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
