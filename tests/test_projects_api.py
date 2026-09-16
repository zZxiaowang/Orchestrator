"""项目是真实容器：新建 / 列表 / 改名换目录 / 归档，运行归属与模块数据。

回归来源：上一轮只落了导航契约与界面壳子——左侧栏的"项目"其实是从运行列表里临时拼出来的
候选，既不能新建也不能绑定工作区。这里逐条钉住真实行为。
"""

from __future__ import annotations

import time
from pathlib import Path

from tests.conftest import FakeRelay, build_project_client

TERMINAL = {"done", "failed", "cancelled", "blocked"}


def wait_for_status(client, run_id: str, expected: set[str], timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/v1/runs/{run_id}").json()["run"]
        if last["status"] in expected:
            return last
        time.sleep(0.05)
    raise AssertionError(f"状态未在 {timeout}s 内变为 {expected}，当前 {last.get('status')}")


def test_default_project_always_exists(tmp_path: Path):
    """历史运行（没有 project_id）必须有明确归属，所以默认项目始终在。"""

    with build_project_client(tmp_path) as client:
        payload = client.get("/api/v1/projects").json()
        assert [item["project_id"] for item in payload["projects"]] == ["default"]
        assert payload["default_project_id"] == "default"
        default = payload["projects"][0]
        assert default["name"] == "默认项目"
        # 默认项目是"历史数据容器"：不绑定工作区，避免改变"没指定项目时新建运行工作区"的既有行为
        assert default["workspace"] is None
        assert default["runs"] == 0


def test_create_project_binds_a_real_workspace(tmp_path: Path):
    workspace = tmp_path / "code" / "demo"
    with build_project_client(tmp_path) as client:
        created = client.post(
            "/api/v1/projects",
            json={
                "name": "演示项目",
                "project_id": "demo",
                "root_path": str(workspace),
                "description": "自检用",
            },
        )
        assert created.status_code == 201
        project = created.json()["project"]
        assert project["project_id"] == "demo"
        assert project["context_id"] == "project:demo"
        assert project["description"] == "自检用"
        # 工作区真的建出来了，且被绑定到这个项目上
        assert workspace.is_dir()
        assert project["workspace"]["root_path"] == str(workspace.resolve())
        assert project["workspace"]["workspace_id"] == "demo"

        listed = client.get("/api/v1/projects").json()["projects"]
        assert [item["project_id"] for item in listed] == ["demo", "default"]


def test_project_id_and_workspace_are_validated(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        client.post("/api/v1/projects", json={"name": "A", "project_id": "alpha"})
        duplicate = client.post("/api/v1/projects", json={"name": "B", "project_id": "alpha"})
        assert duplicate.status_code == 400
        assert duplicate.json()["error"]["code"] == "project_exists"

        bad_id = client.post("/api/v1/projects", json={"name": "C", "project_id": "有中文"})
        assert bad_id.status_code == 400
        assert bad_id.json()["error"]["code"] == "invalid_project_id"

        root_as_workspace = client.post(
            "/api/v1/projects",
            json={"name": "D", "project_id": "dd", "root_path": str(tmp_path.anchor)},
        )
        assert root_as_workspace.status_code == 400
        assert root_as_workspace.json()["error"]["code"] == "invalid_project_workspace"


def test_project_can_be_renamed_rebound_and_archived(tmp_path: Path):
    first = tmp_path / "ws-one"
    second = tmp_path / "ws-two"
    with build_project_client(tmp_path) as client:
        client.post(
            "/api/v1/projects",
            json={"name": "旧名字", "project_id": "demo", "root_path": str(first)},
        )
        updated = client.put(
            "/api/v1/projects/demo",
            json={"name": "新名字", "root_path": str(second)},
        ).json()["project"]
        assert updated["name"] == "新名字"
        assert updated["workspace"]["root_path"] == str(second.resolve())
        assert second.is_dir()

        archived = client.delete("/api/v1/projects/demo").json()
        assert archived["archived"] is True
        assert archived["project"]["status"] == "archived"
        assert [
            item["project_id"] for item in client.get("/api/v1/projects").json()["projects"]
        ] == ["default"]
        all_projects = client.get("/api/v1/projects", params={"include_archived": True}).json()
        assert "demo" in [item["project_id"] for item in all_projects["projects"]]
        # 归档只是从列表里收起来：数据还在，直接按 ID 仍然打得开（可恢复）
        module = client.get("/api/v1/projects/demo/modules/overview")
        assert module.status_code == 200
        assert module.json()["project"]["status"] == "archived"
        # 删掉的项目才真的查不到
        deleted = client.put("/api/v1/projects/demo", json={"status": "deleted"}).json()
        assert deleted["project"]["status"] == "deleted"
        assert client.get("/api/v1/projects/demo/modules/overview").status_code == 404


def test_run_belongs_to_project_and_lists_are_isolated(tmp_path: Path):
    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        client.post("/api/v1/projects", json={"name": "Demo", "project_id": "demo"})
        created = client.post(
            "/api/v1/runs", json={"task": "为示例项目建立骨架", "project_id": "demo"}
        ).json()["run"]
        run_id = created["id"]

        assert created["project_id"] == "demo"
        assert created["context_type"] == "project"
        assert created["context_id"] == "project:demo"
        # 项目与工作区一一绑定：没给 target_dir 时运行落在项目工作区里
        expected_root = (tmp_path / "projects" / "demo" / "workspace").resolve()
        assert Path(created["workspace_dir"]) == expected_root

        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        wait_for_status(client, run_id, TERMINAL)

        mine = client.get("/api/v1/runs", params={"project_id": "demo"}).json()["runs"]
        assert [item["id"] for item in mine] == [run_id]
        assert all(item["project_id"] == "demo" for item in mine)

        # 别的项目看不到它（运行列表按项目边界过滤，不再有全局聚合）
        others = client.get("/api/v1/runs", params={"project_id": "default"}).json()["runs"]
        assert others == []
        # 默认项目卡片也不该把它算进去
        default_card = next(
            item
            for item in client.get("/api/v1/projects").json()["projects"]
            if item["project_id"] == "default"
        )
        assert default_card["runs"] == 0


def test_run_for_unknown_project_is_rejected(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        resp = client.post("/api/v1/runs", json={"task": "随便做点什么", "project_id": "not-exist"})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "project_not_found"


def test_project_modules_expose_real_data(tmp_path: Path):
    """八个模块都有真实数据：概览计数、架构原文、计划步骤、执行步骤、验证、日志。"""

    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        client.post("/api/v1/projects", json={"name": "Demo", "project_id": "demo"})
        run_id = client.post(
            "/api/v1/runs", json={"task": "为示例项目建立骨架", "project_id": "demo"}
        ).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        wait_for_status(client, run_id, TERMINAL)

        def module(name: str) -> dict:
            response = client.get(f"/api/v1/projects/demo/modules/{name}")
            assert response.status_code == 200, response.text
            return response.json()

        overview = module("overview")
        assert overview["is_empty_state"] is False
        assert overview["counts"]["runs"] == 1
        assert overview["counts"]["steps_done"] >= 1
        assert overview["current_run"]["id"] == run_id
        # 概览的路由就是项目根（契约：`#/projects/<id>` 即概览）
        assert overview["route"] == "#/projects/demo"

        architecture = module("architecture")
        assert architecture["architecture"]["has_plan"] is True
        assert architecture["architecture"]["goal"]
        assert architecture["architecture"]["raw"]
        assert architecture["models"]["architect"]["model"] == "gpt-5"

        plan = module("plan")
        assert [item["id"] for item in plan["plan"]["steps"]] == [1, 2]
        assert all(item["status"] == "done" for item in plan["plan"]["steps"])

        execution = module("execution")
        assert execution["execution"]["has_run"] is True
        assert len(execution["execution"]["steps"]) == 2
        assert execution["execution"]["steps"][0]["files"]

        verification = module("verification")
        assert verification["verification"]["totals"]["failed"] == 0
        assert verification["verification"]["steps"]

        # 旧模块名按别名落到契约模块上（历史链接不 404）
        assert module("steps")["module"] == "execution"
        assert module("verify")["module"] == "verification"

        logs = module("logs")
        assert logs["logs"]["has_run"] is True
        assert any(item["phase"] == "executor" for item in logs["logs"]["messages"])

        settings = module("settings")
        assert settings["settings"]["editable"]
        assert settings["settings"]["project"]["workspace"]["root_path"]

        assert module("architecture")["project"]["name"] == "Demo"


def test_module_data_does_not_leak_across_projects(tmp_path: Path):
    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        client.post("/api/v1/projects", json={"name": "A", "project_id": "alpha"})
        client.post("/api/v1/projects", json={"name": "B", "project_id": "beta"})
        client.post("/api/v1/runs", json={"task": "为 alpha 建骨架", "project_id": "alpha"})

        beta_overview = client.get("/api/v1/projects/beta/modules/overview").json()
        assert beta_overview["counts"]["runs"] == 0
        assert beta_overview["is_empty_state"] is True
        assert beta_overview["runs"] == []
