"""usage 缺失时的估算：必须算得出来、且明确标注是"估算"。"""

from __future__ import annotations

from app.core.relay import CallStats
from app.schemas.run import PhaseMetrics
from app.services.metrics import apply_call, estimate_usage, usage_verdict


def test_estimate_scales_with_chars():
    small = estimate_usage(350, 70)
    large = estimate_usage(3500, 700)
    assert small["total_tokens"] < large["total_tokens"]
    assert small["total_tokens"] == small["prompt_tokens"] + small["completion_tokens"]
    assert small["prompt_tokens"] >= 1 and small["completion_tokens"] >= 1


def test_provider_usage_wins_over_estimate():
    stats = CallStats()
    stats.calls = 1
    stats.attempts = 1
    stats.output_chars = 1000
    stats.record_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    entry = PhaseMetrics(phase="executor", step_id=1)
    apply_call(entry, stats, context_chars=9999)
    assert entry.total_tokens == 15
    assert entry.usage_source == "provider"
    assert entry.usage_reason == ""


def test_missing_usage_is_estimated_and_labelled():
    """提供方没给 usage 时：不再显示"未知"，而是给出估算值并标注来源。"""

    stats = CallStats()
    stats.calls = 1
    stats.attempts = 1
    stats.streamed = True
    stats.output_chars = 1400  # 模型确实产出了内容，只是网关没回 usage
    entry = PhaseMetrics(phase="executor", step_id=2)
    apply_call(entry, stats, context_chars=7000)

    assert entry.usage_source == "estimated"
    assert entry.usage_reason == "estimated_from_chars"
    assert entry.prompt_tokens and entry.prompt_tokens > 1000  # 7000/3.5 = 2000
    assert entry.completion_tokens == 400  # 1400/3.5
    assert entry.total_tokens == entry.prompt_tokens + entry.completion_tokens


def test_no_output_and_no_usage_stays_unknown():
    """既没 usage 又没有产出（例如调用直接失败）：保持"未知"，不编数字。"""

    stats = CallStats()
    stats.calls = 1
    entry = PhaseMetrics(phase="architect", step_id=None)
    apply_call(entry, stats, context_chars=1200)
    assert entry.usage_source == "unknown"
    assert entry.total_tokens is None


def test_verdict_reports_partial_usage():
    stats = CallStats()
    stats.calls = 2
    stats.attempts = 2
    stats.output_chars = 10
    stats.record_usage({"total_tokens": 7})
    assert usage_verdict(stats) == ("provider", "provider_partial_usage")
