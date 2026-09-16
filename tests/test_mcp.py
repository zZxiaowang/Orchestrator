"""MCP：协议客户端（stdio / HTTP）、三道闸门、工具清单缓存，以及"模型自主调用"闭环。

用的都是**真实协议**：stdio 打的是仓库自带的示例服务器脚本（零依赖、离线可跑），
HTTP 用 MockTransport 覆盖 JSON 与 SSE 两种响应形态。
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from app.capabilities import mcp as mcp_module
from app.capabilities.mcp import build_client, call_tool, list_tools
from app.schemas.capability import Capability, CapabilityKind
from tests.conftest import FakeRelay, build_project_client

TERMINAL = {"done", "failed", "cancelled", "blocked"}


def wait_for_status(client, run_id: str, expected: set[str], timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/v1/runs/{run_id}").json()["run"]
        if last["status"] in expected:
            return last
        time.sleep(0.05)
    raise AssertionError(f"状态未在 {timeout}s 内变为 {expected}，当前 {last.get('status')}")


def _demo_capability(root: Path | None = None) -> Capability:
    command, args = mcp_module_preset_launcher()
    return Capability(
        id="mcp.demo",
        kind=CapabilityKind.MCP,
        name="示例 MCP",
        enabled=True,
        meta={"transport": "stdio", "command": command, "args": args, "trusted": True},
    )


def mcp_module_preset_launcher() -> tuple[str, list[str]]:
    from app.api.capabilities import _demo_server_launcher

    return _demo_server_launcher()


def test_stdio_client_lists_and_calls_tools(tmp_path: Path):
    capability = _demo_capability(tmp_path)
    tools = asyncio_run(list_tools(capability))
    assert [item.name for item in tools] == ["echo", "now"]
    assert tools[0].input_schema.get("properties")

    result = asyncio_run(call_tool(capability, "echo", {"text": "你好"}))
    assert result.ok is True
    assert result.content == "echo: 你好"
    assert result.duration_ms >= 0

    unknown = asyncio_run(call_tool(capability, "nope", {}))
    assert unknown.ok is False
    assert "未知工具" in unknown.error


def test_stdio_client_reports_missing_command(tmp_path: Path):
    capability = Capability(
        id="mcp.bad",
        kind=CapabilityKind.MCP,
        name="坏服务器",
        meta={"transport": "stdio", "command": "", "trusted": True},
    )
    import pytest

    with pytest.raises(Exception) as exc:
        build_client(capability)
    assert getattr(exc.value, "code", "") == "invalid_mcp_server"


def test_http_client_parses_json_and_sse():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.content.decode("utf-8")
        calls.append(payload)
        if "initialize" in payload:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}
            )
        if "tools/list" in payload:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [{"name": "ping", "description": "ping", "inputSchema": {}}]
                    },
                },
            )
        # tools/call 这次用 SSE 形态回，验证两种解析都работа
        body = (
            "event: message\n"
            'data: {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "pong"}]}}\n\n'
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    capability = Capability(
        id="mcp.http",
        kind=CapabilityKind.MCP,
        name="HTTP 服务器",
        meta={"transport": "http", "url": "https://mcp.test/rpc", "trusted": True},
    )
    transport = httpx.MockTransport(handler)
    tools = asyncio_run(list_tools(capability, transport=transport))
    assert [item.name for item in tools] == ["ping"]
    result = asyncio_run(call_tool(capability, "ping", {}, transport=transport))
    assert result.ok is True and result.content == "pong"
    # 每个操作 3 次 POST：initialize + notifications/initialized + 实际请求
    assert len(calls) == 6


def test_presets_cover_common_servers():
    ids = {item["id"] for item in mcp_module.MCP_PRESETS}
    assert {
        "filesystem",
        "git",
        "fetch",
        "sqlite",
        "memory",
        "time",
        "playwright",
        "sequential-thinking",
    } <= ids
    # 预设只给启动方式，不带任何密钥字段
    for item in mcp_module.MCP_PRESETS:
        assert "api_key" not in item and "token" not in item


def test_api_adds_server_disabled_and_untrusted(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        presets = client.get("/api/v1/capabilities/mcp/presets").json()["presets"]
        assert len(presets) >= 8

        created = client.post(
            "/api/v1/capabilities/mcp/servers", json={"preset_id": "demo", "name": "示例 MCP"}
        )
        assert created.status_code == 201, created.text
        capability = created.json()["capability"]
        # 会跑代码的能力默认**不启用、未确认信任**
        assert capability["enabled"] is False
        assert capability["meta"]["trusted"] is False

        # 未启用 → 拒绝，并说清去哪儿打开
        blocked = client.get(f"/api/v1/capabilities/{capability['id']}/mcp/tools")
        assert blocked.status_code == 400
        assert blocked.json()["error"]["code"] == "mcp_disabled"
        assert "能力中心" in blocked.json()["error"]["message"]
        disabled = client.post(
            f"/api/v1/capabilities/{capability['id']}/mcp/call", json={"tool": "echo"}
        )
        assert disabled.status_code == 400
        assert disabled.json()["error"]["code"] == "mcp_disabled"

        # 启用但没确认信任 → 仍然拒绝
        client.post(f"/api/v1/capabilities/{capability['id']}/enable")
        untrusted = client.post(
            f"/api/v1/capabilities/{capability['id']}/mcp/call", json={"tool": "echo"}
        )
        assert untrusted.status_code == 400
        assert untrusted.json()["error"]["code"] == "mcp_needs_trust"

        # 确认信任后：列工具 → 调用成功 → 审计留痕
        client.post(f"/api/v1/capabilities/{capability['id']}/trust")
        tools = client.get(f"/api/v1/capabilities/{capability['id']}/mcp/tools").json()
        assert [item["name"] for item in tools["tools"]] == ["echo", "now"]
        assert tools["cached"] is False

        cached = client.get(f"/api/v1/capabilities/{capability['id']}/mcp/tools").json()
        assert cached["cached"] is True

        called = client.post(
            f"/api/v1/capabilities/{capability['id']}/mcp/call",
            json={"tool": "echo", "arguments": {"text": "hi"}},
        ).json()
        assert called["result"]["ok"] is True
        assert called["result"]["content"] == "echo: hi"

        audit = client.get("/api/v1/capabilities/audit").json()["events"]
        assert any(event.get("event") == "mcp_call" and event.get("manual") for event in audit)


def test_api_rejects_bad_server_config(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        no_command = client.post("/api/v1/capabilities/mcp/servers", json={"transport": "stdio"})
        assert no_command.status_code == 400
        assert no_command.json()["error"]["code"] == "invalid_mcp_server"

        no_url = client.post("/api/v1/capabilities/mcp/servers", json={"transport": "http"})
        assert no_url.status_code == 400

        unknown_preset = client.post("/api/v1/capabilities/mcp/servers", json={"preset_id": "nope"})
        assert unknown_preset.status_code == 400
        assert unknown_preset.json()["error"]["code"] == "mcp_preset_not_found"


def test_model_requested_tool_call_runs_and_feeds_back(tmp_path: Path):
    """模型自主调用：执行段请求工具 → 应用执行（同一条闸门）→ 结果回灌 → 同一步继续。"""

    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        created = client.post(
            "/api/v1/capabilities/mcp/servers", json={"preset_id": "demo", "name": "示例 MCP"}
        ).json()["capability"]
        client.post(f"/api/v1/capabilities/{created['id']}/enable")
        client.post(f"/api/v1/capabilities/{created['id']}/trust")
        client.get(f"/api/v1/capabilities/{created['id']}/mcp/tools")  # 预热工具清单
        # 用真实的能力 ID 让模型去调（服务器 ID 由名字派生，测试里不能写死）
        relay.tool_call = {
            "capability_id": created["id"],
            "tool": "echo",
            "arguments": {"text": "来自模型"},
        }

        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)

        assert run["status"] == "done", run
        step = run["steps"][0]
        assert step["tool_results"], step
        assert step["tool_results"][0]["ok"] is True
        assert step["tool_results"][0]["content"] == "echo: 来自模型"

        # 第二轮请求里带上了工具结果（模型据此继续）
        followups = [
            item
            for item in relay.requests
            if "工具调用结果" in item["body"]["messages"][-1]["content"]
        ]
        assert followups, "应当有一轮把工具结果回灌给执行段"
        assert "echo: 来自模型" in followups[-1]["body"]["messages"][-1]["content"]

        # 工具清单也进了这一步的上下文（模型知道有什么可调）
        prompts = [
            item["body"]["messages"][-1]["content"]
            for item in relay.requests
            if "执行工程师" in item["body"]["messages"][0]["content"]
        ]
        assert any("可用工具（MCP）" in text for text in prompts)

        audit = client.get("/api/v1/capabilities/audit").json()["events"]
        assert any(event.get("event") == "mcp_call" and event.get("run_id") for event in audit)


def test_untrusted_model_call_is_refused_but_step_continues(tmp_path: Path):
    """模型调了没确认信任的服务器：如实拒绝并回灌，不假装执行过。"""

    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        created = client.post(
            "/api/v1/capabilities/mcp/servers", json={"preset_id": "demo", "name": "示例 MCP"}
        ).json()["capability"]
        client.post(f"/api/v1/capabilities/{created['id']}/enable")  # 只启用，不确认信任
        relay.tool_call = {
            "capability_id": created["id"],
            "tool": "echo",
            "arguments": {"text": "x"},
        }

        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)

        step = run["steps"][0]
        assert step["tool_results"], step
        assert step["tool_results"][0]["ok"] is False
        assert "还没确认信任" in step["tool_results"][0]["error"]
        followups = [
            item
            for item in relay.requests
            if "工具调用结果" in item["body"]["messages"][-1]["content"]
        ]
        assert followups
        assert "还没确认信任" in followups[-1]["body"]["messages"][-1]["content"]


def test_mcp_calls_can_be_disabled_by_setting(tmp_path: Path):
    """设置里关掉 MCP 调用：模型请求会被忽略（连工具清单都不下发）。"""

    relay = FakeRelay()
    with build_project_client(tmp_path, relay, mcp_enabled=False) as client:
        created = client.post(
            "/api/v1/capabilities/mcp/servers", json={"preset_id": "demo", "name": "示例 MCP"}
        ).json()["capability"]
        client.post(f"/api/v1/capabilities/{created['id']}/enable")
        client.post(f"/api/v1/capabilities/{created['id']}/trust")
        client.get(f"/api/v1/capabilities/{created['id']}/mcp/tools")
        relay.tool_call = {
            "capability_id": created["id"],
            "tool": "echo",
            "arguments": {"text": "x"},
        }

        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)
        assert run["steps"][0]["tool_results"] == []
        prompts = [
            item["body"]["messages"][-1]["content"]
            for item in relay.requests
            if "执行工程师" in item["body"]["messages"][0]["content"]
        ]
        assert not any("可用工具（MCP）" in text for text in prompts)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
