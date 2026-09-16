"""普通对话是**真实会话**：新建 / 多轮（带历史）/ 列表 / 删除，且不碰项目与工作区。

回归来源：上一轮只落了界面壳子——「＋ 新对话」只改前端上下文，对话列表读的是
localStorage 里一个从来没人写入的数组。这里逐条钉住后端行为。
"""

from __future__ import annotations

import time
from pathlib import Path

from tests.conftest import FakeRelay, build_project_client


def wait_for_messages(client, chat_id: str, count: int, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/v1/chats/{chat_id}").json()["chat"]
        if len(last["messages_list"]) >= count:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"消息数未在 {timeout}s 内达到 {count}，当前 {len(last.get('messages_list', []))}"
    )


def test_chat_session_roundtrip_with_history(tmp_path: Path):
    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        created = client.post("/api/v1/chats", json={"message": "你是哪个模型"})
        assert created.status_code == 201
        chat_id = created.json()["chat"]["id"]

        detail = wait_for_messages(client, chat_id, 2)
        assert [item["role"] for item in detail["messages_list"]] == ["user", "assistant"]
        assert detail["messages_list"][1]["content"].strip()
        assert detail["title"]  # 标题按第一条消息生成
        assert detail["context_id"] == f"chat:{chat_id}"

        # 第二轮：必须带上历史，否则"接着说"就断了
        second = client.post(f"/api/v1/chats/{chat_id}/messages", json={"text": "再展开讲讲"})
        assert second.status_code == 202
        detail = wait_for_messages(client, chat_id, 4)
        assert [item["role"] for item in detail["messages_list"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        followups = [item for item in relay.requests if len(item["body"]["messages"]) >= 4]
        assert followups, "第二轮请求应当带上历史轮次"
        assert "再展开讲讲" in followups[-1]["body"]["messages"][-1]["content"]
        assert "你是哪个模型" in followups[-1]["body"]["messages"][-2]["content"]

        listed = client.get("/api/v1/chats").json()["chats"]
        assert [item["id"] for item in listed] == [chat_id]
        assert listed[0]["messages"] == 4
        assert listed[0]["preview"]

        assert client.delete(f"/api/v1/chats/{chat_id}").json()["deleted"] == chat_id
        assert client.get("/api/v1/chats").json()["chats"] == []
        assert client.get(f"/api/v1/chats/{chat_id}").status_code == 404


def test_empty_chat_can_be_created_without_a_message(tmp_path: Path):
    """「＋ 新对话」先建一个空会话，用户再自己说第一句。"""

    with build_project_client(tmp_path) as client:
        created = client.post("/api/v1/chats", json={}).json()["chat"]
        assert created["title"] == "新对话"
        assert created["messages_list"] == []
        assert created["context_type"] == "chat"
        assert [item["id"] for item in client.get("/api/v1/chats").json()["chats"]] == [
            created["id"]
        ]


def test_chat_never_touches_projects_or_workspace(tmp_path: Path):
    """普通对话不产生纲领与步骤、不建工作区，也不出现在任何项目的运行列表里。"""

    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        chat = client.post("/api/v1/chats", json={"message": "你好"}).json()["chat"]
        wait_for_messages(client, chat["id"], 2)

        detail = client.get(f"/api/v1/chats/{chat['id']}").json()["chat"]
        assert "plan" not in detail and "steps" not in detail
        assert client.get("/api/v1/runs", params={"project_id": "default"}).json()["runs"] == []
        assert not (tmp_path / "runs" / chat["id"] / "workspace").exists()

        # 项目内动作一律拒绝：普通对话不进执行框架
        approve = client.post(f"/api/v1/runs/{chat['id']}/approve", json={"feedback": ""})
        assert approve.status_code == 400
        assert approve.json()["error"]["code"] == "not_a_project_run"
        assert (
            client.post(
                f"/api/v1/runs/{chat['id']}/continue", json={"instruction": "接着做"}
            ).status_code
            == 400
        )
        assert client.post(f"/api/v1/runs/{chat['id']}", json={}).status_code in (404, 405)


def test_project_run_cannot_be_used_as_a_chat(tmp_path: Path):
    """反向同样不允许：项目运行不是普通对话。"""

    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "建立骨架"}).json()["run"]["id"]

        assert client.get(f"/api/v1/chats/{run_id}").status_code == 400
        assert (
            client.post(f"/api/v1/chats/{run_id}/messages", json={"text": "你好"}).status_code
            == 400
        )
        assert client.delete(f"/api/v1/chats/{run_id}").status_code == 400
        # 它仍然在项目运行列表里，说明只是"不能当对话用"
        assert [
            item["id"]
            for item in client.get("/api/v1/runs", params={"project_id": "default"}).json()["runs"]
        ] == [run_id]


def test_chats_are_listed_separately_from_project_runs(tmp_path: Path):
    """同一条存储里两种上下文互不串：chat 进对话列表，task 进项目运行列表。"""

    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        client.post("/api/v1/chats", json={"message": "随便聊聊"})
        run_id = client.post("/api/v1/runs", json={"task": "建立骨架"}).json()["run"]["id"]

        chats = client.get("/api/v1/chats").json()["chats"]
        runs = client.get("/api/v1/runs").json()["runs"]
        assert len(chats) == 1
        assert [item["id"] for item in runs] == [run_id]
        assert chats[0]["id"] != run_id
