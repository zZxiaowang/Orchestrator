"""统一能力层：skill / MCP / 插件共用一套安装、启用、作用域与审计。

这一层是 P1（Skills）与 P2（MCP）的地基，所以行为要钉死：
三种形态共用一份存储、启用状态与作用域可查、审计留痕、旧插件只镜像不越权。
"""

from __future__ import annotations

from pathlib import Path

from app.capabilities.registry import CapabilityRegistry
from app.schemas.capability import Capability, CapabilityKind, CapabilityScope
from tests.conftest import build_project_client


def _registry(tmp_path: Path) -> CapabilityRegistry:
    return CapabilityRegistry(tmp_path / "capabilities")


def _skill(capability_id: str = "demo-skill", **overrides) -> Capability:
    data = {
        "id": capability_id,
        "kind": CapabilityKind.SKILL,
        "name": "演示 Skill",
        "description": "把一段流程写成指令包",
        "permissions": ["instructions"],
        "meta": {"entry": "SKILL.md"},
    }
    data.update(overrides)
    return Capability(**data)


def test_registry_stores_all_three_kinds(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.upsert(_skill())
    registry.upsert(
        Capability(
            id="demo-mcp", kind=CapabilityKind.MCP, name="filesystem", meta={"transport": "stdio"}
        )
    )
    registry.upsert(Capability(id="plugin.legacy", kind=CapabilityKind.PLUGIN, name="旧插件"))

    # 顺序按形态固定：skill → mcp → plugin（与能力中心分页一致）
    assert [item.id for item in registry.list()] == ["demo-skill", "demo-mcp", "plugin.legacy"]
    assert [item.id for item in registry.list(kind="skill")] == ["demo-skill"]
    assert registry.get("demo-mcp").meta["transport"] == "stdio"  # type: ignore[union-attr]
    # 重启后还在（真的落盘）
    assert [item.id for item in _registry(tmp_path).list()] == [
        "demo-skill",
        "demo-mcp",
        "plugin.legacy",
    ]


def test_enable_disable_and_scope(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.upsert(_skill("global-skill"))
    registry.upsert(_skill("project-skill", scope=CapabilityScope.PROJECT, project_id="alpha"))
    assert {item.id for item in registry.active_for("alpha")} == {"global-skill", "project-skill"}
    assert {item.id for item in registry.active_for("beta")} == {"global-skill"}

    registry.set_enabled("global-skill", False)
    assert [item.id for item in registry.active_for("alpha")] == ["project-skill"]
    assert all(item.enabled for item in registry.list(include_disabled=False))


def test_touch_and_audit_are_recorded(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.upsert(_skill("demo-skill"))
    registry.set_enabled("demo-skill", False)
    registry.touch("demo-skill")
    events = [event["event"] for event in registry.read_audit(limit=20)]
    assert events[0] == "install"
    assert "disable" in events
    assert registry.get("demo-skill").last_used_at is not None  # type: ignore[union-attr]


def test_unknown_capability_is_rejected(tmp_path: Path):
    registry = _registry(tmp_path)
    for action in (
        lambda: registry.require("nope"),
        lambda: registry.set_enabled("nope", True),
        lambda: registry.remove("nope"),
    ):
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - 断言错误码可区分
            assert getattr(exc, "code", "") == "capability_not_found"
        else:  # pragma: no cover - 不该走到这里
            raise AssertionError("不存在的能力必须报错")


def test_invalid_capability_id_is_rejected():
    try:
        _skill("非法 ID")
    except Exception as exc:  # noqa: BLE001
        assert "非法能力 ID" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非法 ID 必须被拒")


def test_plugins_are_mirrored_not_owned(tmp_path: Path):
    """插件只镜像进能力层：卸载插件走插件市场，能力层不替它删数据。"""

    registry = _registry(tmp_path)
    changed = registry.sync_plugins(
        [
            {
                "id": "night-batch",
                "name": "夜间批量",
                "version": "1.0.0",
                "enabled": True,
                "contribution": {"slot": "sidebar.footer.action"},
                "capabilities": ["ui.slot"],
            }
        ]
    )
    assert changed == 1
    mirrored = registry.require("plugin.night-batch")
    assert mirrored.kind is CapabilityKind.PLUGIN
    assert mirrored.meta["plugin_id"] == "night-batch"
    # 再同步一次内容没变 → 不重复写
    assert (
        registry.sync_plugins(
            [
                {
                    "id": "night-batch",
                    "name": "夜间批量",
                    "version": "1.0.0",
                    "enabled": True,
                    "contribution": {"slot": "sidebar.footer.action"},
                    "capabilities": ["ui.slot"],
                }
            ]
        )
        == 0
    )
    # 插件被卸载后镜像也跟着消失（能力中心不会留一条假记录）
    assert registry.sync_plugins([]) == 1
    assert registry.get("plugin.night-batch") is None
    # 但 skill / MCP 不受镜像影响
    registry.upsert(Capability(id="demo-skill", kind=CapabilityKind.SKILL, name="演示 Skill"))
    assert registry.sync_plugins([]) == 0
    assert registry.get("demo-skill") is not None


def test_capability_api_lists_kinds_and_toggles(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        # 安装走注册表（P1/P2 才有安装接口），读取与开关走 HTTP 接口
        registry = client.app.state.capability_registry
        registry.upsert(Capability(id="demo-skill", kind=CapabilityKind.SKILL, name="演示 Skill"))
        registry.upsert(Capability(id="demo-mcp", kind=CapabilityKind.MCP, name="filesystem"))

        listed = client.get("/api/v1/capabilities").json()
        assert [item["id"] for item in listed["capabilities"]] == ["demo-skill", "demo-mcp"]
        assert [item["id"] for item in listed["kinds"]] == ["skill", "mcp", "plugin"]
        assert listed["counts"] == {"skill": 1, "mcp": 1, "plugin": 0}

        disabled = client.post("/api/v1/capabilities/demo-skill/disable").json()
        assert disabled["capability"]["enabled"] is False
        assert [
            item["id"]
            for item in client.get("/api/v1/capabilities?kind=skill&include_disabled=false").json()[
                "capabilities"
            ]
        ] == []

        assert client.delete("/api/v1/capabilities/demo-mcp").json()["deleted"] == "demo-mcp"
        assert client.get("/api/v1/capabilities").json()["counts"]["mcp"] == 0

        audit = client.get("/api/v1/capabilities/audit").json()["events"]
        assert any(event["event"] == "uninstall" for event in audit)


def test_plugin_uninstall_is_refused_in_capability_layer(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        registry = client.app.state.capability_registry
        registry.upsert(Capability(id="plugin.demo", kind=CapabilityKind.PLUGIN, name="旧插件"))
        response = client.delete("/api/v1/capabilities/plugin.demo")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "capability_managed_elsewhere"


def test_tool_call_request_shape_is_stable():
    """模型请求工具调用走应用层协议：字段名固定，执行层据此分发。"""

    from app.schemas.capability import ToolCallRequest

    call = ToolCallRequest.model_validate(
        {"capability_id": "demo-mcp", "tool": "read_file", "arguments": {"path": "README.md"}}
    )
    assert call.capability_id == "demo-mcp"
    assert call.tool == "read_file"
    assert call.arguments == {"path": "README.md"}
