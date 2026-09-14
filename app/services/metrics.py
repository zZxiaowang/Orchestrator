"""运行指标采集与聚合（PhaseMetrics 契约的采集侧实现）。

职责边界（见 docs/state-and-event-contracts.md）：

* 只负责把「一次模型调用 / 一次步骤执行」的原始信息变成指标记录，
  以及把记录汇总成运行级统计；
* 不负责持久化（由 app/services/storage.py 写入单文件 JSON）；
* 不负责推送（由 app/services/events.py 用单调序号广播）。
  计时一律使用 ``time.perf_counter``（单调），时间戳绝不参与事件排序。

命名约定：

* phase：架构段 ``architect``、执行段 ``executor``，与分段路由保持一致；
* attempt：一次真实 HTTP 请求，参数降级重发与 5xx/流式重试都累加 attempts；
* usage：两种协议归一化成 prompt/completion/total 三元组；网关不返回 usage 时
  保持 ``None``（前端渲染成「未知」），绝不用 0 冒充已知值。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# ── 状态常量 ──

STATUS_OK = "ok"
STATUS_RETRIED = "retried"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

PHASE_ARCHITECT = "architect"
PHASE_EXECUTOR = "executor"

USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")

_PROMPT_KEYS = ("prompt_tokens", "input_tokens")
_COMPLETION_KEYS = ("completion_tokens", "output_tokens")
_TOTAL_KEYS = ("total_tokens",)
_NESTED_KEYS = ("response", "data", "result")


def safe_text(value: Any, limit: int = 400) -> str:
    """把任意错误体（含 httpx 的 bytes）安全转成单行文本。

    ``resp.aread()`` 返回 bytes，直接拼进错误信息会抛
    "expected str instance, bytes found"，从而掩盖真实的网关错误。
    """

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def _first_int(source: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in source:
            value = _coerce_int(source.get(key))
            if value is not None:
                return value
    return None


def normalize_usage(raw: Any) -> dict[str, int | None]:
    """把 usage 归一化成 ``{prompt_tokens, completion_tokens, total_tokens}``。

    * ``chat_completions``：``prompt_tokens`` / ``completion_tokens``；
    * ``responses``：``input_tokens`` / ``output_tokens``；
    * 只有 total 时照原样保留；prompt 与 completion 都已知而 total 缺失时补齐；
    * 任何缺失项保持 ``None``，不参与求和。
    """

    normalized: dict[str, int | None] = dict.fromkeys(USAGE_KEYS)
    if not isinstance(raw, Mapping):
        return normalized
    normalized["prompt_tokens"] = _first_int(raw, _PROMPT_KEYS)
    normalized["completion_tokens"] = _first_int(raw, _COMPLETION_KEYS)
    normalized["total_tokens"] = _first_int(raw, _TOTAL_KEYS)
    if normalized["total_tokens"] is None:
        prompt = normalized["prompt_tokens"]
        completion = normalized["completion_tokens"]
        if prompt is not None and completion is not None:
            normalized["total_tokens"] = prompt + completion
    return normalized


def usage_known(usage: Mapping[str, Any] | None) -> bool:
    """usage 中至少有一项已知值时，才算「拿到了用量」。"""

    if not usage:
        return False
    return any(usage.get(key) is not None for key in USAGE_KEYS)


def extract_usage(payload: Any, *, depth: int = 3) -> dict[str, int | None]:
    """从响应体里定位 usage（顶层 / response.usage / data.usage …）并归一化。"""

    empty: dict[str, int | None] = dict.fromkeys(USAGE_KEYS)
    if depth <= 0 or not isinstance(payload, Mapping):
        return empty
    direct = payload.get("usage")
    if isinstance(direct, Mapping):
        normalized = normalize_usage(direct)
        if usage_known(normalized):
            return normalized
    for key in _NESTED_KEYS:
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            found = extract_usage(nested, depth=depth - 1)
            if usage_known(found):
                return found
    return empty


@dataclass
class Stopwatch:
    """单调计时器（毫秒），不受系统时钟调整与 Windows 时钟精度影响。"""

    started_at: float = field(default_factory=time.perf_counter)
    stopped_at: float | None = None

    def stop(self) -> int:
        if self.stopped_at is None:
            self.stopped_at = time.perf_counter()
        return self.elapsed_ms()

    def elapsed_ms(self) -> int:
        end = self.stopped_at if self.stopped_at is not None else time.perf_counter()
        return max(0, int(round((end - self.started_at) * 1000)))


def classify_status(attempts: int, status: str = STATUS_OK) -> str:
    """成功但发生过重试时标成 retried，便于区分「一次成功」与「降级后成功」。"""

    if status in (STATUS_FAILED, STATUS_BLOCKED, STATUS_RETRIED):
        return status
    return STATUS_RETRIED if int(attempts or 0) > 1 else STATUS_OK


def build_record(
    phase: str,
    *,
    status: str = STATUS_OK,
    attempts: int = 0,
    duration_ms: int = 0,
    usage: Mapping[str, Any] | None = None,
    context_chars: int = 0,
    model: str = "",
    protocol: str = "",
    error: str = "",
    step_index: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一条步骤级指标记录（字段名与 PhaseMetrics 契约保持同名）。

    调用方在模型调用前后各取一次 ``Stopwatch``，把 ``attempts`` 从
    runner/RelayResult 带过来即可；无需在本模块内做任何 IO。
    """

    normalized = normalize_usage(usage) if usage is not None else dict.fromkeys(USAGE_KEYS)
    record: dict[str, Any] = {
        "phase": phase,
        "status": classify_status(int(attempts or 0), status),
        "attempts": max(0, int(attempts or 0)),
        "duration_ms": max(0, int(duration_ms or 0)),
        "model": model or "",
        "protocol": protocol or "",
        "context_chars": max(0, int(context_chars or 0)),
        "prompt_tokens": normalized["prompt_tokens"],
        "completion_tokens": normalized["completion_tokens"],
        "total_tokens": normalized["total_tokens"],
        "error": safe_text(error) if error else "",
    }
    if step_index is not None:
        record["step_index"] = int(step_index)
    if extra:
        for key, value in extra.items():
            record.setdefault(key, value)
    return record


