"""第 3 步验收 · 统计接口与 metrics_updated 事件的形状契约。

后端端点由 app/api/routes.py 暴露（GET /api/runs/{run_id}/metrics）。
这里锁定它必须复用的响应形状、404 语义与事件载荷，
避免接口层和采集层各写一套字段名。
"""

from __future__ import annotations

import json

from app.services import metrics_sink


def _run_payload() -> dict:
    return {
        "run_id": "run-42",
        "status": "done",
        "metrics": {
            "architect": {
                "calls": 1,
                "retries": 0,
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
                "duration_ms": 900,
                "protocol": "chat_completions",
                "status": "done",
                "route": {"alias": "默认", "model": "gpt-4o"},
            },
            "steps": [
                {
                    "step_id": "1",
                    "calls": 2,
                    "retries": 1,
                    "usage": {
                        "prompt_tokens": 30,
                        "completion_tokens": 10,
                        "total_tokens": 40,
                    },
                    "duration_ms": 2400,
                    "protocol": "responses",
                    "status": "done",
                    "context_chars": 15432,
                    "fetched": 2,
                },
                {
                    "step_id": "2",
                    "calls": 0,
                    "retries": 0,
                    "usage": {
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "total_tokens": None,
                    },
                    "duration_ms": 3,
                    "status": "blocked",
                    "context_chars": 900,
                    "fetched": 0,
                },
            ],
        },
    }


def test_unknown_run_has_no_metrics_response():
    # 路由层据此返回 404，而不是伪造一份空统计
    assert metrics_sink.metrics_response(None) is None
    assert metrics_sink.metrics_response(None, run_id="missing") is None


def test_valid_run_returns_summary_and_step_details():
    payload = metrics_sink.metrics_response(_run_payload())
    assert payload is not None
    assert payload["run_id"] == "run-42"
    assert payload["status"] == "done"
    assert payload["architect"]["calls"] == 1
    assert [step["step_id"] for step in payload["steps"]] == ["1", "2"]
    assert payload["steps"][0]["context_chars"] == 15432
    assert payload["steps"][0]["fetched"] == 2

    totals = payload["totals"]
    assert totals["calls"] == 3
    assert totals["retries"] == 1
    assert totals["duration_ms"] == 900 + 2400 + 3
    assert totals["usage"]["total_tokens"] == 160
    assert totals["steps"] == 2
    assert totals["blocked_steps"] == 1


def test_run_id_falls_back_to_path_parameter():
    payload = metrics_sink.metrics_response({"status": "done", "metrics": {}}, run_id="run-9")
    assert payload is not None
    assert payload["run_id"] == "run-9"


def test_metrics_response_is_json_serializable():
    payload = metrics_sink.metrics_response(_run_payload())
    dumped = json.dumps(payload, ensure_ascii=False)
    # None 原样输出，由前端渲染成「未知」，后端不做文字替换
    assert "未知" not in dumped
    assert json.loads(dumped)["totals"]["steps"] == 2


def test_metrics_event_carries_monotonic_seq_without_timestamp():
    run = _run_payload()
    first = metrics_sink.event_payload(run, seq=7)
    second = metrics_sink.event_payload(run, seq=8)

    assert metrics_sink.METRICS_EVENT == "metrics_updated"
    assert first["seq"] == 7
    assert second["seq"] == 8
    assert second["seq"] > first["seq"]
    for banned in ("ts", "time", "timestamp", "at", "created_at"):
        assert banned not in first

    assert first["run_id"] == "run-42"
    assert first["metrics"]["steps"][1]["status"] == "blocked"
    assert first["metrics"]["totals"]["retries"] == 1


def test_event_payload_is_json_serializable():
    dumped = json.dumps(metrics_sink.event_payload(_run_payload(), seq=1), ensure_ascii=False)
    assert '"steps"' in dumped
    assert '"totals"' in dumped


def test_bytes_error_body_is_never_concatenated_raw():
    from app.core.relay import _excerpt

    text = _excerpt(b'{"error":"bad gateway"} \xff')
    assert isinstance(text, str)
    assert "bad gateway" in text
