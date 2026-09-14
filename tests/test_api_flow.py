"""端到端：创建运行 → 架构段出纲领 → 确认 → 执行段落地。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings, SettingsStore
from app.main import create_app
from app.schemas.run import RunStatus
from tests.conftest import FakeRelay

#: 一次执行结束的全部终态（blocked = 需要用户补充信息，同样停止推进）
TERMINAL = {"done", "failed", "cancelled", "blocked"}


def build_client(
    tmp_path: Path,
    relay: FakeRelay,
    *,
    use_store_provider: bool = False,
    settings_overrides: dict | None = None,
) -> TestClient:
    fields = {
        "relay_base_url": "https://relay.test/v1",
        "relay_api_key": "sk-test-1234567890",
        "relay_wire_api": "chat_completions",
        "architect_model": "gpt-5",
        "editor_model": "deepseek-v4",
        "max_plan_steps": 2,
    }
    fields.update(settings_overrides or {})
    settings = Settings(**fields)
    store = SettingsStore(tmp_path / "settings.json")
    app = create_app(
        transport=relay.transport(),
        # 默认用固定配置（便于测试"配置有问题"的场景）；需要反映配置切换时用 store
        settings_provider=store.get if use_store_provider else (lambda: settings),
        runs_dir=tmp_path / "runs",
        web_dir=tmp_path / "no-web",
        # 注入临时配置库，避免测试写坏仓库里的 data/settings.json
        settings_store_override=store,
    )
    return TestClient(app)


def wait_for_status(
    client: TestClient, run_id: str, expected: set[str], timeout: float = 10.0
) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/v1/runs/{run_id}").json()["run"]
        if last["status"] in expected:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"状态未在 {timeout}s 内变为 {expected}，当前 {last.get('status')}，错误 {last.get('error')}"
    )


def test_end_to_end_plan_then_execute(tmp_path: Path):
    relay = FakeRelay(fence_plan=True)
    with build_client(tmp_path, relay) as client:
        created = client.post(
            "/api/v1/runs", json={"task": "建立可验证的项目骨架", "title": "骨架搭建"}
        )
        assert created.status_code == 201
        run_id = created.json()["run"]["id"]

        run = wait_for_status(client, run_id, {"awaiting_approval"})
        assert run["plan"]["goal"]
        assert len(run["steps"]) == 2
        assert run["steps"][0]["status"] == "pending"

        approved = client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        assert approved.json()["action"] == "execute"

        run = wait_for_status(client, run_id, TERMINAL)
        assert run["status"] == "done", run.get("error")
        assert all(step["status"] == "done" for step in run["steps"])

        workspace = tmp_path / "runs" / run_id / "workspace"
        assert (workspace / "steps" / "step-1.md").is_file()
        assert (workspace / "steps" / "step-2.md").is_file()

        # 命令只做建议，默认不执行
        assert run["steps"][0]["commands"][0]["cmd"] == "echo ok"

        docs = {
            doc["name"]: doc["content"]
            for doc in client.get(f"/api/v1/runs/{run_id}/docs").json()["docs"]
        }
        assert set(docs) == {"plan.md", "report.md"}
        assert "纲领" in docs["plan.md"]
        assert "执行报告" in docs["report.md"]

        files = client.get(f"/api/v1/runs/{run_id}/tree").json()["files"]
        assert "steps/step-1.md" in files


def test_replan_with_feedback_then_cancel(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "重构数据层"}).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})

        response = client.post(
            f"/api/v1/runs/{run_id}/approve", json={"feedback": "把第一步拆得更细"}
        )
        assert response.json()["action"] == "replan"
        run = wait_for_status(client, run_id, {"awaiting_approval"})
        assert run["plan_revision"] == 2

        cancelled = client.post(f"/api/v1/runs/{run_id}/cancel")
        assert cancelled.json()["run"]["status"] == "cancelled"


def test_event_bus_replays_history_with_since_filter():
    """SSE 的数据源：新连接补历史，``since`` 之后只收增量。"""
    import asyncio

    from app.services.events import EventBus

    async def scenario() -> None:
        bus = EventBus()
        bus.publish("r1", "status", status="planning")
        first = bus.publish("r1", "plan", plan={"goal": "G"})
        bus.publish("r1", "status", status="awaiting_approval")

        async def collect(since: int | None, limit: int) -> list[dict]:
            received: list[dict] = []
            async for event in bus.subscribe("r1", since=since):
                if event["type"] == "ping":
                    continue
                received.append(event)
                if len(received) >= limit:
                    break
            return received

        replayed = await collect(None, 3)
        assert [event["type"] for event in replayed] == ["status", "plan", "status"]

        incremental = await collect(first["seq"], 1)
        assert [event["type"] for event in incremental] == ["status"]
        assert incremental[0]["data"]["status"] == "awaiting_approval"
        assert bus.current_seq("r1") == 3

    asyncio.run(scenario())


def test_events_endpoint_rejects_unknown_run(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        response = client.get("/api/v1/runs/不存在的运行/events")
        assert response.status_code == 404


def test_settings_are_masked_and_validated(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        payload = client.get("/api/v1/settings").json()
        assert payload["ready"] is True
        assert payload["relay_api_key_masked"].startswith("sk-t")
        assert payload["relay_api_key_masked"].endswith("7890")
        assert "1234567890" not in str(payload)

        bad = client.put("/api/v1/settings", json={"max_plan_steps": 0})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "configuration_error"


def test_api_key_that_is_a_url_is_rejected_with_clear_message(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        response = client.put(
            "/api/v1/settings",
            json={"relay_api_key": "https://api.example.com/v1"},
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["details"]["looks_like_url"] is True
        assert "地址" in error["message"]


def test_api_key_is_normalized_on_save(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        payload = client.put(
            "/api/v1/settings", json={"relay_api_key": '  "Bearer sk-abc123def"  '}
        ).json()
        # 引号与 Bearer 前缀被剥掉，掩码保留首尾可辨识字符
        assert payload["relay_api_key_masked"] == "sk-a******3def"


def test_settings_test_endpoint_reports_connectivity(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        payload = client.post("/api/v1/settings/test").json()
        assert payload["ok"] is True
        for role in ("architect", "editor"):
            result = payload["results"][role]
            assert result["ok"] is True
            assert result["model_count"] == 2
            assert "deepseek-v4" in result["models"]


def test_health_reports_config_problems(tmp_path: Path):
    """Key 栏填成地址时，health 也必须报"未就绪"，两个接口口径一致。"""
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        assert client.get("/api/v1/health").json()["ready"] is True

        client.put("/api/v1/settings", json={"relay_api_key": "https://relay.test/v1"})
        # 保存被拒绝，配置保持原样 → 仍然就绪
        assert client.get("/api/v1/health").json()["ready"] is True


def test_provider_crud_and_switch_via_api(tmp_path: Path):
    """通过接口完成：列出预设 → 新建第二套配置 → 切换 → 测试 → 删除。"""
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        listed = client.get("/api/v1/providers").json()
        assert "official" in listed["presets"]
        assert any(
            item["base_url"].startswith("https://api.openai.com")
            for item in listed["presets"]["official"]
        )

        # 先建一套"中转"并保持为当前配置
        base = client.post(
            "/api/v1/providers",
            json={
                "name": "公司中转",
                "kind": "relay",
                "base_url": "https://relay.test/v1",
                "api_key": "sk-relay-1234",
                "architect_model": "gpt-5",
                "editor_model": "deepseek-v4",
            },
        ).json()
        assert base["active_provider"]["name"] == "公司中转"

        # 再建一套"个人 Key 直连"，activate=false → 不抢走当前配置
        created = client.post(
            "/api/v1/providers",
            json={
                "name": "个人 Key 直连",
                "kind": "official",
                "base_url": "https://relay.test/v1",
                "api_key": "sk-personal-1234",
                "architect_model": "deepseek-chat",
                "editor_model": "deepseek-chat",
                "activate": False,
            },
        )
        assert created.status_code == 201
        provider_id = created.json()["provider_id"]
        payload = created.json()
        assert len(payload["providers"]) == 2
        assert payload["active_provider"]["name"] == "公司中转"

        activated = client.post(f"/api/v1/providers/{provider_id}/activate").json()
        assert activated["active_provider"]["name"] == "个人 Key 直连"
        assert activated["architect"]["model"] == "deepseek-chat"
        assert activated["ready"] is True

        tested = client.post(f"/api/v1/providers/{provider_id}/test").json()
        assert tested["ok"] is True
        assert tested["results"]["architect"]["model"] == "deepseek-chat"

        updated = client.put(
            f"/api/v1/providers/{provider_id}", json={"name": "个人 Key（已改名）"}
        ).json()
        assert updated["active_provider"]["name"] == "个人 Key（已改名）"
        assert updated["active_provider"]["api_key_masked"].startswith("sk-p")

        deleted = client.delete(f"/api/v1/providers/{provider_id}").json()
        assert len(deleted["providers"]) == 1
        assert deleted["active_provider"]["name"] != "个人 Key（已改名）"


def test_provider_with_url_as_key_is_rejected_via_api(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        response = client.post(
            "/api/v1/providers",
            json={
                "name": "写错了",
                "base_url": "https://relay.test/v1",
                "api_key": "https://relay.test/v1",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["details"]["looks_like_url"] is True


def test_architect_and_editor_can_use_different_providers(tmp_path: Path):
    """架构段与执行段各自指定配置：中转↔个人 Key 可以任意组合。"""
    relay = FakeRelay()
    with build_client(tmp_path, relay, use_store_provider=True) as client:
        relay_profile = client.post(
            "/api/v1/providers",
            json={
                "name": "公司中转",
                "kind": "relay",
                "base_url": "https://relay-a.test/v1",
                "api_key": "sk-relay-a-1234",
                "architect_model": "gpt-5.6-sol",
                "editor_model": "deepseek-v4-flash",
            },
        ).json()
        relay_id = relay_profile["provider_id"]

        direct_profile = client.post(
            "/api/v1/providers",
            json={
                "name": "个人 Key 直连",
                "kind": "official",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-direct-b-1234",
                "architect_model": "deepseek-chat",
                "editor_model": "deepseek-chat",
                "activate": False,
            },
        ).json()
        direct_id = direct_profile["provider_id"]

        # 架构走中转，执行走个人 Key
        routed = client.put(
            "/api/v1/routes",
            json={
                "architect": {"provider_id": relay_id, "model": ""},
                "editor": {"provider_id": direct_id, "model": "deepseek-reasoner"},
            },
        ).json()
        assert routed["architect"]["base_url"] == "https://relay-a.test/v1"
        assert routed["architect"]["model"] == "gpt-5.6-sol"  # 留空 → 用该配置的默认模型
        assert routed["editor"]["base_url"] == "https://api.deepseek.com/v1"
        assert routed["editor"]["model"] == "deepseek-reasoner"

        # 换成：架构走个人 Key，执行走中转
        flipped = client.put(
            "/api/v1/routes",
            json={
                "architect": {"provider_id": direct_id},
                "editor": {"provider_id": relay_id},
            },
        ).json()
        assert flipped["architect"]["base_url"] == "https://api.deepseek.com/v1"
        assert flipped["architect"]["model"] == "deepseek-chat"
        assert flipped["editor"]["base_url"] == "https://relay-a.test/v1"
        assert flipped["editor"]["model"] == "deepseek-v4-flash"

        # 侧栏"切换配置"的语义是把两段都切过去 → 清空分段路由
        client.post(f"/api/v1/providers/{relay_id}/activate")
        after = client.get("/api/v1/settings").json()
        assert after["routes"] == {}
        assert after["architect"]["base_url"] == "https://relay-a.test/v1"
        assert after["editor"]["base_url"] == "https://relay-a.test/v1"


def test_routes_reject_unknown_provider(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay, use_store_provider=True) as client:
        client.post(
            "/api/v1/providers",
            json={
                "name": "中转",
                "base_url": "https://relay-a.test/v1",
                "api_key": "sk-relay-a-1234",
            },
        )
        response = client.put("/api/v1/routes", json={"architect": {"provider_id": "p-not-exist"}})
        assert response.status_code == 404


def test_blocked_step_marks_run_blocked(tmp_path: Path, monkeypatch):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "需要外部凭证的任务"}).json()["run"][
            "id"
        ]
        run = wait_for_status(client, run_id, {"awaiting_approval"})

        # 让执行段返回 blocked 结果
        monkeypatch.setattr(
            FakeRelay,
            "_executor_text",
            lambda self, body: (
                '{"summary":"缺少凭证","blocked":true,"block_reason":"需要 API 凭证",'
                '"files":[],"commands":[],"notes":[]}'
            ),
        )
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)
        # 被阻塞 = 需要用户补充信息，可恢复，不再等同于失败
        assert run["status"] == "blocked"
        assert run["steps"][0]["status"] == "blocked"
        assert "凭证" in run["error"]["message"]


def test_executor_can_request_files_on_demand(tmp_path: Path):
    """执行段先索取文件、系统补齐后同一轮继续——不必把整个仓库塞进上下文。"""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("ORIGINAL = 1\n", encoding="utf-8")

    relay = FakeRelay()
    relay.need_files_once = True
    with build_client(tmp_path, relay) as client:
        created = client.post(
            "/api/v1/runs",
            json={"task": "改造现有项目", "target_dir": str(project)},
        ).json()
        run_id = created["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})

        run = wait_for_status(client, run_id, TERMINAL)
        assert run["status"] == "done", run.get("error")
        step = run["steps"][0]
        assert step["fetched_files"] == ["src/app.py"]
        assert step["context_chars"] > 0

        # 第二轮请求确实带上了被索取文件的内容
        followups = [
            item
            for item in relay.requests
            if "## 你索要的文件" in item["body"]["messages"][-1]["content"]
        ]
        assert followups, "执行段第二轮请求应包含索要的文件"
        assert "ORIGINAL = 1" in followups[-1]["body"]["messages"][-1]["content"]


def test_blocked_run_can_resume_with_extra_info(tmp_path: Path):
    """被阻塞 → 补充信息 → 只重跑那一步，不必从头再来。"""
    relay = FakeRelay()
    relay.block_first_executor = True
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "盘点现有任务栏"}).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})

        run = wait_for_status(client, run_id, {"blocked"})
        assert run["steps"][0]["status"] == "blocked"
        assert "工作区为空" in run["steps"][0]["error"]

        resumed = client.post(
            f"/api/v1/runs/{run_id}/resume",
            json={"note": "任务栏尚未实现，请从零新建"},
        ).json()
        assert resumed["action"] == "resume"

        run = wait_for_status(client, run_id, TERMINAL)
        assert run["status"] == "done", run.get("error")
        assert "从零新建" in run["user_notes"][0]
        assert all(step["status"] == "done" for step in run["steps"])

        # 补充说明确实进入了执行段提示词
        assert any("从零新建" in item["body"]["messages"][-1]["content"] for item in relay.requests)


def test_resume_can_attach_existing_directory(tmp_path: Path):
    """事后指定落地目录：从空工作区切到真实项目，继续跑未完成的步骤。"""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "App.tsx").write_text("export const App = () => null;\n", encoding="utf-8")

    relay = FakeRelay()
    relay.block_first_executor = True
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "改造现有任务栏"}).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        wait_for_status(client, run_id, {"blocked"})

        client.post(
            f"/api/v1/runs/{run_id}/resume",
            json={"note": "入口在 src/App.tsx", "target_dir": str(project)},
        )
        run = wait_for_status(client, run_id, TERMINAL)
        assert run["status"] == "done", run.get("error")
        assert run["workspace_dir"] == str(project)

        # 执行段看到了目标目录里的文件内容
        assert any("App.tsx" in item["body"]["messages"][-1]["content"] for item in relay.requests)


def test_resume_rejects_missing_directory(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "改造现有代码"}).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        response = client.post(
            f"/api/v1/runs/{run_id}/resume",
            json={"note": "看这个目录", "target_dir": str(tmp_path / "not-exist")},
        )
        assert response.status_code == 400


def test_architect_context_explains_empty_workspace(tmp_path: Path):
    """需求提到"现有实现"但工作区为空时，架构段必须被告知，避免设计出"盘点现有代码"的步骤。"""
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "盘点现有任务栏"}).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        architect_request = next(
            item
            for item in relay.requests
            if "资深架构师" in item["body"]["messages"][0]["content"]
        )
        context = architect_request["body"]["messages"][1]["content"]
        assert "全新空工作区" in context
        assert "从零新建" in context


def test_brief_reaches_both_segments(tmp_path: Path):
    """把前期沟通喂给编排器：架构段与执行段都要看到同一份简报。"""
    relay = FakeRelay()
    brief = "前情：README 里承诺了 24 项界面自检；已知问题：设置弹窗在小窗口会超出屏幕。"
    with build_client(tmp_path, relay) as client:
        run_id = client.post(
            "/api/v1/runs",
            json={"task": "按简报补完下一阶段", "brief": brief},
        ).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})

        architect_request = next(
            item
            for item in relay.requests
            if "资深架构师" in item["body"]["messages"][0]["content"]
        )
        assert brief in architect_request["body"]["messages"][1]["content"]

        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        wait_for_status(client, run_id, TERMINAL)
        executor_requests = [
            item
            for item in relay.requests
            if "执行工程师" in item["body"]["messages"][0]["content"]
        ]
        assert executor_requests
        assert brief in executor_requests[0]["body"]["messages"][-1]["content"]


def test_step_without_output_is_blocked_not_done(tmp_path: Path):
    """执行段反复索取文件却不产出改动时，不能把这一步标成"已完成"。"""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("ORIGINAL = 1\n", encoding="utf-8")

    relay = FakeRelay()
    relay.always_need_files = True
    with build_client(tmp_path, relay) as client:
        run_id = client.post(
            "/api/v1/runs", json={"task": "改造现有项目", "target_dir": str(project)}
        ).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})

        run = wait_for_status(client, run_id, {"blocked", "failed", "done"})
        assert run["status"] == "blocked", run
        step = run["steps"][0]
        assert step["status"] == "blocked"
        assert "未产出任何改动" in step["error"]
        # 已经补过文件（说明循环确实跑了）
        assert step["fetched_files"] == ["src/app.py"]


def test_retry_step_can_redo_one_step_then_resume(tmp_path: Path):
    """重做某一步（默认只跑这一步），确认后可继续跑完剩下的。"""
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "两步任务"}).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)
        assert run["status"] == "done"
        first_round_calls = relay.executor_calls

        retried = client.post(
            f"/api/v1/runs/{run_id}/steps/1/retry",
            json={"note": "第 1 步没产出，请直接写文件", "stop_after": True},
        ).json()
        assert retried["action"] == "retry_step"

        run = wait_for_status(client, run_id, {"paused", "done", "failed", "blocked"})
        assert run["status"] == "paused", run
        assert run["steps"][0]["status"] == "done"
        # 只重跑了一步（第二步没有再次调用模型）
        assert relay.executor_calls == first_round_calls + 1
        assert "第 1 步没产出，请直接写文件" in run["user_notes"]

        client.post(f"/api/v1/runs/{run_id}/resume", json={"note": ""})
        run = wait_for_status(client, run_id, TERMINAL)
        assert run["status"] == "done"
        assert all(step["status"] == "done" for step in run["steps"])


def test_chat_request_skips_orchestration(tmp_path: Path):
    """「你是哪个模型」这类问答不该生成纲领、不该碰工作区。"""

    relay = FakeRelay()
    relay.intent_kind = "chat"
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "你是哪个模型"}).json()["run"]["id"]
        run = wait_for_status(client, run_id, {"done"})

        assert run["kind"] == "chat"
        assert run["plan"] is None
        assert run["steps"] == []
        answers = [item for item in run["messages"] if item["phase"] == "chat"]
        assert answers and answers[0]["content"].strip()

        # 启发式直接判定，连分流那次模型调用都省了；架构段/执行段一次都没跑
        assert relay.intent_calls == 0
        assert relay.executor_calls == 0
        assert not any(
            "资深架构师" in item["body"]["messages"][0]["content"] for item in relay.requests
        )

        # 没有纲领/报告产物，也没有动过工作区
        assert client.get(f"/api/v1/runs/{run_id}/docs").json()["docs"] == []
        workspace = tmp_path / "runs" / run_id / "workspace"
        assert not workspace.exists() or list(workspace.rglob("*")) == []


def test_step_is_blocked_when_objective_checks_fail(tmp_path: Path):
    """产出了文件但没通过客观验收：不允许标成完成。"""

    relay = FakeRelay()
    relay.plan_checks = [
        {"type": "file_contains", "path": "steps/step-1.md", "text": "这段内容根本不存在"},
        {"type": "py_compile", "path": "steps/step-1.md"},
    ]
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)

        assert run["status"] == "blocked", run
        step = run["steps"][0]
        assert step["status"] == "blocked"
        assert "客观验收未通过" in step["error"]

        results = step["verification"]
        assert results, "应当留下验收明细"
        failed = [item for item in results if not item["ok"]]
        assert {item["type"] for item in failed} == {"file_contains", "py_compile"}
        # 交付物存在这条是通过的：说明检查真的跑了，而不是一律拒绝
        assert any(item["ok"] and item["type"] == "file_exists" for item in results)

        docs = {
            doc["name"]: doc["content"]
            for doc in client.get(f"/api/v1/runs/{run_id}/docs").json()["docs"]
        }
        assert "客观验收" in docs["report.md"]


def test_verification_passes_are_recorded_not_just_failures(tmp_path: Path):
    """通过也要留痕：否则界面上无法区分「验收通过」和「压根没验」。"""

    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)

        assert run["status"] == "done", run.get("error")
        for step in run["steps"]:
            assert step["verification"], step
            assert all(item["ok"] for item in step["verification"])


def test_failing_verification_command_is_fed_back_and_fixed(tmp_path: Path):
    """自开发闭环：跑命令失败 → 报错回灌 → 执行段继续修 → 复验通过。"""

    relay = FakeRelay()
    relay.command_flow = "fix-after-failure"
    with build_client(
        tmp_path,
        relay,
        settings_overrides={
            "allow_command_execution": True,
            "command_allowlist": [sys.executable],
            "step_command_rounds": 2,
        },
    ) as client:
        run_id = client.post("/api/v1/runs", json={"task": "建立骨架并跑通验证"}).json()["run"][
            "id"
        ]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)

        assert run["status"] == "done", run.get("error")
        step = run["steps"][0]
        results = step["command_results"]
        # 第一次失败、修正后第二次通过——两次都留在记录里
        assert [item["ok"] for item in results] == [False, True]
        assert results[0]["exit_code"] not in (0, None)
        assert step["status"] == "done"

        # 关键：失败输出真的回到了执行段的上下文里（第二轮请求里能看到）
        followups = [
            item
            for item in relay.requests
            if "系统已经执行过这些命令" in item["body"]["messages"][-1]["content"]
        ]
        assert followups, "应当有第二轮请求带着失败输出"
        assert "__no_such_tests_dir__" in followups[-1]["body"]["messages"][-1]["content"]


def test_command_failure_after_rounds_blocks_the_step(tmp_path: Path):
    """修不回来就不许标完成：轮次用尽后该步 blocked，报错摊开。"""

    relay = FakeRelay()
    relay.command_flow = "always-fail"
    with build_client(
        tmp_path,
        relay,
        settings_overrides={
            "allow_command_execution": True,
            "command_allowlist": [sys.executable],
            "step_command_rounds": 1,
        },
    ) as client:
        run_id = client.post("/api/v1/runs", json={"task": "建立骨架并跑通验证"}).json()["run"][
            "id"
        ]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)

        assert run["status"] == "blocked", run
        step = run["steps"][0]
        assert step["status"] == "blocked"
        assert "验证命令未通过" in step["error"]
        assert len(step["command_results"]) == 2  # 首轮 + 一轮修正


def test_non_whitelisted_command_stays_a_suggestion(tmp_path: Path):
    """白名单外的命令永不执行——只是建议，步骤照样能完成。"""

    relay = FakeRelay()
    relay.command_flow = "always-fail"
    with build_client(
        tmp_path,
        relay,
        settings_overrides={
            "allow_command_execution": True,
            "command_allowlist": ["some-other-tool"],
        },
    ) as client:
        run_id = client.post("/api/v1/runs", json={"task": "建立骨架"}).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)

        assert run["status"] == "done", run.get("error")
        step = run["steps"][0]
        assert step["command_results"], step
        assert all(item["skipped"] for item in step["command_results"])
        assert all(item["exit_code"] is None for item in step["command_results"])


def _init_project_repo(path: Path) -> Path:
    """一个有初始提交的真实项目目录（模拟"让它改自己的仓库"）。"""

    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "测试"),
        ("config", "user.email", "test@example.com"),
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# 示例项目\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


def test_step_can_be_reverted_with_git(tmp_path: Path):
    """自开发的安全网：改错了能一键把这一步的改动还原回去。"""

    project = _init_project_repo(tmp_path / "project")
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post(
            "/api/v1/runs", json={"task": "改一下这个项目", "target_dir": str(project)}
        ).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for_status(client, run_id, TERMINAL)

        assert run["status"] == "done", run.get("error")
        created = project / "steps" / "step-1.md"
        assert created.is_file()
        assert run["steps"][0]["git_snapshot"]["head"], "应当记录步骤级 git 锚点"

        reverted = client.post(f"/api/v1/runs/{run_id}/steps/1/revert").json()
        assert reverted["action"] == "revert_step"
        assert "steps/step-1.md" in reverted["reverted"]["removed"]
        assert not created.exists()
        assert reverted["reverted"]["errors"] == []

        step = reverted["run"]["steps"][0]
        assert step["status"] == "pending"
        assert step["files"] == []
        assert reverted["run"]["status"] == "paused"


def test_revert_is_refused_while_running(tmp_path: Path):
    """运行中不许回滚：否则刚写下的文件会和正在跑的步骤打架。"""

    project = _init_project_repo(tmp_path / "project")
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post(
            "/api/v1/runs", json={"task": "改一下这个项目", "target_dir": str(project)}
        ).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})

        # 人为把它标成"执行中"：真实场景就是某一步正在跑的时候
        orchestrator = client.app.state.orchestrator
        running = orchestrator.store.load(run_id)
        running.status = RunStatus.EXECUTING
        orchestrator.store.save(running)

        response = client.post(f"/api/v1/runs/{run_id}/steps/1/revert")
        assert response.status_code == 409
