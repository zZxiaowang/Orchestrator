"""主备配置自动降级（failover）。

契约（对应纲领「弹性模型路由器」）：

* **只在明确可恢复的故障上降级**：限流（429）、网关/上游 5xx、请求超时、连接错误。
  鉴权失败（401/403）、参数与业务错误（400/404/405/422）、用户取消**绝不降级**，
  否则「Key 填错」会被伪装成「已自动恢复」，反而更难排查。
* **总尝试次数有硬上限**：一次模型逻辑调用最多尝试 ``max_attempts`` 次，
  候选固定为「主用 → 备用」，**不允许**主备来回循环。
* **流式只在尚未提交有效内容时透明切换**：一旦产出过非空内容，
  再失败就以明确错误结束，避免把两次生成拼成看似正常的重复输出。
* **全程脱敏**：事件/指标/日志里的地址去掉 userinfo 与 query，
  任何错误消息与结构化记录都不包含 API Key。
* **缺省不启用**：备用端点字段全为空时只有一个候选，行为与加这个功能之前完全一致。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.errors import ConfigurationError, RelayError

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_WIRE_API = "chat_completions"
PRIMARY_LABEL = "primary"
BACKUP_LABEL = "backup"

_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{4,}", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{4,}")


class FailureKind(StrEnum):
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    NETWORK = "network"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    CANCELLED = "cancelled"
    CONFIG = "config"
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        return self in RETRYABLE_KINDS


RETRYABLE_KINDS = frozenset(
    {FailureKind.RATE_LIMIT, FailureKind.SERVER, FailureKind.TIMEOUT, FailureKind.NETWORK}
)

_STATUS_KINDS: dict[int, FailureKind] = {
    400: FailureKind.INVALID_REQUEST,
    404: FailureKind.INVALID_REQUEST,
    405: FailureKind.INVALID_REQUEST,
    406: FailureKind.INVALID_REQUEST,
    409: FailureKind.INVALID_REQUEST,
    413: FailureKind.INVALID_REQUEST,
    415: FailureKind.INVALID_REQUEST,
    422: FailureKind.INVALID_REQUEST,
    401: FailureKind.AUTH,
    403: FailureKind.AUTH,
    408: FailureKind.TIMEOUT,
    429: FailureKind.RATE_LIMIT,
    500: FailureKind.SERVER,
    502: FailureKind.SERVER,
    503: FailureKind.SERVER,
    504: FailureKind.SERVER,
}

#: 状态码缺失时的文本兜底；关键字取自 relay._hint_for 的真实措辞
_MESSAGE_RULES: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    (FailureKind.CANCELLED, ("取消", "cancelled", "canceled")),
    (
        FailureKind.AUTH,
        (
            "invalid token",
            "invalid api key",
            "unauthorized",
            "forbidden",
            "key 无效",
            "无权访问",
            "无权限",
        ),
    ),
    (FailureKind.CONFIG, ("未配置", "not configured", "缺少 key", "缺少 api")),
    (FailureKind.RATE_LIMIT, ("限流", "rate limit", "too many requests", "429")),
    (FailureKind.TIMEOUT, ("超时", "timeout", "timed out")),
    (
        FailureKind.SERVER,
        (
            "回源失败",
            "暂时不可用",
            "上游异常",
            "bad gateway",
            "bad_response_status_code",
            "502",
            "503",
            "504",
        ),
    ),
    (
        FailureKind.INVALID_REQUEST,
        ("路径不存在", "参数", "unsupported", "invalid request", "校验"),
    ),
)


# ── 脱敏 ──


def redact_text(value: str) -> str:
    """抹掉文本里的 Key 形态字符串（sk-… / Bearer …）。"""
    text = str(value or "")
    text = _BEARER_RE.sub(r"\1***", text)
    return _KEY_RE.sub("sk-***", text)


def mask_endpoint(base_url: str) -> str:
    """只保留 scheme + host + path：去掉 userinfo、端口以外的 query，用于落盘/上报。"""
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return redact_text(raw)
    host = parsed.hostname or ""
    if not host:
        return redact_text(raw)
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path.rstrip('/')}"


# ── 故障分类 ──


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status", "status_code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        for key in ("status", "status_code"):
            value = details.get(key)
            if isinstance(value, int):
                return value
    return None


def _failure_text(exc: BaseException) -> str:
    parts = [str(exc) or exc.__class__.__name__]
    hint = getattr(exc, "hint", "")
    if hint:
        parts.append(str(hint))
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        for key in ("message", "reason", "detail"):
            value = details.get(key)
            if isinstance(value, str):
                parts.append(value)
    return " ".join(parts).lower()


def classify_failure(exc: BaseException) -> FailureKind:
    """把异常归类成可审计的失败原因；只有 RETRYABLE_KINDS 才允许降级。"""
    if isinstance(exc, asyncio.CancelledError):
        return FailureKind.CANCELLED
    if isinstance(exc, (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError)):
        return FailureKind.TIMEOUT
    if isinstance(exc, (httpx.NetworkError, httpx.ProxyError)):
        return FailureKind.NETWORK
    status = _status_of(exc)
    if status is not None:
        kind = _STATUS_KINDS.get(status)
        if kind is not None:
            return kind
        if status >= 500:
            return FailureKind.SERVER
    text = _failure_text(exc)
    for kind, needles in _MESSAGE_RULES:
        if any(needle in text for needle in needles):
            return kind
    return FailureKind.UNKNOWN


def is_retryable(exc: BaseException | FailureKind) -> bool:
    kind = exc if isinstance(exc, FailureKind) else classify_failure(exc)
    return kind.retryable


# ── 记录结构（事件 / 指标 / 日志共用）──


@dataclass
class RelayCandidate:
    """一个候选端点；``base_url`` 只放脱敏后的地址用于展示与上报。"""

    label: str
    alias: str
    model: str
    base_url: str = ""
    client: Any = None

    def safe_route(self) -> dict[str, str]:
        return {
            "role": self.label,
            "alias": self.alias,
            "model": self.model,
            "base_url": self.base_url,
        }


@dataclass
class FallbackAttempt:
    label: str
    alias: str
    model: str
    base_url: str = ""
    ok: bool = False
    failure: str = ""
    retryable: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.label,
            "alias": self.alias,
            "model": self.model,
            "base_url": self.base_url,
            "ok": self.ok,
            "failure": self.failure,
            "retryable": self.retryable,
            "reason": redact_text(self.reason)[:300],
        }


@dataclass
class FallbackOutcome:
    used_label: str = ""
    used_alias: str = ""
    used_model: str = ""
    switched: bool = False
    switch_reason: str = ""
    attempts: list[FallbackAttempt] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "used_role": self.used_label,
            "used_alias": self.used_alias,
            "used_model": self.used_model,
            "switched": self.switched,
            "switch_reason": redact_text(self.switch_reason)[:200],
            "attempts": [item.as_dict() for item in self.attempts],
        }


# ── 运行器 ──


class FailoverRunner:
    """按「主用 → 备用」顺序调用模型，并在允许的情况下透明切换。"""

    def __init__(
        self,
        candidates: Sequence[RelayCandidate],
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        on_attempt: Callable[[FallbackAttempt], None] | None = None,
    ) -> None:
        cleaned = [item for item in (candidates or []) if item is not None]
        if not cleaned:
            raise ValueError("主备降级至少需要一个候选配置。")
        self.max_attempts = max(1, int(max_attempts or DEFAULT_MAX_ATTEMPTS))
        # 硬上限：候选数不会超过尝试预算，也就不存在主备来回循环
        self.candidates = cleaned[: self.max_attempts]
        self.attempts: list[FallbackAttempt] = []
        self.last_outcome: FallbackOutcome | None = None
        self._on_attempt = on_attempt

    # ── 对外接口 ──

    async def acomplete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> tuple[Any, FallbackOutcome]:
        switched = False
        last_exc: BaseException | None = None
        for index, candidate in enumerate(self.candidates):
            try:
                result = await candidate.client.acomplete(
                    messages, model=candidate.model, json_mode=json_mode, temperature=temperature
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 需要按分类决定是否降级
                last_exc = exc
                kind = classify_failure(exc)
                self._record(candidate, ok=False, kind=kind, reason=self._failure_reason(exc))
                nxt = self.candidates[index + 1] if index + 1 < len(self.candidates) else None
                if not kind.retryable or nxt is None:
                    self._outcome(candidate, switched=switched)
                    raise self._exhausted(exc, switched) from exc
                switched = True
                self._outcome(
                    candidate,
                    switched=True,
                    switch_reason=self._switch_reason(candidate, nxt, kind, exc),
                )
                continue
            self._record(candidate, ok=True)
            return result, self._outcome(candidate, switched=switched)
        fallback_exc = last_exc or RuntimeError("没有可用的候选配置。")
        raise self._exhausted(fallback_exc, switched) from last_exc

    async def astream(
        self, messages: Sequence[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[str]:
        committed = False
        switched = False
        for index, candidate in enumerate(self.candidates):
            try:
                async for chunk in candidate.client.astream_with_fallback(
                    messages, model=candidate.model, **kwargs
                ):
                    if chunk:
                        committed = True
                    yield chunk
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 需要按分类决定是否降级
                kind = classify_failure(exc)
                self._record(candidate, ok=False, kind=kind, reason=self._failure_reason(exc))
                nxt = self.candidates[index + 1] if index + 1 < len(self.candidates) else None
                if committed:
                    self._outcome(candidate, switched=switched)
                    raise RelayError(
                        "流式输出已经开始，主用配置中途失败；为避免重复内容，本次不再自动切换。",
                        hint="请重试本次运行；若该配置反复失败，请把可用的备用配置改为主用。",
                        details={
                            "attempts": [item.as_dict() for item in self.attempts],
                            "committed": True,
                        },
                    ) from exc
                if not kind.retryable or nxt is None:
                    self._outcome(candidate, switched=switched)
                    raise self._exhausted(exc, switched) from exc
                switched = True
                self._outcome(
                    candidate,
                    switched=True,
                    switch_reason=self._switch_reason(candidate, nxt, kind, exc),
                )
                continue
            self._record(candidate, ok=True)
            self._outcome(candidate, switched=switched)
            return
        raise self._exhausted(RuntimeError("没有可用的候选配置。"), switched)

    # ── 内部 ──

    def _failure_reason(self, exc: BaseException) -> str:
        text = str(exc) or exc.__class__.__name__
        hint = getattr(exc, "hint", "") or ""
        return redact_text(f"{text} {hint}".strip())[:300]

    def _switch_reason(
        self, source: RelayCandidate, target: RelayCandidate, kind: FailureKind, exc: BaseException
    ) -> str:
        return redact_text(
            f"{source.alias} 失败（{kind.value}）：{self._failure_reason(exc)}"
            f"；已切换到 {target.alias}（{target.model}）"
        )

    def _record(
        self,
        candidate: RelayCandidate,
        *,
        ok: bool,
        kind: FailureKind | None = None,
        reason: str = "",
    ) -> FallbackAttempt:
        attempt = FallbackAttempt(
            label=candidate.label,
            alias=candidate.alias,
            model=candidate.model,
            base_url=candidate.base_url,
            ok=ok,
            failure=kind.value if kind else "",
            retryable=bool(kind and kind.retryable),
            reason=reason,
        )
        self.attempts.append(attempt)
        if self._on_attempt is not None:
            # 回调失败不能影响主流程
            with suppress(Exception):
                self._on_attempt(attempt)
        return attempt

    def _outcome(
        self, candidate: RelayCandidate, *, switched: bool, switch_reason: str = ""
    ) -> FallbackOutcome:
        outcome = FallbackOutcome(
            used_label=candidate.label,
            used_alias=candidate.alias,
            used_model=candidate.model,
            switched=switched,
            switch_reason=switch_reason,
            attempts=list(self.attempts),
        )
        self.last_outcome = outcome
        return outcome

    def _exhausted(self, exc: BaseException, switched: bool) -> BaseException:
        if not switched:
            # 从未切换：原样抛出，保持旧有的提示与状态码语义
            return exc
        return RelayError(
            "主用与备用配置均调用失败（共尝试 "
            f"{len(self.attempts)} 次）。最后一次：{self._failure_reason(exc)}",
            hint="请检查中转服务状态，或把可用性更好的那套配置设为主用。",
            details={"attempts": [item.as_dict() for item in self.attempts]},
        )


# ── 从 Settings 构造候选 ──

_PHASE_SCHEMA: dict[str, dict[str, str]] = {
    "architect": {
        "title": "架构段",
        "model": "architect_model",
        "base_url": "architect_base_url",
        "api_key": "architect_api_key",
        "wire_api": "architect_wire_api",
        "backup_model": "architect_backup_model",
        "backup_base_url": "architect_backup_base_url",
        "backup_api_key": "architect_backup_api_key",
        "backup_wire_api": "architect_backup_wire_api",
        "backup_label": "architect_backup_label",
    },
    "editor": {
        "title": "执行段",
        "model": "editor_model",
        "base_url": "editor_base_url",
        "api_key": "editor_api_key",
        "wire_api": "editor_wire_api",
        "backup_model": "editor_backup_model",
        "backup_base_url": "editor_backup_base_url",
        "backup_api_key": "editor_backup_api_key",
        "backup_wire_api": "editor_backup_wire_api",
        "backup_label": "editor_backup_label",
    },
}


def _phase_schema(phase: str) -> dict[str, str]:
    key = str(phase or "").strip().lower()
    schema = _PHASE_SCHEMA.get(key)
    if schema is None:
        raise ValueError(f"未知阶段：{phase}（只支持 architect / editor）")
    return schema


def _text(settings: Any, name: str, default: str = "") -> str:
    value = getattr(settings, name, default)
    return str(value).strip() if value is not None else ""


def _int(settings: Any, name: str, default: int) -> int:
    try:
        parsed = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _float(settings: Any, name: str, default: float) -> float:
    try:
        parsed = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def primary_fields(settings: Any, phase: str) -> dict[str, str]:
    """主用端点：阶段专属字段优先，留空回落到公共 RELAY_*。"""
    schema = _phase_schema(phase)
    return {
        "base_url": _text(settings, schema["base_url"]) or _text(settings, "relay_base_url"),
        "api_key": _text(settings, schema["api_key"]) or _text(settings, "relay_api_key"),
        "wire_api": _text(settings, schema["wire_api"])
        or _text(settings, "relay_wire_api")
        or DEFAULT_WIRE_API,
        "model": _text(settings, schema["model"]),
    }


def backup_fields(settings: Any, phase: str) -> dict[str, str]:
    """备用端点：只认阶段专属的 backup 字段，模型/协议留空时继承主用。"""
    schema = _phase_schema(phase)
    primary = primary_fields(settings, phase)
    return {
        "base_url": _text(settings, schema["backup_base_url"]),
        "api_key": _text(settings, schema["backup_api_key"]),
        "wire_api": _text(settings, schema["backup_wire_api"]) or primary["wire_api"],
        "model": _text(settings, schema["backup_model"]) or primary["model"],
        "label": _text(settings, schema["backup_label"]),
    }


def backup_configured(settings: Any, phase: str) -> bool:
    fields = backup_fields(settings, phase)
    return bool(fields["base_url"] and fields["api_key"])


def _same_endpoint(primary: dict[str, str], backup: dict[str, str]) -> bool:
    return (
        primary["base_url"].rstrip("/").lower() == backup["base_url"].rstrip("/").lower()
        and primary["model"].strip() == backup["model"].strip()
        and primary["api_key"] == backup["api_key"]
    )


def default_client_factory(
    *,
    base_url: str,
    api_key: str,
    wire_api: str,
    model: str,
    timeout: float,
    transport: Any = None,
) -> Any:
    from app.core.relay import RelayClient

    return RelayClient(
        base_url,
        api_key,
        wire_api=wire_api,
        timeout=timeout,
        transport=transport,
        model_hint=model,
    )


def build_candidates(
    settings: Any,
    phase: str,
    *,
    client_factory: Callable[..., Any] | None = None,
    timeout: float | None = None,
    transport: Any = None,
    with_backup: bool = True,
) -> list[RelayCandidate]:
    """构造「主用（→ 备用）」候选；备用未配置或与主用完全相同则只有主用。"""
    schema = _phase_schema(phase)
    title = schema["title"]
    factory = client_factory or default_client_factory
    if timeout is None:
        timeout = _float(settings, "request_timeout_seconds", 300.0)
    primary = primary_fields(settings, phase)
    candidates = [
        RelayCandidate(
            label=PRIMARY_LABEL,
            alias=f"{title}·主用",
            model=primary["model"],
            base_url=mask_endpoint(primary["base_url"]),
            client=factory(
                base_url=primary["base_url"],
                api_key=primary["api_key"],
                wire_api=primary["wire_api"],
                model=primary["model"],
                timeout=timeout,
                transport=transport,
            ),
        )
    ]
    if not with_backup:
        return candidates
    backup = backup_fields(settings, phase)
    if not (backup["base_url"] and backup["api_key"]):
        return candidates
    if _same_endpoint(primary, backup):
        return candidates
    alias = backup["label"] or "备用"
    candidates.append(
        RelayCandidate(
            label=BACKUP_LABEL,
            alias=f"{title}·{alias}",
            model=backup["model"],
            base_url=mask_endpoint(backup["base_url"]),
            client=factory(
                base_url=backup["base_url"],
                api_key=backup["api_key"],
                wire_api=backup["wire_api"],
                model=backup["model"],
                timeout=timeout,
                transport=transport,
            ),
        )
    )
    return candidates


def build_runner(
    settings: Any,
    phase: str,
    *,
    transport: Any = None,
    client_factory: Callable[..., Any] | None = None,
    on_attempt: Callable[[FallbackAttempt], None] | None = None,
    with_backup: bool = True,
) -> FailoverRunner:
    """编排器入口：一次调用拿到配好尝试预算的运行器。"""
    candidates = build_candidates(
        settings,
        phase,
        client_factory=client_factory,
        transport=transport,
        with_backup=with_backup,
    )
    return FailoverRunner(
        candidates,
        max_attempts=_int(settings, "fallback_max_attempts", DEFAULT_MAX_ATTEMPTS),
        on_attempt=on_attempt,
    )


def validate_backup_settings(settings: Any) -> None:
    """保存设置时调用：备用配置一旦填写，就必须成对且形状合法。"""
    from app.core.config import _validate_api_key, _validate_base_url

    for phase, schema in _PHASE_SCHEMA.items():
        title = schema["title"]
        fields = backup_fields(settings, phase)
        _validate_base_url(fields["base_url"], f"{title}备用配置的 base_url")
        _validate_api_key(fields["api_key"], f"{title}备用配置的 API Key")
        if bool(fields["base_url"]) != bool(fields["api_key"]):
            raise ConfigurationError(
                f"{title}的备用配置只填了一半：base_url 与 API Key 必须成对填写，"
                "或全部留空表示不启用自动降级。",
                details={"phase": phase},
            )
        if fields["wire_api"] not in ("chat_completions", "responses"):
            raise ConfigurationError(
                f"{title}备用配置的协议 '{fields['wire_api']}' 不受支持。",
                details={"phase": phase, "allowed": ["chat_completions", "responses"]},
            )


__all__ = [
    "BACKUP_LABEL",
    "DEFAULT_MAX_ATTEMPTS",
    "FailoverRunner",
    "FailureKind",
    "FallbackAttempt",
    "FallbackOutcome",
    "PRIMARY_LABEL",
    "RelayCandidate",
    "RETRYABLE_KINDS",
    "backup_configured",
    "backup_fields",
    "build_candidates",
    "build_runner",
    "classify_failure",
    "is_retryable",
    "mask_endpoint",
    "primary_fields",
    "redact_text",
    "validate_backup_settings",
]
