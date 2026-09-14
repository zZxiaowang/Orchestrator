"""指标接线：真实跑一遍编排，确认 run.metrics 与 /metrics 接口真的有人写。

历史问题：指标模型、看板外壳、单元测试都在，但没有任何代码往 run.metrics 写数据，
所以界面上那块统计永远是空的。这些测试直接钉住"主链路必须落账"。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from tests.conftest import FakeRelay
from tests.test_api_flow import TERMINAL, build_client, wait_for_status


def _execute_once(client, run_id: str) -> dict:
    wait_for_status(client, run_id, {"awaiting_approval"})
    client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
    return wait_for_status(client, run_id, TERMINAL)


def test_run_records_architect_and_step_metrics(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立可验证的骨架"}).json()[
            "run"
        ]["id"]
        run = _execute_once(client, run_id)
        assert run["status"] == "done", run.get("error")

        metrics = run["metrics"]
        architect = [item for item in metrics if item["phase"] == "architect"]
        executor = [item for item in metrics if item["phase"] == "executor"]

        assert len(architect) == 1, metrics
        assert architect[0]["step_id"] is None
        assert architect[0]["calls"] >= 1
        # 分流 + 架构段两次调用都记在同一条上（含 JSON 重试时更多）
        assert architect[0]["context_chars"] > 0
        assert architect[0]["duration_ms"] >= 0
        assert architect[0]["route"]["model"] == "gpt-5"
        assert architect[0]["route"]["alias"]
        # 流式 usage 由 stream_options.include_usage 取回，不再永远是"未知"。
        # 这条任务的文案含明确产出动作，分流走启发式（零模型调用），
        # 所以架构段这里只有纲领生成这一次调用。
        assert architect[0]["usage_source"] == "provider"
        assert architect[0]["usage_reason"] == ""
        assert architect[0]["calls"] == 1
        assert architect[0]["total_tokens"] == 18
        # 健康运行不该显示"重试 1 次"：分流是一次独立调用，不是重试
        assert architect[0]["retries"] == 0

        assert [item["step_id"] for item in executor] == [1, 2]
        for entry in executor:
            assert entry["calls"] >= 1
            assert entry["context_chars"] > 0
            assert entry["total_tokens"] == 18
            assert entry["route"]["model"] == "deepseek-v4"
            assert entry["usage_source"] == "provider"

        # 步骤级字段同步：重试次数可从 step.retries 直接看到
        assert all(step["retries"] == 0 for step in run["steps"])


def test_metrics_endpoint_returns_contract_shape(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        _execute_once(client, run_id)

        payload = client.get(f"/api/v1/runs/{run_id}/metrics").json()
        assert payload["run_id"] == run_id
        assert payload["status"] == "done"
        assert [item["phase"] for item in payload["metrics"]][:1] == ["architect"]
        summary = payload["summary"]
        assert summary["architect"]["calls"] >= 1
        assert summary["executor"]["calls"] >= 2
        assert summary["architect"]["total_tokens"] == 18
        assert summary["unknown_usage"] == []


def test_forced_json_retry_is_the_only_thing_counted_as_retry(tmp_path: Path):
    """输出不是合法 JSON 而强制重试时，账本必须体现出来（含那次额外调用）。"""

    relay = FakeRelay(garbage_first_stream=True)
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        run = wait_for_status(client, run_id, {"awaiting_approval"})

    architect = [item for item in run["metrics"] if item["phase"] == "architect"][0]
    assert architect["retries"] == 1
    assert architect["calls"] == 2  # 流式（垃圾输出）+ 强制 JSON 重试


def test_metrics_endpoint_404_for_unknown_run(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        response = client.get("/api/v1/runs/20260101-000000-zzzz/metrics")
        assert response.status_code == 404


def test_streaming_requests_ask_for_usage(tmp_path: Path):
    """没有这一步，流式调用的 usage 永远是"未知"，看板就是摆设。"""

    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        _execute_once(client, run_id)

    streamed = [item for item in relay.requests if item["body"].get("stream")]
    assert streamed, "本用例应当产生流式请求"
    assert all(
        item["body"].get("stream_options", {}).get("include_usage") is True for item in streamed
    )


def test_failed_architect_call_still_records_usage_attempt(tmp_path: Path):
    """失败也要记账：否则"跑了但没成"在统计里是零成本，用户无从判断值不值得重试。"""

    class FailingArchitectRelay(FakeRelay):
        def handler(self, request):
            body = request.content.decode("utf-8", errors="ignore")
            if "资深架构师" in body:
                return httpx.Response(500, json={"error": {"message": "boom"}})
            return super().handler(request)

    relay = FailingArchitectRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        run = wait_for_status(client, run_id, {"failed"})

    architect = [item for item in run["metrics"] if item["phase"] == "architect"]
    assert architect, run.get("error")
    # 分流走启发式（零调用）。架构段两次调用都失败且都被记下：
    # 1) 流式请求重试 3 次后失败；2) 降级为非流式再试一次也失败。
    assert architect[0]["calls"] == 2
    assert architect[0]["retries"] == 4  # 传输层重试 = 6 次 HTTP - 2 次逻辑调用
    assert architect[0]["usage_source"] == "unknown"
    assert architect[0]["usage_reason"] == "provider_stream_no_usage"
    assert architect[0]["total_tokens"] is None
