"""API 路由。

约定：成功直接返回资源对象；失败返回 ``{"error": {code, message, details}}``。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.core.catalog import CatalogStore, supported_capabilities
from app.core.config import OVERRIDABLE_FIELDS, Settings, get_settings, settings_store
from app.core.errors import AppError, NotFoundError
from app.core.plugins import PluginStore
from app.core.providers import KIND_PRESETS
from app.core.relay import RelayClient
from app.schemas.navigation import (
    PROJECT_LIST_ROUTE,
    PROJECT_MODULES,
    PROJECT_ONLY_ACTIONS,
    NavEntry,
    ProjectModule,
    project_module_context,
)
from app.services.git_service import GitService
from app.services.orchestrator import Orchestrator
from app.services.workspace import Workspace

router = APIRouter(prefix="/api/v1")

#: 客户端（界面/桌面窗口）上报的错误，最近若干条留在内存里供排查
CLIENT_LOGS: deque[dict[str, Any]] = deque(maxlen=200)
logger = logging.getLogger("app.client")


class CreateRunRequest(BaseModel):
    task: str = Field(..., min_length=1, description="需求描述")
    title: str = ""
    target_dir: str = Field("", description="落地目录；留空则在运行目录内新建工作区")
    context: str = Field("", description="补充说明/约束")
    brief: str = Field("", description="前期沟通简报（可与 context 并用，两段都会读到）")
    auto_execute: bool = Field(False, description="生成纲领后是否自动开始执行")


class ApproveRequest(BaseModel):
    feedback: str = Field("", description="填写则按反馈重新生成纲领，否则开始执行")


class ResumeRequest(BaseModel):
    note: str = Field("", description="补充说明：缺什么就补什么")
    target_dir: str = Field("", description="可选：指定要改动的现有目录")
    stop_after_step: int | None = Field(None, description="跑到该步后暂停；不传则一路跑完")


class RetryStepRequest(BaseModel):
    note: str = Field("", description="这次重做要额外交代什么")
    stop_after: bool = Field(True, description="只跑这一步就停下")


class ContinueRequest(BaseModel):
    instruction: str = Field("", description="继续这项任务要做什么（追加要求）")


class SystemRestartRequest(BaseModel):
    rebuild: bool = Field(True, description="重启前是否重新打包（源码改动才会生效）")
    confirm: bool = Field(False, description="必须显式确认：这个动作会结束当前程序")


#: 自开发预设：本项目的质量门。命令按前缀匹配，所以带参数的写法也能命中。
DEV_QUALITY_GATES: tuple[str, ...] = (
    "python -m pytest",
    "python -m ruff",
    "node scripts/ui_check.mjs",
    "powershell -File scripts/package.ps1",
)


class MarketSourceRequest(BaseModel):
    manifest_url: str = Field(..., min_length=8, description="目录清单的 HTTPS 地址")


class PluginInstallRequest(BaseModel):
    source_record_id: str = Field("", description="目录来源；留空表示在所有来源里找")
    item_id: str = Field(..., min_length=1, description="目录条目 id")


class RunMetaRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    archived: bool | None = None


class ClientLogRequest(BaseModel):
    level: str = "error"
    message: str = ""
    stack: str = ""
    url: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class SettingsPatch(BaseModel):
    relay_base_url: str | None = None
    relay_api_key: str | None = None
    relay_wire_api: str | None = None
    architect_model: str | None = None
    architect_base_url: str | None = None
    architect_api_key: str | None = None
    architect_wire_api: str | None = None
    editor_model: str | None = None
    editor_base_url: str | None = None
    editor_api_key: str | None = None
    editor_wire_api: str | None = None
    max_plan_steps: int | None = None
    allow_command_execution: bool | None = None
    command_allowlist: list[str] | None = None
    command_timeout_seconds: float | None = None
    step_command_rounds: int | None = None
    request_timeout_seconds: float | None = None
    # 主备降级：主用失败（502/503/超时）时自动切到备用配置
    architect_backup_base_url: str | None = None
    architect_backup_api_key: str | None = None
    architect_backup_wire_api: str | None = None
    architect_backup_model: str | None = None
    architect_backup_label: str | None = None
    editor_backup_base_url: str | None = None
    editor_backup_api_key: str | None = None
    editor_backup_wire_api: str | None = None
    editor_backup_model: str | None = None
    editor_backup_label: str | None = None


class ProviderPayload(BaseModel):
    name: str | None = None
    kind: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    wire_api: str | None = None
    architect_model: str | None = None
    editor_model: str | None = None
    #: 新建时是否直接切换为当前配置（界面"新建"传 false，先编辑再切换）
    activate: bool | None = None


class RouteAssignment(BaseModel):
    provider_id: str = ""
    model: str = ""


class RoutesPayload(BaseModel):
    architect: RouteAssignment | None = None
    editor: RouteAssignment | None = None


def _orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator  # type: ignore[no-any-return]


def _settings(request: Request) -> Settings:
    provider = getattr(request.app.state, "settings_provider", get_settings)
    return provider()


def _store(request: Request):
    return getattr(request.app.state, "settings_store", settings_store)


def _plugins(request: Request) -> PluginStore:
    return request.app.state.plugin_store  # type: ignore[no-any-return]


def _catalog(request: Request) -> CatalogStore:
    return request.app.state.catalog_store  # type: ignore[no-any-return]


def _git(request: Request) -> GitService:
    """Git 面板操作的仓库：默认就是本项目目录（可用 GIT_DIR 覆盖）。"""
    from app.core.config import project_root

    settings = _settings(request)
    repo = Path(settings.git_dir).expanduser() if settings.git_dir else project_root()
    return GitService(repo)


def plugins_payload(store: PluginStore) -> dict[str, Any]:
    return {
        "plugins": [plugin.describe() for plugin in store.list()],
        "footer_actions": store.footer_actions(),
    }


def _mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"


def settings_payload(settings: Settings, store=None) -> dict[str, Any]:
    architect = settings.resolve_architect()
    editor = settings.resolve_editor()
    problems = settings.config_problems()
    missing = [endpoint.describe() for endpoint in settings.missing_endpoints()]
    payload: dict[str, Any] = {
        "relay_base_url": settings.relay_base_url,
        "relay_api_key_masked": _mask_key(settings.relay_api_key),
        "relay_api_key_set": bool(settings.relay_api_key),
        "relay_wire_api": settings.relay_wire_api,
        "max_plan_steps": settings.max_plan_steps,
        "allow_command_execution": settings.allow_command_execution,
        "command_allowlist": list(settings.command_allowlist),
        "command_timeout_seconds": settings.command_timeout_seconds,
        "step_command_rounds": settings.step_command_rounds,
        "request_timeout_seconds": settings.request_timeout_seconds,
        # 备用配置：Key 只回掩码，界面据此显示"已配置/留空不修改"
        "architect_backup": {
            "base_url": settings.architect_backup_base_url,
            "api_key_masked": _mask_key(settings.architect_backup_api_key),
            "api_key_set": bool(settings.architect_backup_api_key),
            "wire_api": settings.architect_backup_wire_api,
            "model": settings.architect_backup_model,
            "label": settings.architect_backup_label,
        },
        "editor_backup": {
            "base_url": settings.editor_backup_base_url,
            "api_key_masked": _mask_key(settings.editor_backup_api_key),
            "api_key_set": bool(settings.editor_backup_api_key),
            "wire_api": settings.editor_backup_wire_api,
            "model": settings.editor_backup_model,
            "label": settings.editor_backup_label,
        },
        "architect": architect.describe(),
        "editor": editor.describe(),
        "missing": missing,
        "problems": problems,
        "ready": not missing and not problems,
    }
    if store is not None:
        payload["providers"] = [profile.describe() for profile in store.providers()]
        active = store.active_provider()
        payload["active_provider"] = active.describe() if active else None
        payload["routes"] = store.routes()
    return payload


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    problems = settings.config_problems()
    missing = settings.missing_endpoints()
    return {
        "status": "ok",
        "ready": not missing and not problems,
        "missing_endpoints": [e.role for e in missing],
        "problems": problems,
    }


@router.post("/system/restart", status_code=202)
async def system_restart(payload: SystemRestartRequest, request: Request) -> dict[str, Any]:
    """重新打包并重启（改完自己的源码后用）。

    这是一个**会结束当前进程**的动作，所以必须显式 ``confirm=true``；
    实际的重启由外部辅助脚本完成（等本进程退出 → 打包 → 拉起新实例）。
    """

    if not payload.confirm:
        raise AppError(
            "这会结束当前程序，请带上 confirm=true 再调用。",
            code="need_confirm",
        )
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise AppError("测试环境不允许重启。", code="restart_unsupported")

    from app.core.config import DATA_DIR, get_settings, is_frozen, project_root
    from app.services.restart import helper_available, perform_restart

    root = project_root()
    if not helper_available(root):
        raise AppError(
            f"找不到重启辅助脚本：{root}\\scripts\\restart.ps1（更新到最新版本后再试）",
            code="restart_unsupported",
        )
    settings = get_settings()
    result = perform_restart(
        data_dir=DATA_DIR,
        project_root=root,
        rebuild=payload.rebuild,
        host=settings.host,
        port=settings.port,
    )
    result["frozen"] = is_frozen()
    # 给这次 HTTP 响应留出返回时间，然后结束自己；新实例由辅助脚本拉起
    threading.Timer(1.5, lambda: os._exit(0)).start()
    return result


@router.get("/system/info")
async def system_info() -> dict[str, Any]:
    """运行形态信息：仓库根、数据目录、是否打包版——界面据此做"开发模式"。"""

    from app.core.config import DATA_DIR, is_frozen, project_root

    root = project_root()
    return {
        "project_root": str(root),
        "data_dir": str(DATA_DIR),
        "frozen": is_frozen(),
        "is_git_repo": (root / ".git").exists(),
        "quality_gates": list(DEV_QUALITY_GATES),
    }


@router.post("/system/dev-preset")
async def apply_dev_preset(request: Request) -> dict[str, Any]:
    """一键配置"自开发"：允许执行验证命令 + 白名单填本项目质量门 + 允许自动修正。"""

    store = _store(request)
    settings = store.update(
        {
            "allow_command_execution": True,
            "command_allowlist": list(DEV_QUALITY_GATES),
            "step_command_rounds": 2,
        }
    )
    return settings_payload(settings, store)


@router.post("/client-log", status_code=202)
async def client_log(payload: ClientLogRequest) -> dict[str, Any]:
    """界面/桌面窗口上报的前端错误（点不动之类的问题靠它留证）。"""
    entry = {
        "level": payload.level[:16],
        "message": payload.message[:500],
        "stack": payload.stack[:2000],
        "url": payload.url[:300],
        "context": payload.context,
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    CLIENT_LOGS.append(entry)
    logger.warning("[客户端] %s %s | %s", entry["level"], entry["message"], entry["stack"][:200])
    return {"ok": True, "count": len(CLIENT_LOGS)}


@router.get("/client-log")
async def read_client_log(limit: int = 50) -> dict[str, Any]:
    items = list(CLIENT_LOGS)[-max(1, min(limit, 200)) :]
    return {"count": len(CLIENT_LOGS), "items": items}


@router.get("/settings")
async def read_settings(request: Request) -> dict[str, Any]:
    return settings_payload(_settings(request), _store(request))


@router.put("/settings")
async def update_settings(patch: SettingsPatch, request: Request) -> dict[str, Any]:
    raw = {k: v for k, v in patch.model_dump().items() if v is not None}
    unknown = set(raw) - set(OVERRIDABLE_FIELDS)
    if unknown:
        raw = {k: v for k, v in raw.items() if k not in unknown}
    store = getattr(request.app.state, "settings_store", settings_store)
    settings = store.update(raw)
    return settings_payload(settings, store)


@router.get("/runs")
async def list_runs(
    request: Request, include_archived: bool = False, q: str = ""
) -> dict[str, Any]:
    runs = _orchestrator(request).store.list_runs(include_archived=include_archived, query=q)
    return {"runs": runs, "total": len(runs)}


@router.patch("/runs/{run_id}")
async def update_run_meta(run_id: str, payload: RunMetaRequest, request: Request) -> dict[str, Any]:
    """重命名 / 置顶 / 归档一个运行（Codex 式任务列表管理）。"""
    run = _orchestrator(request).update_run_meta(
        run_id, title=payload.title, pinned=payload.pinned, archived=payload.archived
    )
    return {"run": run.model_dump(mode="json"), "summary": run.summarize()}


@router.post("/settings/test")
async def test_settings(request: Request) -> dict[str, Any]:
    """连通性自检：对架构段/执行段各发一次 ``GET /models``（不消耗额度）。"""
    settings = _settings(request)
    transport = getattr(request.app.state, "relay_transport", None)
    results: dict[str, Any] = {}
    for endpoint in (settings.resolve_architect(), settings.resolve_editor()):
        if not endpoint.configured:
            missing = [
                name
                for name, value in (
                    ("base_url", endpoint.base_url),
                    ("api_key", endpoint.api_key),
                    ("model", endpoint.model),
                )
                if not str(value).strip()
            ]
            results[endpoint.role] = {
                "ok": False,
                "message": f"配置不完整，缺少：{', '.join(missing)}",
            }
            continue

        client = RelayClient(
            endpoint.base_url,
            endpoint.api_key,
            wire_api=endpoint.wire_api,
            timeout=20,
            transport=transport,
            model_hint=endpoint.model,
        )
        try:
            result = await client.aping()
        except AppError as exc:
            result = {"ok": False, "message": exc.message, **exc.details}
        result["model"] = endpoint.model
        results[endpoint.role] = result
    return {"results": results, "ok": all(item.get("ok") for item in results.values())}


@router.get("/providers")
async def list_providers(request: Request) -> dict[str, Any]:
    store = _store(request)
    profiles = store.providers()
    active = store.active_provider()
    return {
        "providers": [profile.describe() for profile in profiles],
        "active_provider_id": active.id if active else "",
        "presets": KIND_PRESETS,
    }


@router.post("/providers", status_code=201)
async def create_provider(payload: ProviderPayload, request: Request) -> dict[str, Any]:
    data = {key: value for key, value in payload.model_dump().items() if value is not None}
    activate = bool(data.pop("activate", True))
    store = _store(request)
    profile = store.create_provider(data, activate=activate)
    payload_out = settings_payload(store.get(), store)
    payload_out["provider_id"] = profile.id
    return payload_out


@router.put("/providers/{provider_id}")
async def update_provider(
    provider_id: str, payload: ProviderPayload, request: Request
) -> dict[str, Any]:
    data = {key: value for key, value in payload.model_dump().items() if value is not None}
    data.pop("activate", None)
    store = _store(request)
    profile = store.update_provider(provider_id, data)
    payload_out = settings_payload(store.get(), store)
    payload_out["provider_id"] = profile.id
    return payload_out


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    return settings_payload(store.delete_provider(provider_id), store)


@router.post("/providers/{provider_id}/activate")
async def activate_provider(provider_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    return settings_payload(store.activate_provider(provider_id), store)


@router.get("/routes")
async def read_routes(request: Request) -> dict[str, Any]:
    store = _store(request)
    return {"routes": store.routes(), "providers": [p.describe() for p in store.providers()]}


@router.put("/routes")
async def update_routes(payload: RoutesPayload, request: Request) -> dict[str, Any]:
    """分段指定：架构段与执行段各用哪套配置、哪个模型。

    传 ``provider_id: ""`` 表示该段"跟随当前配置"。
    """
    store = _store(request)
    raw = {
        role: assignment.model_dump()
        for role, assignment in (
            ("architect", payload.architect),
            ("editor", payload.editor),
        )
        if assignment is not None
    }
    settings = store.set_routes(raw)
    return settings_payload(settings, store)


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request) -> dict[str, Any]:
    """测试指定配置，但**不**切换当前配置。"""
    store = _store(request)
    profile = next((item for item in store.providers() if item.id == provider_id), None)
    if profile is None:
        raise NotFoundError(f"未找到该配置：{provider_id}", details={"provider_id": provider_id})
    transport = getattr(request.app.state, "relay_transport", None)
    results: dict[str, Any] = {}
    for role, model in (
        ("architect", profile.architect_model),
        ("editor", profile.editor_model),
    ):
        results[role] = await _ping_endpoint(
            base_url=profile.base_url,
            api_key=profile.api_key,
            wire_api=profile.wire_api,
            model=model,
            transport=transport,
        )
    return {
        "profile": profile.describe(),
        "results": results,
        "ok": all(item.get("ok") for item in results.values()),
    }


async def _ping_endpoint(
    *,
    base_url: str,
    api_key: str,
    wire_api: str,
    model: str,
    transport,
) -> dict[str, Any]:
    """对一个端点做一次不消耗额度的连通性探测。"""
    missing = [
        name
        for name, value in (("base_url", base_url), ("api_key", api_key), ("model", model))
        if not str(value or "").strip()
    ]
    if missing:
        return {
            "ok": False,
            "model": model,
            "message": f"配置不完整，缺少：{', '.join(missing)}",
        }

    client = RelayClient(
        base_url,
        api_key,
        wire_api=wire_api,
        timeout=20,
        transport=transport,
        model_hint=model,
    )
    try:
        result = await client.aping()
    except AppError as exc:
        result = {"ok": False, "message": exc.message, **exc.details}
    result["model"] = model
    return result


@router.post("/runs", status_code=201)
async def create_run(payload: CreateRunRequest, request: Request) -> dict[str, Any]:
    orchestrator = _orchestrator(request)
    run = orchestrator.create_run(
        payload.task,
        title=payload.title,
        target_dir=payload.target_dir,
        context=payload.context,
        brief=payload.brief,
    )
    orchestrator.start_planning(run.id)
    return {"run": run.model_dump(mode="json"), "auto_execute": payload.auto_execute}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    orchestrator = _orchestrator(request)
    run = orchestrator.store.load(run_id)
    return {
        "run": run.model_dump(mode="json"),
        "summary": run.summarize(),
        # 客户端据此增量订阅，避免把已渲染过的历史事件重复回放
        "event_seq": orchestrator.bus.current_seq(run_id),
    }


@router.post("/runs/{run_id}/approve")
async def approve_run(run_id: str, payload: ApproveRequest, request: Request) -> dict[str, Any]:
    orchestrator = _orchestrator(request)
    orchestrator.store.load(run_id)  # 不存在时直接 404
    feedback = payload.feedback.strip()
    if feedback:
        orchestrator.start_planning(run_id, feedback=feedback)
        return {"run": orchestrator.store.load(run_id).model_dump(mode="json"), "action": "replan"}
    orchestrator.start_execution(run_id)
    return {"run": orchestrator.store.load(run_id).model_dump(mode="json"), "action": "execute"}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    run = _orchestrator(request).cancel(run_id)
    return {"run": run.model_dump(mode="json")}


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: str, request: Request) -> dict[str, Any]:
    """重试失败的运行：规划失败就重新规划，执行失败就从失败步骤继续。

    502/503 这类网关抖动是暂时的，用户不该为此重建任务、重看一遍纲领。
    """

    run = _orchestrator(request).retry_run(run_id)
    return {"run": run.model_dump(mode="json"), "action": "retry_run"}


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, payload: ResumeRequest, request: Request) -> dict[str, Any]:
    """被阻塞/失败后补充信息并继续：只会重跑未完成的那一步。"""
    run = _orchestrator(request).resume(
        run_id,
        note=payload.note,
        target_dir=payload.target_dir,
        stop_after_step=payload.stop_after_step,
    )
    return {"run": run.model_dump(mode="json"), "action": "resume"}


@router.post("/runs/{run_id}/continue")
async def continue_run(run_id: str, payload: ContinueRequest, request: Request) -> dict[str, Any]:
    """多轮续聊：在已结束的运行上追加新要求，规划新增步骤后仍需确认再执行。"""

    run = _orchestrator(request).continue_run(run_id, payload.instruction)
    return {"run": run.model_dump(mode="json"), "action": "continue"}


@router.post("/runs/{run_id}/steps/{step_id}/retry")
async def retry_step(
    run_id: str, step_id: int, payload: RetryStepRequest, request: Request
) -> dict[str, Any]:
    """重做某一步（默认只跑这一步）。"""
    run = _orchestrator(request).retry_step(
        run_id, step_id, note=payload.note, stop_after=payload.stop_after
    )
    return {"run": run.model_dump(mode="json"), "action": "retry_step"}


@router.post("/runs/{run_id}/steps/{step_id}/revert")
async def revert_step(run_id: str, step_id: int, request: Request) -> dict[str, Any]:
    """回滚某一步的文件改动（只还原这一步碰过的路径），改错了不用手工翻 backup。"""

    run, reverted = _orchestrator(request).revert_step(run_id, step_id)
    return {
        "run": run.model_dump(mode="json"),
        "reverted": reverted,
        "action": "revert_step",
    }


@router.get("/runs/{run_id}/metrics")
async def run_metrics(run_id: str, request: Request) -> dict[str, Any]:
    """运行级指标：架构段一条 + 执行段每步一条（``Run.metrics`` 的 PhaseMetrics 契约）。

    统计看板与外部工具都以这个接口为准；运行记录不可用时返回 404，不伪造空统计。
    """

    orchestrator = _orchestrator(request)
    if not orchestrator.store.exists(run_id):
        raise NotFoundError(f"未找到该运行：{run_id}", details={"run_id": run_id})
    run = orchestrator.store.load(run_id)
    return {
        "run_id": run.id,
        "status": run.status.value,
        "metrics": [item.model_dump(mode="json") for item in run.metrics],
        "summary": run.metrics_summary(),
    }


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, since: int | None = None) -> StreamingResponse:
    orchestrator = _orchestrator(request)
    if not orchestrator.store.exists(run_id):
        raise NotFoundError(f"未找到该运行：{run_id}", details={"run_id": run_id})

    async def event_stream():
        yield ": connected\n\n"
        async for event in orchestrator.bus.subscribe(run_id, since=since):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}/tree")
async def workspace_tree(run_id: str, request: Request, limit: int = 200) -> dict[str, Any]:
    run = _orchestrator(request).store.load(run_id)
    workspace = Workspace(Path(run.workspace_dir))
    return {"root": run.workspace_dir, "files": workspace.tree(limit=limit, max_depth=6)}


@router.get("/runs/{run_id}/file")
async def read_workspace_file(run_id: str, path: str, request: Request) -> dict[str, Any]:
    run = _orchestrator(request).store.load(run_id)
    workspace = Workspace(Path(run.workspace_dir))
    return {"path": path, "content": workspace.read(path)}


@router.get("/runs/{run_id}/docs")
async def list_run_docs(run_id: str, request: Request) -> dict[str, Any]:
    store = _orchestrator(request).store
    if not store.exists(run_id):
        raise NotFoundError(f"未找到该运行：{run_id}", details={"run_id": run_id})
    directory = store.run_dir(run_id)
    docs: list[dict[str, str]] = []
    for name in ("plan.md", "report.md"):
        file = directory / name
        if file.is_file():
            docs.append({"name": name, "content": file.read_text(encoding="utf-8")})
    return {"docs": docs}


# ── 插件市场 ──


class GitPathsRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class GitCommitRequest(BaseModel):
    message: str = Field("", description="提交信息")
    paths: list[str] = Field(default_factory=list, description="留空 = 提交全部改动")
    add_all: bool = True


class GitAutoCommitRequest(BaseModel):
    push: bool = Field(False, description="提交后是否同时推送（默认只提交到本地）")


class GitBranchRequest(BaseModel):
    name: str = Field(..., min_length=1)
    create: bool = False


class GitProxyRequest(BaseModel):
    proxy_url: str = Field("", description="例如 http://127.0.0.1:10809；留空表示用系统代理")


@router.get("/git/branches")
async def git_branches(request: Request) -> dict[str, Any]:
    service = _git(request)
    return service.branches()


@router.post("/git/branches")
async def git_checkout(payload: GitBranchRequest, request: Request) -> dict[str, Any]:
    return _git(request).checkout_branch(payload.name, create=payload.create)


@router.post("/git/discard")
async def git_discard(payload: GitPathsRequest, request: Request) -> dict[str, Any]:
    """丢弃选中的改动（破坏性，界面有二次确认）。"""
    return _git(request).discard(payload.paths)


@router.get("/git/show")
async def git_show(request: Request, commit: str) -> dict[str, Any]:
    return _git(request).show_commit(commit)


@router.get("/git/proxy")
async def git_proxy_status(request: Request) -> dict[str, Any]:
    """Windows 系统代理与 git 代理的关系（git 默认不读系统代理）。"""
    return _git(request).proxy_status()


@router.post("/git/proxy")
async def git_proxy_set(payload: GitProxyRequest, request: Request) -> dict[str, Any]:
    service = _git(request)
    target = payload.proxy_url.strip() or service.system_proxy()
    if not target:
        raise AppError("没有可用的代理地址：请填写，或先打开系统代理。", code="no_proxy")
    return service.set_proxy(target)


@router.delete("/git/proxy")
async def git_proxy_clear(request: Request) -> dict[str, Any]:
    return _git(request).clear_proxy()


@router.get("/git/status")
async def git_status(request: Request) -> dict[str, Any]:
    return _git(request).status()


@router.get("/git/log")
async def git_log(request: Request, limit: int = 30) -> dict[str, Any]:
    service = _git(request)
    return {"commits": service.log(limit), "branches": service.branches()}


@router.get("/git/diff")
async def git_diff(request: Request, path: str, staged: bool = False) -> dict[str, Any]:
    return {"path": path, "staged": staged, "diff": _git(request).diff(path, staged=staged)}


@router.post("/git/stage")
async def git_stage(payload: GitPathsRequest, request: Request) -> dict[str, Any]:
    service = _git(request)
    service.stage(payload.paths)
    return service.status()


@router.post("/git/unstage")
async def git_unstage(payload: GitPathsRequest, request: Request) -> dict[str, Any]:
    service = _git(request)
    service.unstage(payload.paths)
    return service.status()


@router.post("/git/commit")
async def git_commit(payload: GitCommitRequest, request: Request) -> dict[str, Any]:
    service = _git(request)
    result = service.commit(payload.message, paths=payload.paths, add_all=payload.add_all)
    return {**result, "status": service.status()}


@router.post("/git/push")
async def git_push(request: Request) -> dict[str, Any]:
    service = _git(request)
    return {**service.push(), "status": service.status()}


@router.post("/git/pull")
async def git_pull(request: Request) -> dict[str, Any]:
    service = _git(request)
    return {**service.pull(), "status": service.status()}


@router.get("/git/auto-commit")
async def git_auto_commit_status(request: Request) -> dict[str, Any]:
    """每日开机自动提交的开关状态。"""
    return _git(request).auto_commit_status()


@router.post("/git/auto-commit")
async def git_auto_commit_enable(payload: GitAutoCommitRequest, request: Request) -> dict[str, Any]:
    return _git(request).enable_auto_commit(push=payload.push)


@router.delete("/git/auto-commit")
async def git_auto_commit_disable(request: Request) -> dict[str, Any]:
    return _git(request).disable_auto_commit()


@router.post("/git/auto-commit/run")
async def git_auto_commit_run(request: Request) -> dict[str, Any]:
    """立刻跑一次自动提交逻辑（验证开关是否按预期工作）。"""
    return _git(request).run_auto_commit_now()


@router.get("/market/capabilities")
async def market_capabilities() -> dict[str, Any]:
    """宿主支持的能力与槽位（安装前要给用户看的就是这份清单）。"""
    return supported_capabilities()


@router.get("/market/sources")
async def list_market_sources(request: Request) -> dict[str, Any]:
    store = _catalog(request)
    return {"sources": [source.describe() for source in store.list()]}


@router.post("/market/sources", status_code=201)
async def add_market_source(payload: MarketSourceRequest, request: Request) -> dict[str, Any]:
    store = _catalog(request)
    source = store.add_source(payload.manifest_url)
    return {
        "source": source.describe(),
        "sources": [item.describe() for item in store.list()],
    }


@router.delete("/market/sources/{source_record_id}")
async def remove_market_source(source_record_id: str, request: Request) -> dict[str, Any]:
    store = _catalog(request)
    store.remove_source(source_record_id)
    return {"sources": [item.describe() for item in store.list()]}


@router.get("/market/items")
async def list_market_items(
    request: Request,
    q: str = "",
    category: str = "",
    capability: str = "",
    source_id: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    catalog = _catalog(request)
    installed = {plugin.manifest.id for plugin in _plugins(request).list()}
    items = catalog.items(
        source_record_id=source_id,
        query=q,
        category=category,
        capability=capability,
        limit=limit,
    )
    return {
        "items": [item.describe(installed=item.id in installed) for item in items],
        "total": len(items),
    }


@router.get("/plugins")
async def list_plugins(request: Request) -> dict[str, Any]:
    return plugins_payload(_plugins(request))


@router.post("/plugins", status_code=201)
async def install_plugin(payload: PluginInstallRequest, request: Request) -> dict[str, Any]:
    """安装目录条目对应的**声明式插件**（不下载、不执行任何第三方代码）。"""
    catalog = _catalog(request)
    store = _plugins(request)
    item = next(
        (
            candidate
            for candidate in catalog.items(source_record_id=payload.source_record_id, limit=200)
            if candidate.id == payload.item_id
        ),
        None,
    )
    if item is None:
        raise NotFoundError(
            f"该来源里没有这个插件：{payload.item_id}",
            details={"item_id": payload.item_id, "source_record_id": payload.source_record_id},
        )
    plugin = store.install(item.to_manifest())
    return {"plugin": plugin.describe(), **plugins_payload(store)}


@router.post("/plugins/{plugin_id}/enable")
async def enable_plugin(plugin_id: str, request: Request) -> dict[str, Any]:
    store = _plugins(request)
    store.set_enabled(plugin_id, True)
    return plugins_payload(store)


@router.post("/plugins/{plugin_id}/disable")
async def disable_plugin(plugin_id: str, request: Request) -> dict[str, Any]:
    store = _plugins(request)
    store.set_enabled(plugin_id, False)
    return plugins_payload(store)


@router.delete("/plugins/{plugin_id}")
async def uninstall_plugin(plugin_id: str, request: Request) -> dict[str, Any]:
    store = _plugins(request)
    store.uninstall(plugin_id)
    return plugins_payload(store)


# ---------------------------------------------------------------------------
# 导航信息架构（第 3 步）：左侧栏一级入口只有「普通对话」与「项目」
#
# 上位契约：docs/project-navigation-contract.md。架构 / 计划 / 执行 / 步骤 / 验证 / 日志 /
# 设置只作为「项目」下的二级模块出现；普通对话上下文既不渲染也不返回这类数据。
# ---------------------------------------------------------------------------

#: 一级导航文案：这里就是左侧栏一级区域的全部内容。
PRIMARY_NAVIGATION_LABELS: dict[str, str] = {
    NavEntry.CHAT.value: "普通对话",
    NavEntry.PROJECTS.value: "项目",
}

#: 一级导航落点。
PRIMARY_NAVIGATION_ROUTES: dict[str, str] = {
    NavEntry.CHAT.value: "#/chat",
    NavEntry.PROJECTS.value: PROJECT_LIST_ROUTE,
}

#: 二级模块文案：只挂在「项目」下面。
PROJECT_MODULE_LABELS: dict[str, str] = {
    ProjectModule.OVERVIEW.value: "概览",
    ProjectModule.ARCHITECTURE.value: "架构",
    ProjectModule.PLAN.value: "计划",
    ProjectModule.EXECUTION.value: "执行",
    ProjectModule.VERIFICATION.value: "验证",
    ProjectModule.LOGS.value: "日志",
    ProjectModule.SETTINGS.value: "设置",
}


def primary_navigation_items() -> list[dict[str, str]]:
    """左侧栏一级入口，顺序固定为「普通对话」→「项目」。"""
    return [
        {
            "id": entry.value,
            "label": PRIMARY_NAVIGATION_LABELS[entry.value],
            "route": PRIMARY_NAVIGATION_ROUTES[entry.value],
            "context_type": "chat" if entry is NavEntry.CHAT else "project",
        }
        for entry in NavEntry
    ]


def project_module_items() -> list[dict[str, Any]]:
    """项目内部二级模块；没有选中项目时不允许渲染。"""
    return [
        {
            "id": module.value,
            "label": PROJECT_MODULE_LABELS.get(module.value, module.value),
            "visible_without_project": False,
        }
        for module in PROJECT_MODULES
    ]


def _module_not_found(module_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "project_module_not_found",
                "message": f"项目里没有这个模块：{module_id}",
                "details": {
                    "module_id": module_id,
                    "allowed": [module.value for module in PROJECT_MODULES],
                },
            }
        },
    )


def _empty_project_state(module_id: str) -> dict[str, Any]:
    """没有选中项目时的引导态：只给选择/创建入口，绝不返回全局运行数据。"""
    return {
        "requires_project": True,
        "module": module_id,
        "selected_project_id": None,
        "is_empty_state": True,
        "empty_state": {
            "title": "先选择一个项目",
            "message": "架构、计划、执行、步骤、验证、日志与设置都属于项目内部，请先选择或创建项目。",
            "actions": [
                {"id": "projects.list", "label": "选择已有项目", "route": PROJECT_LIST_ROUTE},
                {
                    "id": "projects.create",
                    "label": "新建项目",
                    "route": f"{PROJECT_LIST_ROUTE}?new=1",
                },
            ],
        },
    }


@router.get("/navigation/sidebar")
async def navigation_sidebar() -> dict[str, Any]:
    """左侧栏契约：一级入口只有普通对话与项目，工程概念全部下沉到项目内。"""
    return {
        "primary": primary_navigation_items(),
        "project_modules": project_module_items(),
        "project_only_actions": sorted(PROJECT_ONLY_ACTIONS),
        "default_route": PROJECT_LIST_ROUTE,
        "default_context_type": "project",
    }


@router.get("/navigation/chat-workspace")
async def navigation_chat_workspace() -> dict[str, Any]:
    """普通对话工作区：独立于项目，不携带任何项目执行控制。"""
    return {
        "route": "#/chat",
        "context_type": "chat",
        "shows_project_controls": False,
        "shows_execution_controls": False,
    }


@router.get("/navigation/project-modules/{module_id}")
async def project_module_entry(module_id: str) -> Any:
    """未选中项目时点二级模块的落点：项目选择/创建引导，而不是全局运行数据。"""
    try:
        module = ProjectModule(module_id)
    except ValueError:
        return _module_not_found(module_id)
    return _empty_project_state(module.value)


@router.get("/projects/{project_id}/modules/{module_id}")
async def project_module(project_id: str, module_id: str) -> Any:
    """项目内部二级模块：上下文由 project_id 决定，刷新后据此恢复项目与模块。"""
    normalized = (project_id or "").strip()
    if not normalized:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "missing_project_context",
                    "message": "项目模块必须携带 project_id",
                    "details": {"module_id": module_id},
                }
            },
        )
    try:
        module = ProjectModule(module_id)
    except ValueError:
        return _module_not_found(module_id)
    context = project_module_context(module, normalized)
    return {
        "project_id": context.project_id,
        "module": context.module,
        "context_id": context.context_id,
        "route": context.route,
        "is_empty_state": False,
    }
