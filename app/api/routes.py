"""API 路由。

约定：成功直接返回资源对象；失败返回 ``{"error": {code, message, details}}``。
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.catalog import CatalogStore, supported_capabilities
from app.core.config import OVERRIDABLE_FIELDS, Settings, get_settings, settings_store
from app.core.errors import AppError, NotFoundError
from app.core.plugins import PluginStore
from app.core.providers import KIND_PRESETS
from app.core.relay import RelayClient
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
    request_timeout_seconds: float | None = None


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
    from app.core.config import ORCHESTRATOR_ROOT

    settings = _settings(request)
    repo = Path(settings.git_dir).expanduser() if settings.git_dir else ORCHESTRATOR_ROOT
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
        "request_timeout_seconds": settings.request_timeout_seconds,
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


@router.post("/runs/{run_id}/steps/{step_id}/retry")
async def retry_step(
    run_id: str, step_id: int, payload: RetryStepRequest, request: Request
) -> dict[str, Any]:
    """重做某一步（默认只跑这一步）。"""
    run = _orchestrator(request).retry_step(
        run_id, step_id, note=payload.note, stop_after=payload.stop_after
    )
    return {"run": run.model_dump(mode="json"), "action": "retry_step"}


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
