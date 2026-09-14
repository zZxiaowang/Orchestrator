"""主备配置自动降级：契约、边界与脱敏。

对应第 5 步验收标准：
1. 架构段/执行段各自可选主用与备用，缺省不启用降级；
2. 502、限流、超时触发切换；无效 Key、参数错误、用户取消不切换；
3. 总尝试次数有硬上限，主备不可循环；
4. 流式仅在未提交有效内容时透明切换；
5. 事件/指标/日志脱敏，任何结构体不含 API Key；
6. Key 栏填地址的保存拦截继续生效，旧设置可正常加载。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.core.config import Settings, _validate_api_key
from app.core.errors import ConfigurationError, RelayError
from app.core.fallback import (
    DEFAULT_MAX_ATTEMPTS,
    FailoverRunner,
    FailureKind,
    RelayCandidate,
    backup_configured,
    build_candidates,
    build_runner,
    classify_failure,
    mask_endpoint,
    redact_text,
    validate_backup_settings,
)
from app.core.relay import RelayResult


class GatewayFailure(Exception):
    """模拟中转异常：带 HTTP 状态码，等价于 relay 抛出的 RelayError。"""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"gateway error {status}")
        self.status = status


class FakeClient:
    def __init__(self, *, text: str = "ok", chunks=(), error=None, model: str = "") -> None:
        self.text = text
        self.chunks = list(chunks)
        self.error = error
        self.model = model
        self.calls: list[dict[str, object]] = []

    async def acomplete(self, messages, *, model, json_mode=False, temperature=None):
        self.calls.append({"model": model, "json_mode": json_mode, "stream": False})
        if self.error is not None:
            raise self.error
        return RelayResult(text=self.text, model=model or self.model)

    async def astream_with_fallback(self, messages, *, model, **kwargs):
        self.calls.append({"model": model, "stream": True})
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


class Factory:
    """记录 build_candidates 传给客户端的构造参数。"""

    def __init__(self) -> None:
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, **kwargs):
        self.kwargs.append(dict(kwargs))
        return FakeClient(model=str(kwargs.get("model", "")))


def candidate(client, *, label="primary", alias="架构段·主用", model="m-1", base_url=""):
    return RelayCandidate(label=label, alias=alias, model=model, base_url=base_url, client=client)


def two_candidates(primary, backup):
    return [
        candidate(primary),
        candidate(backup, label="backup", alias="架构段·备用", model="m-2"),
    ]


async def _collect(agen):
    return [chunk async for chunk in agen]


def _settings(**overrides) -> Settings:
    base = {
        "relay_base_url": "https://relay.example.com/v1",
        "relay_api_key": "sk-main000000",
        "architect_model": "gpt-5",
        "editor_model": "deepseek-v4",
    }
    base.update(overrides)
    return Settings(**base)


# ── 配置层 ──


def test_backup_disabled_by_default():
    settings = _settings()
    assert settings.fallback_max_attempts == DEFAULT_MAX_ATTEMPTS
    assert backup_configured(settings, "architect") is False
    assert backup_configured(settings, "editor") is False
    factory = Factory()
    candidates = build_candidates(settings, "architect", client_factory=factory)
    assert [item.label for item in candidates] == ["primary"]
    assert candidates[0].model == "gpt-5"
    assert factory.kwargs[0]["base_url"] == "https://relay.example.com/v1"
    assert factory.kwargs[0]["api_key"] == "sk-main000000"


def test_architect_and_editor_pick_backup_separately():
    settings = _settings(
        architect_backup_base_url="https://user:pw@backup-a.example.com/v1?token=1",
        architect_backup_api_key="sk-arch000000",
        architect_backup_model="gpt-5-mini",
        architect_backup_label="个人直连",
        editor_backup_base_url="https://backup-b.example.com/v1",
        editor_backup_api_key="sk-edit000000",
    )
    architect = build_candidates(settings, "architect", client_factory=Factory())
    editor = build_candidates(settings, "editor", client_factory=Factory())
    assert [item.label for item in architect] == ["primary", "backup"]
    assert [item.label for item in editor] == ["primary", "backup"]
    assert architect[1].model == "gpt-5-mini"
    assert architect[1].base_url == "https://backup-a.example.com/v1"
    assert "个人直连" in architect[1].alias
    assert editor[1].model == "deepseek-v4"
    assert editor[1].base_url == "https://backup-b.example.com/v1"


def test_backup_equal_to_primary_is_ignored():
    settings = _settings(
        architect_backup_base_url="https://relay.example.com/v1",
        architect_backup_api_key="sk-main000000",
        architect_backup_model="gpt-5",
    )
    assert backup_configured(settings, "architect") is True
    candidates = build_candidates(settings, "architect", client_factory=Factory())
    assert [item.label for item in candidates] == ["primary"]


def test_build_runner_reads_budget_from_settings():
    settings = _settings(
        architect_backup_base_url="https://backup.example.com/v1",
        architect_backup_api_key="sk-backup000000",
        fallback_max_attempts=2,
    )
    runner = build_runner(settings, "architect", client_factory=Factory())
    assert runner.max_attempts == 2
    assert [item.label for item in runner.candidates] == ["primary", "backup"]


def test_unknown_phase_is_rejected():
    with pytest.raises(ValueError):
        build_candidates(_settings(), "reviewer")


# ── 降级判定 ──


@pytest.mark.parametrize(
    "failure",
    [
        GatewayFailure(429, "rate limit"),
        GatewayFailure(500),
        GatewayFailure(502, "bad gateway"),
        GatewayFailure(503),
        GatewayFailure(504),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection reset"),
    ],
)
def test_recoverable_failures_switch_to_backup(failure):
    primary = FakeClient(error=failure)
    backup = FakeClient(text="from-backup")
    runner = FailoverRunner(two_candidates(primary, backup), max_attempts=3)
    result, outcome = asyncio.run(runner.acomplete([{"role": "user", "content": "hi"}]))
    assert result.text == "from-backup"
    assert outcome.switched is True
    assert outcome.used_label == "backup"
    assert len(primary.calls) == 1
    assert len(backup.calls) == 1
    assert outcome.attempts[0].ok is False
    assert outcome.attempts[0].retryable is True
    assert outcome.attempts[0].failure in {"rate_limit", "server", "timeout", "network"}
    assert outcome.attempts[1].ok is True
    assert outcome.attempts[1].failure == ""


@pytest.mark.parametrize(
    "failure",
    [
        GatewayFailure(401, "invalid token"),
        GatewayFailure(403, "forbidden"),
        GatewayFailure(400, "bad request"),
        GatewayFailure(404, "not found"),
        GatewayFailure(422, "invalid params"),
    ],
)
def test_non_recoverable_failures_do_not_switch(failure):
    primary = FakeClient(error=failure)
    backup = FakeClient(text="must-not-be-used")
    runner = FailoverRunner(two_candidates(primary, backup), max_attempts=3)
    with pytest.raises(GatewayFailure):
        asyncio.run(runner.acomplete([{"role": "user", "content": "hi"}]))
    assert backup.calls == []
    assert runner.last_outcome is not None
    assert runner.last_outcome.switched is False
    assert runner.last_outcome.attempts[0].failure in {"auth", "invalid_request"}


def test_auth_hint_text_without_status_is_not_retryable():
    class PlainRelayError(Exception):
        def __init__(self) -> None:
            super().__init__("调用失败")
            self.hint = "中转 Key 无效或未生效，请在设置里重新填写 RELAY_API_KEY。"

    assert classify_failure(PlainRelayError()) is FailureKind.AUTH
    assert classify_failure(PlainRelayError()).retryable is False


def test_gateway_hint_text_without_status_is_retryable():
    class PlainRelayError(Exception):
        def __init__(self) -> None:
            super().__init__("调用失败")
            self.hint = "网关回源失败（bad_response_status_code）。已自动重试。"

    assert classify_failure(PlainRelayError()) is FailureKind.SERVER
    assert classify_failure(PlainRelayError()).retryable is True


def test_user_cancellation_is_never_retried():
    class CancelClient(FakeClient):
        async def acomplete(self, messages, *, model, json_mode=False, temperature=None):
            raise asyncio.CancelledError()

    primary = CancelClient()
    backup = FakeClient(text="must-not-be-used")
    runner = FailoverRunner(two_candidates(primary, backup), max_attempts=3)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.acomplete([{"role": "user", "content": "hi"}]))
    assert backup.calls == []
    assert classify_failure(asyncio.CancelledError()) is FailureKind.CANCELLED


# ── 尝试预算 ──


def test_attempt_budget_never_cycles():
    primary = FakeClient(error=GatewayFailure(502))
    backup = FakeClient(error=GatewayFailure(502))
    runner = FailoverRunner(two_candidates(primary, backup), max_attempts=3)
    with pytest.raises(RelayError) as info:
        asyncio.run(runner.acomplete([{"role": "user", "content": "hi"}]))
    assert len(primary.calls) == 1
    assert len(backup.calls) == 1
    assert len(runner.attempts) == 2
    assert "均调用失败" in str(info.value)


def test_budget_caps_candidate_count():
    runner = FailoverRunner(
        two_candidates(FakeClient(), FakeClient()) + [candidate(FakeClient(), label="backup")],
        max_attempts=2,
    )
    assert [item.label for item in runner.candidates] == ["primary", "backup"]


def test_single_candidate_reraises_original_error():
    primary = FakeClient(error=GatewayFailure(502))
    runner = FailoverRunner([candidate(primary)], max_attempts=1)
    with pytest.raises(GatewayFailure):
        asyncio.run(runner.acomplete([{"role": "user", "content": "hi"}]))
    assert len(primary.calls) == 1
    assert runner.last_outcome is not None
    assert runner.last_outcome.switched is False


# ── 流式语义 ──


def test_stream_switches_before_any_content():
    primary = FakeClient(chunks=[], error=GatewayFailure(502))
    backup = FakeClient(chunks=["你好", "，世界"])
    runner = FailoverRunner(two_candidates(primary, backup), max_attempts=3)
    chunks = asyncio.run(_collect(runner.astream([{"role": "user", "content": "hi"}])))
    assert "".join(chunks) == "你好，世界"
    assert runner.last_outcome is not None
    assert runner.last_outcome.switched is True
    assert len(primary.calls) == 1
    assert len(backup.calls) == 1


def test_stream_does_not_switch_after_content():
    primary = FakeClient(chunks=["已经", "输出"], error=GatewayFailure(502))
    backup = FakeClient(chunks=["不应该出现"])
    runner = FailoverRunner(two_candidates(primary, backup), max_attempts=3)

    async def drain():
        return [item async for item in runner.astream([{"role": "user", "content": "hi"}])]

    with pytest.raises(RelayError) as info:
        asyncio.run(drain())
    assert "不再自动切换" in str(info.value)
    assert backup.calls == []
    assert runner.last_outcome is not None
    assert runner.last_outcome.switched is False
    assert runner.last_outcome.attempts[-1].ok is False


# ── 脱敏 ──


def test_mask_and_redact_helpers():
    assert mask_endpoint("https://user:pw@relay.example.com/v1?token=1") == (
        "https://relay.example.com/v1"
    )
    assert mask_endpoint("https://relay.example.com/v1/") == "https://relay.example.com/v1"
    assert mask_endpoint("") == ""
    assert redact_text("Bearer sk-abcdef123456") == "Bearer ***"
    assert redact_text("key=sk-abcdef123456") == "key=sk-***"


def test_records_are_redacted():
    secret = "sk-secret1234567890"
    primary = FakeClient(error=GatewayFailure(502, f"bad gateway key={secret}"))
    backup = FakeClient(error=GatewayFailure(502, f"still bad key={secret}"))
    runner = FailoverRunner(two_candidates(primary, backup), max_attempts=3)
    with pytest.raises(RelayError) as info:
        asyncio.run(runner.acomplete([{"role": "user", "content": "hi"}]))
    assert runner.last_outcome is not None
    blob = json.dumps(runner.last_outcome.as_dict(), ensure_ascii=False)
    dump = getattr(info.value, "as_dict", None)
    if callable(dump):
        blob += json.dumps(dump(), ensure_ascii=False, default=str)
    blob += str(info.value)
    assert secret not in blob
    assert "secret1234567890" not in blob
    assert "sk-***" in blob


def test_stream_records_are_redacted():
    secret = "sk-stream1234567890"
    primary = FakeClient(chunks=["内容"], error=GatewayFailure(502, f"boom {secret}"))
    backup = FakeClient(chunks=["不该出现"])
    runner = FailoverRunner(two_candidates(primary, backup), max_attempts=3)

    async def drain():
        return [item async for item in runner.astream([{"role": "user", "content": "hi"}])]

    with pytest.raises(RelayError) as info:
        asyncio.run(drain())
    assert runner.last_outcome is not None
    blob = json.dumps(runner.last_outcome.as_dict(), ensure_ascii=False) + str(info.value)
    assert secret not in blob
    assert "stream1234567890" not in blob


def test_attempt_callback_receives_redacted_records():
    seen = []
    primary = FakeClient(error=GatewayFailure(502, "key sk-leak1234567890"))
    backup = FakeClient(text="ok")
    runner = FailoverRunner(two_candidates(primary, backup), max_attempts=3, on_attempt=seen.append)
    asyncio.run(runner.acomplete([{"role": "user", "content": "hi"}]))
    assert [item.ok for item in seen] == [False, True]
    serialized = json.dumps([item.as_dict() for item in seen], ensure_ascii=False)
    assert "leak1234567890" not in serialized
    assert "sk-***" in serialized


def test_candidate_route_is_safe():
    item = candidate(FakeClient(), base_url="https://relay.example.com/v1")
    assert item.safe_route()["model"] == "m-1"
    assert item.safe_route()["base_url"] == "https://relay.example.com/v1"


# ── 设置保存拦截与旧数据兼容 ──


def test_backup_validation_blocks_url_like_key():
    with pytest.raises(ConfigurationError):
        _validate_api_key("https://backup.example.com/v1", "架构段备用配置的 API Key")
    settings = _settings(
        architect_backup_base_url="https://backup.example.com/v1",
        architect_backup_api_key="https://backup.example.com/v1",
    )
    with pytest.raises(ConfigurationError):
        validate_backup_settings(settings)


def test_backup_validation_requires_pair():
    settings = _settings(editor_backup_base_url="https://backup.example.com/v1")
    with pytest.raises(ConfigurationError):
        validate_backup_settings(settings)


def test_backup_validation_accepts_complete_and_empty():
    validate_backup_settings(_settings())
    validate_backup_settings(
        _settings(
            architect_backup_base_url="https://backup.example.com/v1",
            architect_backup_api_key="sk-backup000000",
        )
    )


def test_legacy_settings_load_with_defaults():
    settings = _settings()
    assert settings.architect_backup_base_url == ""
    assert settings.architect_backup_api_key == ""
    assert settings.editor_backup_base_url == ""
    assert settings.editor_backup_api_key == ""
    assert settings.architect_backup_wire_api == ""
    assert settings.fallback_max_attempts == 3
