"""项目内模块的数据装配：概览 / 架构 / 计划 / 执行 / 步骤 / 验证 / 日志 / 设置。

界面上的二级导航在这里拿到**真实数据**，而不是只有路由元信息——上一轮只做了导航壳子，
点进去什么都没有。这里把"项目 → 它的运行 → 运行的纲领 / 步骤 / 验证 / 日志"装配成
各模块直接可渲染的结构，空项目同样给出空状态（不是报错，也不是假数据）。

约定：

* 每个模块都带 ``project`` / ``module`` / ``counts`` / ``is_empty_state`` 四个公共字段；
* 长文本（架构原文、日志正文）一律截断，避免一次把几十万字符塞给界面；
* 模块数据只来自**本项目自己的运行**（按 ``project_id`` 过滤），不跨项目聚合。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.schemas.project import Project
from app.schemas.run import PHASE_ARCHITECT, Run, RunStep, StepStatus
from app.services.verify import summarize

#: 二级模块顺序（与 docs/project-navigation-contract.md 一致）
MODULES: tuple[str, ...] = (
    "overview",
    "architecture",
    "plan",
    "execution",
    "verification",
    "logs",
    "settings",
)

MODULE_LABELS: dict[str, str] = {
    "overview": "概览",
    "architecture": "架构",
    "plan": "计划",
    "execution": "执行",
    "verification": "验证",
    "logs": "日志",
    "settings": "设置",
}

#: 旧模块名 → 契约模块名（旧链接与旧前端导航照常能用）
MODULE_ALIASES: dict[str, str] = {
    "steps": "execution",
    "verify": "verification",
    "events": "logs",
    "context": "settings",
    "project": "overview",
}

#: 单个模块里长文本的截断上限
RAW_LIMIT = 4000
MESSAGE_LIMIT = 1200
MAX_LOGS = 40
MAX_RUNS = 20


def _clip(text: str, limit: int) -> str:
    value = text or ""
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n…（已截断，共 {len(value)} 字符）"


def project_block(project: Project) -> dict[str, Any]:
    workspace = project.workspace
    return {
        "project_id": project.project_id,
        "context_id": project.context_id,
        "name": project.name,
        "description": project.description or "",
        "status": project.status.value,
        "workspace": {
            "workspace_id": workspace.workspace_id,
            "root_path": workspace.root_path or "",
            "label": workspace.label or "",
        }
        if workspace
        else None,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "last_activity_at": project.last_activity_at.isoformat(),
    }


def _step_progress(run: Run) -> dict[str, int]:
    steps = run.steps or []
    return {
        "total": len(steps),
        "done": sum(1 for step in steps if step.status == StepStatus.DONE),
        "running": sum(1 for step in steps if step.status == StepStatus.RUNNING),
        "blocked": sum(1 for step in steps if step.status == StepStatus.BLOCKED),
        "failed": sum(1 for step in steps if step.status == StepStatus.FAILED),
        "pending": sum(
            1 for step in steps if step.status in (StepStatus.PENDING, StepStatus.SKIPPED)
        ),
    }


def run_summary(run: Run) -> dict[str, Any]:
    """项目内运行列表用的摘要（比 Run.summarize 多一点展示字段）。"""

    progress = _step_progress(run)
    return {
        **run.summarize(),
        "kind": run.kind,
        "project_id": run.project_id,
        "steps": progress,
        "error": (run.error or {}).get("message", "") if run.error else "",
    }


def step_block(step: RunStep) -> dict[str, Any]:
    verdict = summarize(step.verification) if step.verification else None
    return {
        "id": step.id,
        "title": step.title,
        "goal": step.goal,
        "status": step.status.value,
        "summary": step.summary,
        "handoff": step.handoff,
        "error": step.error,
        "notes": list(step.notes),
        "deliverables": list(step.deliverables),
        "acceptance": list(step.acceptance),
        "checks": [item.model_dump(mode="json") for item in step.checks],
        "verification": verdict,
        "verification_items": [item.model_dump(mode="json") for item in step.verification],
        "files": [item.model_dump(mode="json") for item in step.files],
        "commands": list(step.commands),
        "command_results": [item.model_dump(mode="json") for item in step.command_results],
        "context_chars": step.context_chars,
        "fetched_files": list(step.fetched_files),
        "started_at": step.started_at.isoformat() if step.started_at else "",
        "finished_at": step.finished_at.isoformat() if step.finished_at else "",
    }


def _counts(runs: Sequence[Run]) -> dict[str, int]:
    steps = [step for run in runs for step in run.steps]
    return {
        "runs": len(runs),
        "runs_done": sum(1 for run in runs if run.status.value == "done"),
        "runs_blocked": sum(1 for run in runs if run.status.value == "blocked"),
        "steps_total": len(steps),
        "steps_done": sum(1 for step in steps if step.status == StepStatus.DONE),
        "files_changed": sum(len(step.files) for step in steps),
    }


def build_module_payload(
    module: str,
    *,
    project: Project,
    runs: Sequence[Run],
    run: Run | None = None,
    models: Mapping[str, Any] | None = None,
    route_path: str = "",
) -> dict[str, Any]:
    """装配一个项目模块的载荷。``runs`` 必须是**本项目**的运行（已按 project_id 过滤）。"""

    ordered = sorted(runs, key=lambda item: item.updated_at, reverse=True)
    latest = run or (ordered[0] if ordered else None)
    payload: dict[str, Any] = {
        "project": project_block(project),
        "module": module,
        "label": MODULE_LABELS.get(module, module),
        "counts": _counts(ordered),
        "is_empty_state": latest is None,
        "runs": [run_summary(item) for item in ordered[:MAX_RUNS]],
        "current_run": run_summary(latest) if latest else None,
        #: 前端路由（``#/projects/<id>/<module>``）；模型路由另有 ``models`` 字段
        "route": route_path,
        "models": {
            "architect": dict((models or {}).get("architect") or {}),
            "editor": dict((models or {}).get("editor") or {}),
        },
    }

    if module == "overview":
        payload["next_actions"] = _next_actions(latest)
    elif module == "architecture":
        payload["architecture"] = _architecture(latest)
    elif module == "plan":
        payload["plan"] = _plan(latest)
    elif module == "execution":
        payload["execution"] = _execution(latest)
    elif module == "verification":
        payload["verification"] = _verification(latest)
    elif module == "logs":
        payload["logs"] = _logs(latest)
    elif module == "settings":
        payload["settings"] = _settings(project, models)
    return payload


def _next_actions(run: Run | None) -> list[dict[str, str]]:
    if run is None:
        return [{"id": "new-run", "label": "新建任务（生成纲领）"}]
    actions: list[dict[str, str]] = []
    if run.status.value == "awaiting_approval":
        actions.append({"id": "approve", "label": "确认并开始执行"})
    if run.status.value in ("blocked", "failed"):
        actions.append({"id": "resume", "label": "补充信息并继续"})
    if run.status.value in ("done", "blocked", "failed", "paused"):
        actions.append({"id": "continue", "label": "继续说下一步"})
    if run.status.value in ("planning", "executing"):
        actions.append({"id": "cancel", "label": "停止这次运行"})
    actions.append({"id": "new-run", "label": "新建任务"})
    return actions


def _architecture(run: Run | None) -> dict[str, Any]:
    if run is None or run.plan is None:
        return {"has_plan": False, "raw": "", "goal": "", "summary": "", "components": []}
    plan = run.plan
    architect = next(
        (item for item in run.metrics if item.phase == PHASE_ARCHITECT and item.step_id is None),
        None,
    )
    return {
        "has_plan": True,
        "run_id": run.id,
        "goal": plan.goal,
        "summary": plan.summary,
        "principles": list(plan.principles),
        "components": [
            {
                "name": item.name,
                "responsibility": item.responsibility,
                "interfaces": list(item.interfaces),
            }
            for item in plan.components
        ],
        "risks": list(plan.risks),
        "open_questions": list(plan.open_questions),
        "plan_revision": run.plan_revision,
        "raw": _clip(run.plan_raw, RAW_LIMIT),
        "metrics": {
            "model": (run.route.get("architect") or {}).get("model", ""),
            "duration_ms": architect.duration_ms if architect else 0,
            "total_tokens": architect.total_tokens if architect else None,
            "context_chars": architect.context_chars if architect else 0,
        },
    }


def _plan(run: Run | None) -> dict[str, Any]:
    if run is None or run.plan is None:
        return {"has_plan": False, "steps": []}
    plan = run.plan
    return {
        "has_plan": True,
        "run_id": run.id,
        "goal": plan.goal,
        "summary": plan.summary,
        "plan_revision": run.plan_revision,
        "steps": [
            {
                "id": step.id,
                "title": step.title,
                "goal": step.goal,
                "deliverables": list(step.deliverables),
                "acceptance": list(step.acceptance),
                "checks": [item.model_dump(mode="json") for item in step.checks],
                "depends_on": list(step.depends_on),
                "status": _plan_step_status(run, step.id),
            }
            for step in plan.steps
        ],
    }


def _plan_step_status(run: Run, step_id: int) -> str:
    for step in run.steps:
        if step.id == step_id:
            return step.status.value
    return "pending"


def _execution(run: Run | None) -> dict[str, Any]:
    if run is None:
        return {"has_run": False, "steps": [], "actions": []}
    return {
        "has_run": True,
        "run_id": run.id,
        "status": run.status.value,
        "title": run.title,
        "task": run.task,
        "error": (run.error or {}).get("message", "") if run.error else "",
        "workspace_dir": run.workspace_dir,
        "metrics": run.metrics_summary(),
        "actions": _next_actions(run),
        "steps": [step_block(step) for step in run.steps],
    }


def _verification(run: Run | None) -> dict[str, Any]:
    if run is None:
        return {"has_run": False, "steps": [], "totals": summarize([])}
    items = [item for step in run.steps for item in step.verification]
    unverified = [
        step.id for step in run.steps if step.status == StepStatus.DONE and not step.verification
    ]
    return {
        "has_run": True,
        "run_id": run.id,
        "totals": summarize(items),
        "unverified_steps": unverified,
        "steps": [
            {
                "id": step.id,
                "title": step.title,
                "status": step.status.value,
                "summary": summarize(step.verification) if step.verification else None,
                "items": [item.model_dump(mode="json") for item in step.verification],
            }
            for step in run.steps
        ],
    }


def _logs(run: Run | None) -> dict[str, Any]:
    if run is None:
        return {"has_run": False, "messages": [], "metrics": {}, "commands": []}
    messages = [
        {
            "role": item.role,
            "phase": item.phase,
            "model": item.model,
            "created_at": item.created_at.isoformat(),
            "content": _clip(item.content, MESSAGE_LIMIT),
            "truncated": len(item.content) > MESSAGE_LIMIT,
        }
        for item in run.messages[-MAX_LOGS:]
    ]
    commands = [
        {
            "step_id": step.id,
            "cmd": item.cmd,
            "ok": item.ok,
            "skipped": item.skipped,
            "exit_code": item.exit_code,
            "duration_ms": item.duration_ms,
        }
        for step in run.steps
        for item in step.command_results
    ]
    return {
        "has_run": True,
        "run_id": run.id,
        "status": run.status.value,
        "messages": messages,
        "commands": commands,
        "metrics": run.metrics_summary(),
    }


def _settings(project: Project, models: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "read_only": ["project_id", "created_at"],
        "editable": ["name", "description", "workspace.root_path"],
        "project": project_block(project),
        "models": {
            "architect": dict((models or {}).get("architect") or {}),
            "editor": dict((models or {}).get("editor") or {}),
        },
    }
