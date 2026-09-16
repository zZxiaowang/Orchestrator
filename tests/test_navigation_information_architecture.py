"""第 3 步验收：左侧栏与页面边界（两级导航）。

对齐文档：``docs/project-navigation-contract.md``、``docs/project-context-contract.md``。

覆盖：

* 左侧栏一级区域只有「普通对话」与「项目」；
* 架构 / 计划 / 执行 / 步骤 / 验证 / 日志 / 设置不再是一级入口，只作为项目二级模块；
* 普通对话工作区不显示也不返回项目执行控制；
* 项目二级模块必须带 project_id，刷新后能据 URL 恢复项目与模块；
* 没有选中项目时返回选择/创建引导，而不是全局运行数据。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import app.desktop as desktop
from app.api import routes as api_routes
from app.schemas.navigation import PROJECT_MODULES, PROJECT_ONLY_ACTIONS, ProjectModule
from app.schemas.project import project_id_from_context_id
from tests.conftest import build_project_client

ROOT = Path(__file__).resolve().parents[1]
DESKTOP_SOURCE = (ROOT / "app" / "desktop.py").read_text(encoding="utf-8")
ROUTES_SOURCE = (ROOT / "app" / "api" / "routes.py").read_text(encoding="utf-8")

PRIMARY_IDS = ["chat", "projects"]
PRIMARY_LABELS = ["普通对话", "项目"]
ENGINEERING_LABELS = ["架构", "计划", "执行", "步骤", "验证", "日志", "设置"]


def run(coro):
    return asyncio.run(coro)


# --- 后端导航契约 -----------------------------------------------------------


def test_sidebar_primary_entries_are_only_chat_and_projects():
    payload = run(api_routes.navigation_sidebar())
    assert [item["id"] for item in payload["primary"]] == PRIMARY_IDS
    assert [item["label"] for item in payload["primary"]] == PRIMARY_LABELS
    assert payload["default_route"] == "#/projects"
    assert payload["default_context_type"] == "project"


def test_engineering_concepts_are_not_primary_entries():
    payload = run(api_routes.navigation_sidebar())
    primary_ids = {item["id"] for item in payload["primary"]}
    assert primary_ids.isdisjoint(PROJECT_ONLY_ACTIONS)
    labels = "".join(item["label"] for item in payload["primary"])
    for label in ENGINEERING_LABELS:
        assert label not in labels


def test_engineering_concepts_live_under_projects_only():
    payload = run(api_routes.navigation_sidebar())
    modules = payload["project_modules"]
    assert [item["id"] for item in modules] == [module.value for module in PROJECT_MODULES]
    assert all(item["visible_without_project"] is False for item in modules)
    assert payload["project_only_actions"] == sorted(PROJECT_ONLY_ACTIONS)
    assert PROJECT_ONLY_ACTIONS


def test_chat_workspace_has_no_project_controls():
    payload = run(api_routes.navigation_chat_workspace())
    assert payload["route"] == "#/chat"
    assert payload["context_type"] == "chat"
    assert payload["shows_project_controls"] is False
    assert payload["shows_execution_controls"] is False


def test_project_module_requires_project_id(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        resp = client.get("/api/v1/projects/%20%20/modules/execution")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_project_context"


def test_project_module_restores_project_id_and_module_from_url(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        created = client.post("/api/v1/projects", json={"name": "Alpha", "project_id": "alpha"})
        assert created.status_code == 201

        resp = client.get("/api/v1/projects/alpha/modules/execution")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["project_id"] == "alpha"
        assert payload["module"] == ProjectModule.EXECUTION.value
        assert payload["context_id"] == "project:alpha"
        assert project_id_from_context_id(payload["context_id"]) == "alpha"
        assert payload["route"].startswith("#/")
        assert "alpha" in payload["route"]
        # 真实数据：模块带着本项目的运行列表与装配结果，而不是只有路由元信息
        assert "execution" in payload and payload["execution"]["has_run"] is False
        assert payload["counts"]["runs"] == 0
        assert payload["project"]["name"] == "Alpha"


def test_unknown_project_module_is_rejected(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        resp = client.get("/api/v1/projects/alpha/modules/not-a-module")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "project_module_not_found"
        assert body["error"]["details"]["allowed"] == [module.value for module in PROJECT_MODULES]


def test_project_module_without_project_returns_guidance_not_run_data():
    payload = run(api_routes.project_module_entry("architecture"))
    assert payload["requires_project"] is True
    assert payload["selected_project_id"] is None
    assert payload["module"] == ProjectModule.ARCHITECTURE.value
    assert payload["empty_state"]["actions"]
    assert "runs" not in payload and "steps" not in payload
    for action in payload["empty_state"]["actions"]:
        assert action["route"].startswith("#/projects")


def test_unknown_project_module_entry_is_rejected():
    resp = run(api_routes.project_module_entry("not-a-module"))
    assert resp.status_code == 404


def test_router_exposes_navigation_endpoints():
    paths = {route.path for route in api_routes.router.routes}
    assert "/api/v1/navigation/sidebar" in paths
    assert "/api/v1/navigation/chat-workspace" in paths
    assert "/api/v1/navigation/project-modules/{module_id}" in paths
    assert "/api/v1/projects/{project_id}/modules/{module_id}" in paths


# --- 桌面端左侧栏 -----------------------------------------------------------


def test_desktop_sidebar_only_has_two_primary_entries():
    assert [item["id"] for item in desktop.SIDEBAR_PRIMARY_ENTRIES] == PRIMARY_IDS
    assert [item["label"] for item in desktop.SIDEBAR_PRIMARY_ENTRIES] == PRIMARY_LABELS
    labels = [item["label"] for item in desktop.SIDEBAR_PRIMARY_ENTRIES]
    for label in ENGINEERING_LABELS:
        assert label not in labels


def test_desktop_project_modules_are_secondary_only():
    ids = [item["id"] for item in desktop.PROJECT_SECONDARY_MODULES]
    assert ids == [module.value for module in ProjectModule]
    assert set(ids).isdisjoint({item["id"] for item in desktop.SIDEBAR_PRIMARY_ENTRIES})
    assert tuple(ids) == desktop.PROJECT_ONLY_SECTIONS


def test_desktop_sidebar_js_uses_single_source_of_truth():
    js = desktop.SIDEBAR_NAVIGATION_JS
    assert json.dumps(list(desktop.SIDEBAR_PRIMARY_ENTRIES), ensure_ascii=False) in js
    assert json.dumps(list(desktop.PROJECT_SECONDARY_MODULES), ensure_ascii=False) in js
    assert "data-nav-primary" in js
    assert "data-project-modules" in js
    assert "moduleHost.hidden" in js


def test_desktop_helper_exposes_the_same_contract():
    assert desktop.sidebar_primary_entries() == list(desktop.SIDEBAR_PRIMARY_ENTRIES)
    assert desktop.project_secondary_modules() == list(desktop.PROJECT_SECONDARY_MODULES)


def test_desktop_navigation_contract_is_visible_in_source():
    assert "SIDEBAR_PRIMARY_ENTRIES" in DESKTOP_SOURCE
    assert "SIDEBAR_NAVIGATION_JS" in DESKTOP_SOURCE
    assert "普通对话" in DESKTOP_SOURCE
    assert "项目" in DESKTOP_SOURCE


def test_routes_source_exposes_primary_navigation_contract():
    assert "navigation_sidebar" in ROUTES_SOURCE
    assert "/navigation/sidebar" in ROUTES_SOURCE
    assert "PRIMARY_NAVIGATION_LABELS" in ROUTES_SOURCE
