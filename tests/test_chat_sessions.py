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


# ── 长上下文管理：滚动窗口 + 累积摘要 + 超阈值自动开新会话 ──


def test_long_chat_keeps_each_turn_bounded(tmp_path: Path):
    """聊得越久不该越贵：每轮发送的历史轮次被窗口限制住（不再线性增长）。"""

    relay = FakeRelay()
    with build_project_client(
        tmp_path,
        relay,
        chat_window_turns=2,
        chat_window_chars=4000,
        chat_fold_batch=2,
        chat_summary_max_chars=100000,  # 本用例只验窗口，不让它开新会话
    ) as client:
        chat = client.post("/api/v1/chats", json={"message": "第 0 句"}).json()["chat"]
        chat_id = chat["id"]
        wait_for_messages(client, chat_id, 2)
        for index in range(1, 7):
            client.post(f"/api/v1/chats/{chat_id}/messages", json={"text": f"第 {index} 句"})
            wait_for_messages(client, chat_id, 2 * (index + 1))

        detail = client.get(f"/api/v1/chats/{chat_id}").json()["chat"]
        assert detail["messages"] == 14  # 会话自己完整留着
        assert detail["folded_turns"] >= 8  # 但发出去的只有窗口内的
        assert detail["summary_chars"] > 0
        assert detail["last_context_chars"] > 0

        chat_calls = [
            item
            for item in relay.requests
            if "本地桌面工具" in item["body"]["messages"][0]["content"]
        ]
        last = chat_calls[-1]["body"]["messages"]
        # system + 摘要 + 窗口 2 轮（4 条）+ 当前提问
        assert len(last) <= 7, [m["role"] for m in last]
        assert relay.summary_calls >= 1


def test_summary_exceeding_limit_opens_a_followup_session(tmp_path: Path):
    """摘要撑不住时自动开新对话承接；旧对话留着，并留一条跳转说明。"""

    relay = FakeRelay()
    with build_project_client(
        tmp_path,
        relay,
        chat_window_turns=1,
        chat_window_chars=1000,
        chat_fold_batch=1,
        chat_summary_max_chars=200,  # 下限就是 200；摘要一生成就超限，下一轮必然触发拆分
    ) as client:
        chat_id = client.post("/api/v1/chats", json={"message": "第一句"}).json()["chat"]["id"]
        wait_for_messages(client, chat_id, 2)
        for index in (2, 3):
            client.post(f"/api/v1/chats/{chat_id}/messages", json={"text": f"第 {index} 句"})
            wait_for_messages(client, chat_id, 2 * index)
        folded = client.get(f"/api/v1/chats/{chat_id}").json()["chat"]
        assert folded["folded_turns"] >= 2
        assert folded["summary_chars"] > 200

        sent = client.post(f"/api/v1/chats/{chat_id}/messages", json={"text": "第四句"})
        assert sent.status_code == 202
        payload = sent.json()
        assert "split_from" in payload and payload["split_reason"]
        followup = payload["chat"]
        assert followup["id"] != chat_id
        assert followup["prev_session_id"] == chat_id
        assert followup["summary_chars"] > 0

        # 旧会话：留着、能点回去、留了一条说明
        previous = client.get(f"/api/v1/chats/{chat_id}").json()["chat"]
        assert previous["next_session_id"] == followup["id"]
        assert any(item["phase"] == "chat-split" for item in previous["messages_list"])
        assert previous["messages_list"][-1]["role"] == "system"

        # 新会话：接着答，并带着承接摘要
        detail = wait_for_messages(client, followup["id"], 2)
        assert [item["role"] for item in detail["messages_list"]] == ["user", "assistant"]
        assert detail["summary"]
        assert detail["last_context_chars"] > 0

        ids = [item["id"] for item in client.get("/api/v1/chats").json()["chats"]]
        assert chat_id in ids and followup["id"] in ids


def test_auto_split_can_be_turned_off(tmp_path: Path):
    """关掉"自动开新对话"后只折叠、不切换会话（保守模式）。"""

    relay = FakeRelay()
    with build_project_client(
        tmp_path,
        relay,
        chat_window_turns=1,
        chat_window_chars=1000,
        chat_fold_batch=1,
        chat_summary_max_chars=200,
        chat_auto_split=False,
    ) as client:
        chat_id = client.post("/api/v1/chats", json={"message": "第一句"}).json()["chat"]["id"]
        wait_for_messages(client, chat_id, 2)
        for index in (2, 3, 4):
            sent = client.post(f"/api/v1/chats/{chat_id}/messages", json={"text": f"第 {index} 句"})
            assert "split_from" not in sent.json()
            wait_for_messages(client, chat_id, 2 * index)
        detail = client.get(f"/api/v1/chats/{chat_id}").json()["chat"]
        assert detail["next_session_id"] == ""
        assert detail["folded_turns"] >= 2


def test_disabled_context_management_sends_full_history(tmp_path: Path):
    """总开关关掉 = 回到旧行为：历史原样发，不折叠、不拆分。"""

    relay = FakeRelay()
    with build_project_client(
        tmp_path,
        relay,
        chat_context_enabled=False,
        chat_window_turns=1,
        chat_fold_batch=1,
    ) as client:
        chat_id = client.post("/api/v1/chats", json={"message": "第一句"}).json()["chat"]["id"]
        wait_for_messages(client, chat_id, 2)
        for index in (2, 3):
            client.post(f"/api/v1/chats/{chat_id}/messages", json={"text": f"第 {index} 句"})
            wait_for_messages(client, chat_id, 2 * index)

        detail = client.get(f"/api/v1/chats/{chat_id}").json()["chat"]
        assert detail["folded_turns"] == 0
        assert relay.summary_calls == 0
        chat_calls = [
            item
            for item in relay.requests
            if "本地桌面工具" in item["body"]["messages"][0]["content"]
        ]
        # 最后一轮仍然是全量历史（1 system + 4 条历史 + 当前提问）
        assert len(chat_calls[-1]["body"]["messages"]) == 6
