"""运行指标数据契约与持久化健壮性测试（迭代第 2 步）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.schemas.run import (
    PHASE_ARCHITECT,
    PHASE_EXECUTOR,
    PhaseMetrics,
    Run,
    RunStatus,
    RunStep,
    StepStatus,
    sanitize_route,
)
from app.schemas.step import ProviderUsage, StepOutput
from app.services.storage import RunStore

LEGACY_RUN_ID = "20260914-000000-legacy"


def _store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


def _write_legacy_run(store: RunStore, run_id: str = LEGACY_RUN_ID) -> Path:
    """写出旧版本（完全没有 metrics / retries 字段）的 run.json。"""
    payload = {
        "id": run_id,
        "title": "旧运行",
        "task": "把现有服务补上日志",
        "status": "done",
        "plan": None,
        "plan_raw": "",
        "plan_revision": 0,
        "steps": [
            {
                "id": 1,
                "title": "第一步",
                "status": "done",
                "context_chars": 1234,
                "started_at": "2026-09-14T00:00:00+00:00",
                "finished_at": "2026-09-14T00:00:02+00:00",
            }
        ],
        "messages": [],
        "user_notes": [],
        "route": {},
        "error": None,
        "created_at": "2026-09-14T00:00:00+00:00",
        "updated_at": "2026-09-14T00:00:02+00:00",
    }
    directory = store.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "run.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_legacy_run_json_loads_with_metric_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_legacy_run(store)

    run = store.load(LEGACY_RUN_ID)

    assert run.status is RunStatus.DONE
    assert run.metrics == []
    assert run.steps[0].retries == 0
    assert run.steps[0].context_chars == 1234

    summary = run.metrics_summary()
    assert summary["architect"]["total_tokens"] is None
    assert summary["executor"]["total_tokens"] is None
    assert summary["unknown_usage"] == []


def test_phase_metrics_documented_defaults() -> None:
    metrics = PhaseMetrics()

    assert metrics.phase == PHASE_EXECUTOR
    assert metrics.step_id is None
    assert metrics.calls == 0
    assert metrics.retries == 0
    assert metrics.context_chars == 0
    assert metrics.prompt_tokens is None
    assert metrics.completion_tokens is None
    assert metrics.total_tokens is None
    assert metrics.usage_source == "unknown"
    assert metrics.usage_reason == ""
    assert metrics.duration_ms == 0
    assert metrics.route == {}


def test_metrics_roundtrip_distinguishes_architect_and_executor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = Run(id="20260914-000001-abcd", title="新运行", status=RunStatus.EXECUTING)
    run.steps.append(RunStep(id=1, title="第一步", status=StepStatus.DONE, context_chars=900))

    architect = run.metrics_for(PHASE_ARCHITECT)
    architect.calls = 2
    architect.retries = 1
    architect.context_chars = 4000
    architect.prompt_tokens = 1000
    architect.completion_tokens = 500
    architect.total_tokens = 1500
    architect.usage_source = "provider"
    architect.duration_ms = 3200
    architect.route = sanitize_route(
        {
            "alias": "中转",
            "model": "gpt-5",
            "protocol": "responses",
            "base_url": "https://relay.example.com/v1",
        }
    )

    executor = run.metrics_for(PHASE_EXECUTOR, step_id=1)
    executor.calls = 1
    executor.context_chars = 900
    executor.duration_ms = 800
    executor.usage_source = "unknown"
    executor.usage_reason = "provider_no_usage"

    store.save(run)
    loaded = store.load(run.id)

    assert [item.phase for item in loaded.metrics] == [PHASE_ARCHITECT, PHASE_EXECUTOR]

    arch = next(item for item in loaded.metrics if item.phase == PHASE_ARCHITECT)
    assert arch.step_id is None
    assert (arch.calls, arch.retries) == (2, 1)
    assert (arch.prompt_tokens, arch.completion_tokens, arch.total_tokens) == (1000, 500, 1500)
    assert arch.context_chars == 4000
    assert arch.duration_ms == 3200
    assert arch.route["model"] == "gpt-5"
    assert arch.route["base_url"] == "https://relay.example.com/v1"

    exe = next(item for item in loaded.metrics if item.phase == PHASE_EXECUTOR)
    assert exe.step_id == 1
    assert exe.total_tokens is None
    assert exe.usage_reason == "provider_no_usage"
    assert loaded.steps[0].context_chars == 900

    summary = loaded.metrics_summary()
    assert summary["architect"]["total_tokens"] == 1500
    assert summary["architect"]["retries"] == 1
    assert summary["architect"]["duration_ms"] == 3200
    assert summary["executor"]["total_tokens"] is None
    assert summary["executor"]["context_chars"] == 900
    assert summary["unknown_usage"] == [
        {"phase": PHASE_EXECUTOR, "step_id": 1, "reason": "provider_no_usage"}
    ]


def test_unknown_usage_is_none_not_zero_and_run_still_completes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = Run(id="20260914-000002-abcd", status=RunStatus.EXECUTING)
    step = RunStep(id=1, title="第一步", status=StepStatus.RUNNING)
    run.steps.append(step)

    metrics = run.metrics_for(PHASE_EXECUTOR, step_id=1)
    metrics.calls = 1
    metrics.usage_source = "unknown"
    metrics.usage_reason = "provider_no_usage"

    step.status = StepStatus.DONE
    run.status = RunStatus.DONE
    store.save(run)

    loaded = store.load(run.id)
    assert loaded.status is RunStatus.DONE
    recorded = loaded.metrics[0]
    assert recorded.total_tokens is None
    assert recorded.total_tokens != 0
    assert loaded.metrics_summary()["executor"]["total_tokens"] is None


def test_route_sanitizer_drops_secrets() -> None:
    metrics = PhaseMetrics(
        route={
            "model": "deepseek-chat",
            "protocol": "chat_completions",
            "base_url": "https://user:sk-secret@relay.example.com/v1?key=sk-secret",
            "alias": "中转",
            "api_key": "sk-should-not-persist",
            "headers": {"Authorization": "Bearer sk-should-not-persist"},
        }
    )

    assert metrics.route["model"] == "deepseek-chat"
    assert metrics.route["alias"] == "中转"
    assert metrics.route["base_url"] == "https://relay.example.com/v1"
    assert "api_key" not in metrics.route
    assert "headers" not in metrics.route

    dumped = json.dumps(metrics.model_dump(mode="json"), ensure_ascii=False)
    assert "sk-secret" not in dumped
    assert "sk-should-not-persist" not in dumped


def test_provider_usage_normalizes_names_and_keeps_unknown_none() -> None:
    responses = ProviderUsage.model_validate({"input_tokens": 120, "output_tokens": 30})
    assert responses.prompt_tokens == 120
    assert responses.completion_tokens == 30
    assert responses.total_tokens == 150

    missing = ProviderUsage.model_validate({})
    assert missing.prompt_tokens is None
    assert missing.total_tokens is None

    explicit_zero = ProviderUsage.model_validate({"prompt_tokens": 0, "completion_tokens": 0})
    assert explicit_zero.total_tokens == 0


def test_step_output_carries_usage_without_faking_zero() -> None:
    output = StepOutput.model_validate(
        {"summary": "完成", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    )
    assert output.usage is not None
    assert output.usage.total_tokens == 15

    default = StepOutput.model_validate({"summary": "完成"})
    assert default.usage is None

    noisy = StepOutput.model_validate(
        {"summary": "完成", "usage": {"prompt_tokens": 1, "api_key": "sk-x"}}
    )
    assert noisy.usage is not None
    assert noisy.usage.prompt_tokens == 1


def test_save_failure_keeps_previous_run_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = Run(id="20260914-000003-abcd", title="第一次", status=RunStatus.EXECUTING)
    store.save(run)
    directory = store.run_dir(run.id)
    before = (directory / "run.json").read_text(encoding="utf-8")

    def _boom(*args: object, **kwargs: object) -> str:
        raise TypeError("不可序列化")

    monkeypatch.setattr(json, "dumps", _boom)
    run.title = "第二次"
    with pytest.raises(TypeError):
        store.save(run)
    monkeypatch.undo()

    assert (directory / "run.json").read_text(encoding="utf-8") == before
    assert not (directory / "run.json.tmp").exists()
    assert store.load(run.id).title == "第一次"


def test_save_failure_during_flush_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = Run(id="20260914-000004-abcd", title="第一次", status=RunStatus.EXECUTING)
    store.save(run)
    directory = store.run_dir(run.id)
    before = (directory / "run.json").read_text(encoding="utf-8")

    def _boom(fd: int) -> None:
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(os, "fsync", _boom)
    with pytest.raises(OSError):
        store.save(run)

    assert (directory / "run.json").read_text(encoding="utf-8") == before
    assert not (directory / "run.json.tmp").exists()
    assert store.load(run.id).title == "第一次"


def test_first_save_failure_leaves_no_partial_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = Run(id="20260914-000005-abcd", status=RunStatus.PLANNING)

    def _boom(*args: object, **kwargs: object) -> str:
        raise TypeError("不可序列化")

    monkeypatch.setattr(json, "dumps", _boom)
    with pytest.raises(TypeError):
        store.save(run)
    monkeypatch.undo()

    directory = store.run_dir(run.id)
    assert not (directory / "run.json").exists()
    assert not (directory / "run.json.tmp").exists()
    assert store.exists(run.id) is False
