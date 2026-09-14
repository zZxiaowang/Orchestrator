"""第 3 步验收 · 指标采集：成功 / 重试后成功 / 失败 / blocked。

直接驱动 app.services.metrics_sink，先把「采集口径」这一层锁死；
编排器接入后，同一批断言仍然成立。
"""

from __future__ import annotations

from app.core.relay import RelayResult
from app.services import metrics_sink


class FakeRun:
    """最小 run 替身：只暴露采集层会读写的字段。"""

    def __init__(self, *, run_id: str = "run-1", status: str = "executing") -> None:
        self.run_id = run_id
        self.status = status
        self.metrics: dict = {"architect": None, "steps": []}


def make_result(**overrides) -> RelayResult:
    payload = {
        "text": "ok",
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "attempts": 1,
        "duration_ms": 120,
        "protocol": "chat_completions",
    }
    payload.update(overrides)
    return RelayResult(**payload)


# ── 架构段 ──────────────────────────────────────────────────────────────


def test_architect_success_records_calls_duration_and_route():
    run = FakeRun()
    entry = metrics_sink.record_architect(
        run,
        make_result(attempts=1, duration_ms=812),
        route={"alias": "默认", "model": "gpt-4o"},
    )
    assert entry["calls"] == 1
    assert entry["retries"] == 0
    assert entry["duration_ms"] == 812
    assert entry["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert entry["route"]["alias"] == "默认"
    assert entry["status"] == "done"
    assert run.metrics["architect"] is entry


def test_retry_then_success_accumulates_attempts():
    run = FakeRun()
    metrics_sink.record_architect(run, make_result(attempts=3, duration_ms=900))
    entry = metrics_sink.record_architect(run, make_result(attempts=1, duration_ms=100))
    assert entry["calls"] == 4
    assert entry["retries"] == 3
    assert entry["duration_ms"] == 1000
    assert entry["usage"]["total_tokens"] == 30


def test_failed_call_still_records_attempts_and_duration():
    run = FakeRun()
    entry = metrics_sink.record_architect(run, None, status="failed", attempts=3, duration_ms=1500)
    assert entry["status"] == "failed"
    assert entry["calls"] == 3
    assert entry["retries"] == 2
    assert entry["duration_ms"] == 1500
    assert entry["usage"] == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }


def test_legacy_run_without_metrics_gets_backfilled():
    run = FakeRun()
    run.metrics = None
    metrics_sink.record_architect(run, make_result())
    assert isinstance(run.metrics, dict)
    assert run.metrics["steps"] == []
    assert run.metrics["architect"]["calls"] == 1


# ── 执行段 ──────────────────────────────────────────────────────────────


def test_step_entry_carries_context_and_fetched_files():
    run = FakeRun()
    entry = metrics_sink.record_step(
        run,
        "2",
        make_result(attempts=2, duration_ms=300),
        status="done",
        context_chars=12000,
        fetched=3,
        route={"alias": "个人 Key"},
    )
    assert entry["step_id"] == "2"
    assert entry["calls"] == 2
    assert entry["retries"] == 1
    assert entry["context_chars"] == 12000
    assert entry["fetched"] == 3
    assert run.metrics["steps"] == [entry]


def test_same_step_id_merges_instead_of_duplicating():
    run = FakeRun()
    metrics_sink.record_step(run, "1", make_result(attempts=1), status="running")
    metrics_sink.record_step(run, "1", make_result(attempts=2), status="done", context_chars=100)
    assert len(run.metrics["steps"]) == 1
    step = run.metrics["steps"][0]
    assert step["calls"] == 3
    assert step["retries"] == 2
    assert step["status"] == "done"
    assert step["context_chars"] == 100


def test_blocked_step_records_entry_without_calls():
    run = FakeRun()
    entry = metrics_sink.record_step(
        run, "3", None, status="blocked", duration_ms=42, context_chars=900, fetched=0
    )
    assert entry["calls"] == 0
    assert entry["retries"] == 0
    assert entry["status"] == "blocked"
    assert entry["duration_ms"] == 42
    assert entry["context_chars"] == 900
    assert entry["fetched"] == 0


# ── usage 归一化（两种协议） ─────────────────────────────────────────────


def test_chat_completions_usage_normalized():
    run = FakeRun()
    metrics_sink.record_architect(
        run, make_result(usage={"prompt_tokens": 4, "completion_tokens": 6})
    )
    entry = run.metrics["architect"]
    assert entry["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 6,
        "total_tokens": 10,
    }
    assert entry["protocol"] == "chat_completions"


def test_responses_usage_normalized():
    run = FakeRun()
    metrics_sink.record_architect(
        run,
        make_result(
            usage={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            protocol="responses",
        ),
    )
    entry = run.metrics["architect"]
    assert entry["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "total_tokens": 9,
    }
    assert entry["protocol"] == "responses"


def test_unknown_usage_stays_none():
    run = FakeRun()
    metrics_sink.record_architect(run, make_result(usage={}))
    assert run.metrics["architect"]["usage"] == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }


# ── 汇总 ────────────────────────────────────────────────────────────────


def test_summarize_totals_are_none_safe():
    run = FakeRun()
    metrics_sink.record_architect(
        run, make_result(usage={"prompt_tokens": 10, "completion_tokens": 5})
    )
    metrics_sink.record_step(
        run,
        "1",
        make_result(attempts=2, duration_ms=200),
        status="done",
        context_chars=1000,
        fetched=1,
    )
    metrics_sink.record_step(run, "2", None, status="blocked", duration_ms=5, context_chars=3000)

    totals = metrics_sink.summarize(run)["totals"]
    assert totals["calls"] == 3
    assert totals["retries"] == 1
    assert totals["duration_ms"] == 120 + 200 + 5
    assert totals["usage"]["total_tokens"] == 30
    assert totals["unknown_usage_calls"] == 0
    assert totals["steps"] == 2
    assert totals["blocked_steps"] == 1
    assert totals["failed_steps"] == 0
    assert totals["context_chars_total"] == 4000
    assert totals["context_chars_peak"] == 3000
    assert totals["fetched_files"] == 1


def test_unknown_usage_calls_are_counted_separately():
    run = FakeRun()
    metrics_sink.record_step(run, "1", make_result(usage={}, attempts=2), status="done")
    totals = metrics_sink.summarize(run)["totals"]
    assert totals["unknown_usage_calls"] == 2
    assert totals["usage"]["total_tokens"] is None


def test_collection_is_safe_without_run():
    assert metrics_sink.read_metrics(None) == {}
    assert metrics_sink.ensure_metrics(None) == {"architect": None, "steps": []}
    assert metrics_sink.summarize(None)["totals"]["steps"] == 0
    assert metrics_sink.record_architect(None, make_result())["calls"] == 1
    assert metrics_sink.record_step(None, "1", make_result())["step_id"] == "1"
