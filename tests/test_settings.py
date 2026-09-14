"""配置校验：非法保存必须被拒绝，且不能污染已有配置。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings, SettingsStore
from app.core.errors import ConfigurationError, RelayError
from app.core.relay import RelayClient


def test_settings_reject_non_url_base():
    for bad in ("1", "127.0.0.1:8799/v1", "ftp://relay.test"):
        with pytest.raises(ConfigurationError):
            Settings(relay_base_url=bad).validate_for_save()


def test_settings_accept_valid_and_empty_base():
    Settings(relay_base_url="").validate_for_save()  # 留空 = 未配置，允许
    Settings(relay_base_url="https://relay.example.com/v1").validate_for_save()
    Settings(relay_base_url="http://127.0.0.1:8799/v1").validate_for_save()


def test_api_key_that_is_a_url_is_rejected():
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(relay_api_key="https://api.example.com/v1").validate_for_save()
    assert excinfo.value.details["looks_like_url"] is True


def test_api_key_with_whitespace_is_rejected():
    with pytest.raises(ConfigurationError):
        Settings(relay_api_key="sk-abc def").validate_for_save()


def test_runtime_validation_tolerates_bad_saved_config(tmp_path: Path):
    """配置写坏也必须能启动：启动只告警，界面负责提示怎么改。"""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "relay_base_url": "1",
                "relay_api_key": "https://api.example.com/v1",
            }
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(path).get()
    settings.validate_runtime()  # 不抛异常，服务照常起来
    problems = settings.config_problems()
    assert len(problems) == 2
    with pytest.raises(ConfigurationError):
        settings.validate_for_save()


def test_api_key_normalization_on_save(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    saved = store.update({"relay_api_key": '  "Bearer sk-real-key-1234"  '})
    assert saved.relay_api_key == "sk-real-key-1234"


def test_demo_mode_never_overwrites_saved_settings(tmp_path: Path, monkeypatch):
    """演示模式：读到的是空配置，保存只在本进程内生效，不落盘。"""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {"relay_base_url": "https://real-relay.example.com/v1", "relay_api_key": "sk-real"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHESTRATOR_IGNORE_SAVED_SETTINGS", "1")

    store = SettingsStore(path)
    assert store.get().relay_base_url == ""  # 忽略已保存配置

    updated = store.update(
        {"relay_base_url": "http://127.0.0.1:8799/v1", "relay_api_key": "sk-demo"}
    )
    assert updated.relay_base_url == "http://127.0.0.1:8799/v1"

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["relay_base_url"] == "https://real-relay.example.com/v1"
    assert on_disk["relay_api_key"] == "sk-real"


# ── 多套配置（中转 / 个人 Key 直连）──


def test_legacy_flat_file_is_migrated_into_a_provider(tmp_path: Path):
    """老格式（扁平 relay_*）会自动变成一条 Provider，配置不丢。"""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "relay_base_url": "https://old-relay.example.com/v1",
                "relay_api_key": "sk-legacy-key-1234",
                "architect_model": "gpt-5.6-sol",
                "editor_model": "deepseek-v4-flash",
                "max_plan_steps": 6,
            }
        ),
        encoding="utf-8",
    )
    store = SettingsStore(path)
    profiles = store.providers()
    assert len(profiles) == 1
    assert profiles[0].base_url == "https://old-relay.example.com/v1"
    assert profiles[0].api_key == "sk-legacy-key-1234"

    settings = store.get()
    assert settings.relay_base_url == "https://old-relay.example.com/v1"
    assert settings.architect_model == "gpt-5.6-sol"
    assert settings.max_plan_steps == 6

    # 迁移只在内存里完成；第一次保存后落盘为新格式
    store.update({"editor_model": "deepseek-chat"})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] >= 2
    assert len(saved["providers"]) == 1
    assert saved["globals"]["max_plan_steps"] == 6
    assert saved["providers"][0]["editor_model"] == "deepseek-chat"


def test_switch_between_relay_and_personal_key(tmp_path: Path):
    """中转一套、个人 Key 直连一套，切换即生效，互不覆盖。"""
    store = SettingsStore(tmp_path / "settings.json")
    relay = store.create_provider(
        {
            "name": "公司中转",
            "kind": "relay",
            "base_url": "https://relay.example.com/v1",
            "api_key": "sk-relay-key-0001",
            "architect_model": "gpt-5.6-sol",
            "editor_model": "deepseek-v4-flash",
        }
    )
    direct = store.create_provider(
        {
            "name": "个人 Key 直连",
            "kind": "official",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-personal-key-0002",
            "architect_model": "deepseek-chat",
            "editor_model": "deepseek-chat",
        },
        activate=False,
    )

    assert store.get().relay_base_url == "https://relay.example.com/v1"
    assert len(store.providers()) == 2

    store.activate_provider(direct.id)
    switched = store.get()
    assert switched.relay_base_url == "https://api.deepseek.com/v1"
    assert switched.relay_api_key == "sk-personal-key-0002"
    assert switched.architect_model == "deepseek-chat"

    store.activate_provider(relay.id)
    assert store.get().relay_api_key == "sk-relay-key-0001"
    # 两套配置都还在
    assert {profile.name for profile in store.providers()} == {"公司中转", "个人 Key 直连"}


def test_update_provider_with_empty_key_keeps_existing(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    profile = store.create_provider(
        {
            "name": "中转",
            "base_url": "https://relay.example.com/v1",
            "api_key": "sk-keep-me-1234",
            "architect_model": "gpt-5",
            "editor_model": "deepseek-v4",
        }
    )
    store.update_provider(profile.id, {"api_key": "", "editor_model": "deepseek-chat"})
    updated = store.providers()[0]
    assert updated.api_key == "sk-keep-me-1234"
    assert updated.editor_model == "deepseek-chat"


def test_delete_active_provider_falls_back_to_remaining(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    first = store.create_provider(
        {"name": "A", "base_url": "https://a.example.com/v1", "api_key": "sk-a-12345678"}
    )
    second = store.create_provider(
        {"name": "B", "base_url": "https://b.example.com/v1", "api_key": "sk-b-12345678"}
    )
    store.activate_provider(second.id)
    store.delete_provider(second.id)

    assert [profile.name for profile in store.providers()] == ["A"]
    assert store.get().relay_base_url == "https://a.example.com/v1"
    assert store.active_provider().id == first.id


def test_provider_rejects_url_in_key_field(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ConfigurationError):
        store.create_provider(
            {
                "name": "写错了",
                "base_url": "https://relay.example.com/v1",
                "api_key": "https://relay.example.com/v1",
            }
        )
    assert store.providers() == []


def test_split_routes_resolve_each_segment_independently(tmp_path: Path):
    """架构段与执行段可以分别指向不同配置（中转 / 个人 Key 任意组合）。"""
    store = SettingsStore(tmp_path / "settings.json")
    relay = store.create_provider(
        {
            "name": "中转",
            "kind": "relay",
            "base_url": "https://relay.example.com/v1",
            "api_key": "sk-relay-1234",
            "wire_api": "chat_completions",
            "architect_model": "gpt-5.6-sol",
            "editor_model": "deepseek-v4-flash",
        }
    )
    direct = store.create_provider(
        {
            "name": "个人直连",
            "kind": "official",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-direct-1234",
            "wire_api": "responses",
            "architect_model": "deepseek-chat",
            "editor_model": "deepseek-chat",
        },
        activate=False,
    )

    store.set_routes(
        {
            "architect": {"provider_id": relay.id, "model": ""},
            "editor": {"provider_id": direct.id, "model": ""},
        }
    )
    settings = store.get()
    assert settings.resolve_architect().base_url == "https://relay.example.com/v1"
    assert settings.resolve_architect().api_key == "sk-relay-1234"
    assert settings.resolve_architect().model == "gpt-5.6-sol"
    assert settings.resolve_architect().wire_api == "chat_completions"

    assert settings.resolve_editor().base_url == "https://api.deepseek.com/v1"
    assert settings.resolve_editor().api_key == "sk-direct-1234"
    assert settings.resolve_editor().model == "deepseek-chat"
    assert settings.resolve_editor().wire_api == "responses"
    # 两段都配置完整
    assert settings.missing_endpoints() == []


def test_route_model_override_and_partial_route(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    relay = store.create_provider(
        {
            "name": "中转",
            "base_url": "https://relay.example.com/v1",
            "api_key": "sk-relay-1234",
            "architect_model": "gpt-5",
            "editor_model": "deepseek-v4",
        }
    )
    direct = store.create_provider(
        {
            "name": "直连",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-direct-1234",
            "architect_model": "deepseek-chat",
            "editor_model": "deepseek-chat",
        },
        activate=False,
    )

    # 只给执行段指定配置，架构段跟随当前配置
    store.set_routes({"editor": {"provider_id": direct.id, "model": "deepseek-reasoner"}})
    settings = store.get()
    assert settings.resolve_architect().base_url == "https://relay.example.com/v1"
    assert settings.resolve_editor().base_url == "https://api.deepseek.com/v1"
    assert settings.resolve_editor().model == "deepseek-reasoner"

    # 写回显式覆盖时，模型留空则回落到该配置自己的模型
    store.set_routes({"editor": {"provider_id": relay.id, "model": ""}})
    assert store.get().resolve_editor().model == "deepseek-v4"


def test_activating_provider_clears_split_routes(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    first = store.create_provider(
        {"name": "A", "base_url": "https://a.example.com/v1", "api_key": "sk-a-12345678"}
    )
    second = store.create_provider(
        {
            "name": "B",
            "base_url": "https://b.example.com/v1",
            "api_key": "sk-b-12345678",
        },
        activate=False,
    )
    store.set_routes({"architect": {"provider_id": second.id, "model": ""}})
    assert store.routes()
    store.activate_provider(first.id)
    assert store.routes() == {}


def test_store_rejects_invalid_update_and_keeps_previous_state(tmp_path: Path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.update({"relay_base_url": "https://relay.example.com/v1", "editor_model": "deepseek-v4"})

    with pytest.raises(ConfigurationError):
        store.update({"relay_base_url": "1"})

    # 内存状态与磁盘内容都保持原样，不会被写坏
    assert store.get().relay_base_url == "https://relay.example.com/v1"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] >= 2
    assert on_disk["providers"][0]["base_url"] == "https://relay.example.com/v1"

    # 失败的保存不会留下半成品：仍可继续正常更新
    updated = store.update({"editor_model": "deepseek-chat"})
    assert updated.editor_model == "deepseek-chat"
    assert updated.relay_base_url == "https://relay.example.com/v1"


def test_store_skips_empty_api_key_so_it_is_not_wiped(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(
        {"relay_api_key": "sk-abcdef123456", "relay_base_url": "https://relay.example.com"}
    )
    assert store.get().relay_api_key == "sk-abcdef123456"
    assert store.update({"relay_api_key": ""}).relay_api_key == "sk-abcdef123456"


def test_relay_client_rejects_malformed_base_url_without_network():
    async def scenario() -> None:
        def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("不应发起网络请求")

        client = RelayClient("1", "sk-test", transport=httpx.MockTransport(handler))
        with pytest.raises(RelayError) as excinfo:
            await client.acomplete([{"role": "user", "content": "hi"}], model="gpt-5")
        assert "http" in (excinfo.value.hint or "")

    asyncio.run(scenario())