def _empty_totals() -> dict[str, Any]:
    return {
        "records": 0,
        "attempts": 0,
        "duration_ms": 0,
        "context_chars": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "unknown_usage_records": 0,
    }


def summarize(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """把步骤级记录聚合出运行级汇总（看板与 /metrics 接口共用）。

    * ``totals``：运行级累计（耗时、尝试次数、token、最大上下文占用、usage 缺失条数）；
    * ``by_phase``：按 architect / executor 分桶，便于看板拆分两段开销；
    * ``status_counts``：ok / retried / failed / blocked 计数；
    * ``steps``：原始记录列表，保持调用方写入的顺序（不排序、不裁剪）。
    """

    totals = _empty_totals()
    by_phase: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    steps: list[dict[str, Any]] = []

    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        steps.append(item)

        known = usage_known(item)
        for bucket in (
            totals,
            by_phase.setdefault(str(item.get("phase") or "unknown"), _empty_totals()),
        ):
            bucket["records"] += 1
            bucket["attempts"] += _coerce_int(item.get("attempts")) or 0
            bucket["duration_ms"] += _coerce_int(item.get("duration_ms")) or 0
            bucket["context_chars"] = max(
                bucket["context_chars"], _coerce_int(item.get("context_chars")) or 0
            )
            if known:
                for key in USAGE_KEYS:
                    bucket[key] += _coerce_int(item.get(key)) or 0
            else:
                bucket["unknown_usage_records"] += 1

        status = str(item.get("status") or STATUS_OK)
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "totals": totals,
        "by_phase": by_phase,
        "status_counts": status_counts,
        "steps": steps,
    }
