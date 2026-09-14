"""插件市场：目录来源、条目归一化、插件安装/启用/卸载与安全边界。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.catalog import CatalogStore
from app.core.config import Settings, SettingsStore
from app.core.errors import AppError
from app.core.plugins import PluginContribution, PluginManifest, PluginStore
from app.main import create_app

MANIFEST = {
    "manifestVersion": "1.0.0",
    "providerId": "org.example.test-catalog",
    "name": "示例目录",
    "description": "测试用目录",
    "attribution": {"name": "Example", "url": "https://example.com"},
    "transport": {
        "kind": "https-json",
        "endpoint": "https://example.com/catalog.json",
        "method": "GET",
    },
}

SNAPSHOT = {
    "items": [
        {
            "id": "declarative-demo",
            "name": "declarative-demo",
            "displayName": "演示插件\u202e(反转)\u0007",
            "summary": "占用侧栏槽位",
            "latestVersion": "1.0.0",
            "license": "MIT",
            "categories": ["demo"],
            "publisher": {"name": "Example"},
            "capabilities": {"required": ["ui.slot"], "optional": []},
            "contributes": [
                {"slot": "sidebar.footer.action", "label": "演示", "action": "open.market"},
                {"slot": "evil.slot", "label": "越权"},
            ],
        },
        {
            "id": "dsh-shape-plugin",
            "name": "dsh-plugin-better-sidebar",
            "displayName": "Better Sidebar",
            "summary": "dsh 形状的条目",
            "latestVersion": "1.2.0",
            "repository": {"url": "https://github.com/example/plugin"},
            "media": {
                "icon": {
                    "assetRef": "mktimg_0123456789abcdefghijklmnopqrstuv",
                    "role": "plugin-icon",
                }
            },
            "capabilities": {"required": ["ui.slot"], "optional": ["storage.local"]},
            "compatibility": {"apiVersion": "1.x", "hosts": ["gui>=2.0"]},
        },
    ]
}


def _client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json=MANIFEST)
        if request.url.path.endswith("catalog.json"):
            return httpx.Response(200, json=SNAPSHOT)
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler), timeout=10)


def test_builtin_catalog_is_available_offline(tmp_path: Path):
    store = CatalogStore(tmp_path / "plugins")
    sources = store.list()
    assert len(sources) == 1 and sources[0].builtin
    items = store.items()
    assert {item.id for item in items} == {"run-statistics", "night-batch", "command-audit"}
    # 每个条目都带回溯信息
    assert all(item.provenance["provider_id"] == "builtin.orchestrator.local" for item in items)


def test_source_must_be_https(tmp_path: Path):
    store = CatalogStore(tmp_path / "plugins", http=_client())
    with pytest.raises(AppError) as excinfo:
        store.add_source("http://example.com/manifest.json")
    assert excinfo.value.code == "catalog_source_insecure"


def test_add_source_normalizes_items_and_sanitizes_text(tmp_path: Path):
    store = CatalogStore(tmp_path / "plugins", http=_client())
    source = store.add_source("https://example.com/manifest.json")
    assert source.manifest.provider_id == "org.example.test-catalog"

    items = store.items(source_record_id=source.source_record_id)
    ids = {item.id for item in items}
    assert ids == {"declarative-demo", "dsh-shape-plugin"}

    demo = next(item for item in items if item.id == "declarative-demo")
    # 双向控制符与控制字符被清掉
    assert "\u202e" not in demo.display_name and "\u0007" not in demo.display_name
    # 非法槽位的贡献被丢弃，合法槽位保留
    assert [entry.slot for entry in demo.contributions] == ["sidebar.footer.action"]
    assert demo.provenance["source_record_id"] == source.source_record_id

    dsh_item = next(item for item in items if item.id == "dsh-shape-plugin")
    assert dsh_item.repository_url == "https://github.com/example/plugin"
    assert dsh_item.icon_ref.startswith("mktimg_")
    assert dsh_item.capabilities_optional == ["storage.local"]


def test_bad_manifest_version_is_rejected(tmp_path: Path):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**MANIFEST, "manifestVersion": "2.0.0"})

    store = CatalogStore(
        tmp_path / "plugins", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AppError) as excinfo:
        store.add_source("https://example.com/manifest.json")
    assert excinfo.value.code == "catalog_manifest_version_unsupported"


def test_builtin_source_cannot_be_removed(tmp_path: Path):
    store = CatalogStore(tmp_path / "plugins")
    with pytest.raises(AppError) as excinfo:
        store.remove_source(store.list()[0].source_record_id)
    assert excinfo.value.code == "catalog_source_builtin"


def test_install_enable_disable_uninstall_footer_slot(tmp_path: Path):
    plugins = PluginStore(tmp_path / "plugins")
    catalog = CatalogStore(tmp_path / "plugins")
    item = next(entry for entry in catalog.items() if entry.id == "night-batch")

    installed = plugins.install(item.to_manifest())
    assert installed.enabled
    actions = plugins.footer_actions()
    assert [action["plugin_id"] for action in actions] == ["night-batch"]
    assert actions[0]["label"] == "批量"
    assert actions[0]["action"] == "open.batch"

    plugins.set_enabled("night-batch", False)
    assert plugins.footer_actions() == []
    assert plugins.get("night-batch").enabled is False

    plugins.uninstall("night-batch")
    assert plugins.list() == []
    with pytest.raises(AppError) as excinfo:
        plugins.uninstall("night-batch")
    assert excinfo.value.code == "plugin_not_found"


def test_unknown_capability_and_slot_are_rejected():
    with pytest.raises(AppError) as excinfo:
        PluginManifest(id="demo", capabilities=["unknown.capability"])
    assert excinfo.value.code == "plugin_capability_unsupported"

    with pytest.raises(AppError) as excinfo:
        PluginContribution(slot="evil.slot", label="越权")
    assert excinfo.value.code == "plugin_slot_unsupported"


def test_invalid_plugin_id_is_rejected():
    with pytest.raises(AppError):
        PluginManifest(id="插件-带中文")


def test_install_persists_across_store_reload(tmp_path: Path):
    plugins = PluginStore(tmp_path / "plugins")
    catalog = CatalogStore(tmp_path / "plugins")
    item = next(entry for entry in catalog.items() if entry.id == "run-statistics")
    plugins.install(item.to_manifest())

    reloaded = PluginStore(tmp_path / "plugins")
    assert [plugin.manifest.id for plugin in reloaded.list()] == ["run-statistics"]
    assert (
        json.loads((tmp_path / "plugins" / "installed.json").read_text(encoding="utf-8"))["version"]
        == 1
    )


def _api_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        relay_base_url="https://relay.test/v1",
        relay_api_key="sk-test-1234567890",
        architect_model="gpt-5",
        editor_model="deepseek-v4",
    )
    app = create_app(
        settings_provider=lambda: settings,
        runs_dir=tmp_path / "runs",
        web_dir=tmp_path / "no-web",
        settings_store_override=SettingsStore(tmp_path / "settings.json"),
        plugins_dir=tmp_path / "plugins",
    )
    return TestClient(app)


def test_market_api_flow(tmp_path: Path):
    with _api_client(tmp_path) as client:
        caps = client.get("/api/v1/market/capabilities").json()
        assert any(item["id"] == "ui.slot" for item in caps["capabilities"])
        assert any(item["id"] == "sidebar.footer.action" for item in caps["slots"])

        sources = client.get("/api/v1/market/sources").json()["sources"]
        assert len(sources) == 1 and sources[0]["builtin"] is True
        builtin_id = sources[0]["source_record_id"]

        items = client.get("/api/v1/market/items", params={"q": "批量"}).json()
        assert [item["id"] for item in items["items"]] == ["night-batch"]
        assert items["items"][0]["installed"] is False

        installed = client.post(
            "/api/v1/plugins",
            json={"source_record_id": builtin_id, "item_id": "night-batch"},
        )
        assert installed.status_code == 201
        payload = installed.json()
        assert payload["plugin"]["id"] == "night-batch"
        assert payload["footer_actions"][0]["action"] == "open.batch"

        # 安装后目录里的该条目标记为已安装
        again = client.get("/api/v1/market/items", params={"q": "批量"}).json()["items"][0]
        assert again["installed"] is True

        disabled = client.post("/api/v1/plugins/night-batch/disable").json()
        assert disabled["footer_actions"] == []

        removed = client.delete("/api/v1/plugins/night-batch").json()
        assert removed["plugins"] == []

        # 内置来源不可删除
        assert client.delete(f"/api/v1/market/sources/{builtin_id}").status_code == 400


def test_market_install_unknown_item_returns_404(tmp_path: Path):
    with _api_client(tmp_path) as client:
        response = client.post(
            "/api/v1/plugins", json={"source_record_id": "", "item_id": "not-exist"}
        )
        assert response.status_code == 404
