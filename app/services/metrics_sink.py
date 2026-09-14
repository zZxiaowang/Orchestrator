"""指标采集接线层（下一阶段 · 第 3 步「贯通指标采集与统计接口」）。

职责：把「模型调用结果」翻译成 ``run.metrics`` 里的标准条目，并给出运行级汇总，
供 ``GET /api/runs/{run_id}/metrics`` 与 ``metrics_updated`` 事件直接消费。
采集层永不抛异常：拿不到数据就退化为空值，绝不拖垮一次运行。

``run.metrics`` 形状（与 app/schemas/run.py 的 PhaseMetrics 契约一致）::

    {
      "architect": {
        "calls": 1,
        "retries": 0,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "duration_ms": 812,
        "protocol": "chat_completions",
        "status": "done",
        "route": {"alias": "默认", "model": "gpt-4o"}
      },
      "steps": [
        {
          "step_id": "1",
          "calls": 2,
          "retries": 1,
          "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
          "duration_ms": 2400,
          "protocol": "responses",
          "status": "done",
          "route": {"alias": "个人 Key", "model": "gpt-5"},
          "context_chars": 15432,
          "fetched": 2
        },
        {
          "step_id": "2",
          "calls": 0,
          "retries": 0,
          "usage": {"prompt_tokens": null, "completion_tokens": null, "total_tokens": null},
          "duration_ms": 3,
          "status": "blocked",
          "context_chars": 900,
          "fetched": 0
        }
      ]
    }

调用点接线（每个调用点只需一行）::

    from app.services.metrics_sink import record_architect, record_step

    # orchestrator：架构段返回后
    record_architect(run, result, route=getattr(runner, "last_route", None))

    # orchestrator：每个 _execute_step 收尾（含 blocked / failed 分支）
    record_step(
        run,
        step.id,
        result,
        status=step.status,
        context_chars=len(context or ""),
        fetched=len(fetched_files or []),
    )

约定：

* ``calls`` = 该条目累计发出的 HTTP 次数（含参数降级重发与 5xx / 流式重试），
  ``retries`` = ``calls - 1``；
* token 缺失一律保持 ``None``（前端渲染成「未知」），绝不用 0 冒充已知值；
  整条调用拿不到 usage 的次数单独计入 ``totals.unknown_usage_calls``；
* 事件载荷只带 ``run_id`` / ``metrics`` / 可选 ``seq``，**不带任何时间戳**，
  断线续传完全依赖现有单调序号。
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from app.core.relay import normalize_usage

#: 事件类型：指标写入后向前端广播
METRICS_EVENT = "metrics_updated"

ARCHITECT_KEY = "architect"
STEPS_KEY = "steps"

#: usage 的统一口径（两种协议归一化后的字段名）
USAGE_FIELDS: tuple[str, ...] = ("prompt_tokens", "completion_tokens", "total_tokens")

_ROUTE_FIELDS: tuple[str, ...] = ("alias", "model", "base_url", "protocol", "phase")
_BLOCKED_STATUS = "blocked"
_FAILED_STATUS = frozenset({"failed", "error"})


# ── 基础读写 ─────────────────────────────────────────────────────────────


def _field(source: Any, name: str, default: Any = None) -> Any:
    """同时支持 dict 与对象两种载体的字段读取。"""
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _write_field(target: Any, name: str, value: Any) -> None:
    if isinstance(target, dict):
        target[name] = value
        return
    with suppress(Exception):  # pragma: no cover - 只读载体时静默放弃
        setattr(target, name, value)


def read_metrics(run: Any) -> dict[str, Any]:
    """只读快照；读不到就返回空 dict。"""
    if run is None:
        return {}
    raw = _field(run, "metrics", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    dumped = getattr(raw, "model_dump", None)
    if callable(dumped):
        try:
            data = dumped()
        except Exception:  # pragma: no cover
            return {}
        if isinstance(data, Mapping):
            return dict(data)
    return {}


def ensure_metrics(run: Any) -> dict[str, Any]:
    """拿到（必要时创建）可写的 run.metrics 容器。

    ``run is None`` 时返回一个临时容器，保证调用点不会因为空运行而抛异常。
    """
    if run is None:
        return {ARCHITECT_KEY: None, STEPS_KEY: []}
    raw = _field(run, "metrics", None)
    if isinstance(raw, dict):
        target = raw
    else:
        target = {}
        _write_field(run, "metrics", target)
    if not isinstance(target.get(STEPS_KEY), list):
        target[STEPS_KEY] = []
    target.setdefault(ARCHITECT_KEY, None)
    return target


# ── 单次调用的采集 ───────────────────────────────────────────────────────


def usage_snapshot(value: Any) -> dict[str, int | None]:
    """复用 relay 的归一化入口，保证 chat_completions / responses 口径一致。"""
    return normalize_usage(value)


def route_summary(route: Any) -> dict[str, Any] | None:
    """裁剪出可持久化的路由信息（不含 Key、不含完整请求参数）。"""
    if route is None:
        return None
    data: dict[str, Any] = {}
    for name in _ROUTE_FIELDS:
        value = _field(route, name, None)
        if value not in (None, ""):
            data[name] = value
    return data or None


def call_metrics(
    result: Any = None,
    *,
    route: Any = None,
    status: str | None = None,
    attempts: int | None = None,
    duration_ms: int | None = None,
    context_chars: int | None = None,
    fetched: int | None = None,
    step_id: Any = None,
) -> dict[str, Any]:
    """把一次（或一轮含重试的）模型调用翻成标准条目。

    ``result`` 可以是 ``RelayResult``、普通 dict，也可以是 ``None``
    （blocked / 调用前失败等没有返回体的场景）。
    ``attempts`` / ``duration_ms`` 显式传入时优先于 ``result`` 上的值，
    便于调用方在 except 分支里回填真实尝试次数。
    """
    total_attempts = attempts
    if total_attempts is None:
        total_attempts = _as_int(_field(result, "attempts", 0), 0)
        if result is not None and total_attempts <= 0:
            total_attempts = 1
    total_attempts = max(_as_int(total_attempts, 0), 0)

    measured = duration_ms
    if measured is None:
        measured = _as_int(_field(result, "duration_ms", 0), 0)
    measured = max(_as_int(measured, 0), 0)

    entry: dict[str, Any] = {
        "calls": total_attempts,
        "retries": max(total_attempts - 1, 0),
        "usage": usage_snapshot(_field(result, "usage", None)),
        "duration_ms": measured,
    }

    protocol = _field(result, "protocol", None)
    if protocol:
        entry["protocol"] = protocol

    route_info = route_summary(route)
    if route_info:
        entry["route"] = route_info

    if status:
        entry["status"] = status
    if step_id is not None:
        entry["step_id"] = str(step_id)
    if context_chars is not None:
        entry["context_chars"] = max(_as_int(context_chars, 0), 0)
    if fetched is not None:
        entry["fetched"] = max(_as_int(fetched, 0), 0)
    return entry


def _sum_usage(left: Any, right: Any) -> dict[str, int | None]:
    """None-safe 求和：两边都未知才保持 None，任何一边已知就用已知值相加。"""

    def part(value: Any) -> dict[str, int | None]:
        data = value if isinstance(value, Mapping) else {}
        out: dict[str, int | None] = {}
        for name in USAGE_FIELDS:
            raw = data.get(name)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                out[name] = None
            else:
                out[name] = int(raw)
        return out

    a, b = part(left), part(right)
    merged: dict[str, int | None] = {}
    for name in USAGE_FIELDS:
        if a[name] is None and b[name] is None:
            merged[name] = None
        else:
            merged[name] = (a[name] or 0) + (b[name] or 0)
    return merged


def merge_call_metrics(existing: Any, incoming: Mapping[str, Any]) -> dict[str, Any]:
    """把同一目标（架构段 / 同一步骤）的多次采集累加到一起。"""
    base = dict(existing) if isinstance(existing, Mapping) else {}
    merged: dict[str, Any] = dict(base)
    calls = _as_int(base.get("calls"), 0) + _as_int(incoming.get("calls"), 0)
    merged["calls"] = calls
    # 重试 = 累计尝试次数 - 1：合并多次调用时，每次调用的第一次尝试都不算重试，
    # 否则"两次调用各 1 次尝试"会被错误地累加成 1 次重试（其实一次都没有）。
    merged["retries"] = max(calls - 1, 0)
    merged["duration_ms"] = _as_int(base.get("duration_ms"), 0) + _as_int(
        incoming.get("duration_ms"), 0
    )
    merged["usage"] = _sum_usage(base.get("usage"), incoming.get("usage"))

    for key in ("route", "protocol", "step_id", "status"):
        value = incoming.get(key) or base.get(key)
        if value not in (None, ""):
            merged[key] = value

    for key in ("context_chars", "fetched"):
        if key in incoming:
            merged[key] = _as_int(incoming.get(key), 0)
        elif key in base:
            merged[key] = _as_int(base.get(key), 0)
    return merged


# ── 两个接线入口 ─────────────────────────────────────────────────────────


def record_architect(
    run: Any,
    result: Any = None,
    *,
    route: Any = None,
    status: str | None = "done",
    **kwargs: Any,
) -> dict[str, Any]:
    """写入（或累加）架构段指标。"""
    metrics = ensure_metrics(run)
    entry = call_metrics(result, route=route, status=status, **kwargs)
    metrics[ARCHITECT_KEY] = merge_call_metrics(metrics.get(ARCHITECT_KEY), entry)
    return metrics[ARCHITECT_KEY]


def record_step(
    run: Any,
    step_id: Any,
    result: Any = None,
    *,
    route: Any = None,
    status: str | None = None,
    context_chars: int | None = None,
    fetched: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """写入（或按 step_id 合并）单步执行指标。

    ``status`` 传 ``blocked`` / ``failed`` 时同样落条目：
    此时 ``calls`` 通常为 0，但耗时与上下文占用仍被记录。
    """
    metrics = ensure_metrics(run)
    entry = call_metrics(
        result,
        route=route,
        status=status,
        step_id=step_id,
        context_chars=context_chars,
        fetched=fetched,
        **kwargs,
    )
    steps = metrics[STEPS_KEY]
    key = entry["step_id"]
    for index, item in enumerate(steps):
        if isinstance(item, Mapping) and str(item.get("step_id")) == key:
            steps[index] = merge_call_metrics(item, entry)
            return steps[index]
    steps.append(entry)
    return entry


# ── 运行级汇总 / 接口与事件载荷 ──────────────────────────────────────────


def summarize(run: Any) -> dict[str, Any]:
    """运行级汇总：架构段条目 + 步骤明细 + totals。"""
    metrics = read_metrics(run)

    architect_raw = metrics.get(ARCHITECT_KEY)
    architect = dict(architect_raw) if isinstance(architect_raw, Mapping) else None

    steps_raw = metrics.get(STEPS_KEY)
    steps: list[dict[str, Any]] = []
    if isinstance(steps_raw, list):
        for item in steps_raw:
            if isinstance(item, Mapping):
                steps.append(dict(item))

    entries: list[dict[str, Any]] = ([architect] if architect else []) + steps

    calls = sum(_as_int(entry.get("calls"), 0) for entry in entries)
    retries = sum(_as_int(entry.get("retries"), 0) for entry in entries)
    duration_ms = sum(_as_int(entry.get("duration_ms"), 0) for entry in entries)

    usage: dict[str, int | None] = dict.fromkeys(USAGE_FIELDS)
    unknown_calls = 0
    for entry in entries:
        usage = _sum_usage(usage, entry.get("usage"))
        entry_calls = _as_int(entry.get("calls"), 0)
        if entry_calls <= 0:
            continue
        entry_usage = entry.get("usage")
        total = entry_usage.get("total_tokens") if isinstance(entry_usage, Mapping) else None
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            unknown_calls += entry_calls

    contexts = [
        _as_int(step.get("context_chars"), 0)
        for step in steps
        if step.get("context_chars") is not None
    ]
    fetched_files = sum(_as_int(step.get("fetched"), 0) for step in steps)

    return {
        "architect": architect,
        "steps": steps,
        "totals": {
            "calls": calls,
            "retries": retries,
            "duration_ms": duration_ms,
            "usage": usage,
            "unknown_usage_calls": unknown_calls,
            "steps": len(steps),
            "blocked_steps": sum(1 for s in steps if s.get("status") == _BLOCKED_STATUS),
            "failed_steps": sum(1 for s in steps if s.get("status") in _FAILED_STATUS),
            "context_chars_total": sum(contexts),
            "context_chars_peak": max(contexts) if contexts else 0,
            "fetched_files": fetched_files,
        },
    }


def metrics_response(run: Any, run_id: Any = None) -> dict[str, Any] | None:
    """``GET /api/runs/{run_id}/metrics`` 的响应体。

    返回 ``None`` 表示该运行不存在 —— 路由层据此返回 404，
    不要伪造一份空统计冒充成功。
    """
    if run is None:
        return None
    resolved = _field(run, "run_id", None)
    if resolved in (None, ""):
        resolved = run_id
    payload: dict[str, Any] = {
        "run_id": "" if resolved in (None, "") else str(resolved),
        "status": _field(run, "status", None),
    }
    payload.update(summarize(run))
    return payload


def event_payload(run: Any, *, run_id: Any = None, seq: int | None = None) -> dict[str, Any]:
    """``metrics_updated`` 事件载荷。

    刻意不带任何时间戳字段：断线续传只认 events 服务分配的单调序号，
    避免重蹈 Windows 时钟精度丢事件的覆辙。
    """
    snapshot = summarize(run)
    resolved = _field(run, "run_id", None) if run is not None else None
    if resolved in (None, ""):
        resolved = run_id
    payload: dict[str, Any] = {
        "run_id": "" if resolved in (None, "") else str(resolved),
        "metrics": {
            "architect": snapshot["architect"],
            "steps": snapshot["steps"],
            "totals": snapshot["totals"],
        },
    }
    if seq is not None:
        payload["seq"] = int(seq)
    return payload
