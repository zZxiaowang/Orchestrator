"""插件（声明式）与本地插件仓库。

设计参照 anywhere-labs/dsh-desktop 的插件生态（"万物皆插件"），但按本工具的
安全边界做了收敛：

* **宿主提供槽位（slot）**，插件在清单里声明自己要占哪个槽位；
* **能力（capabilities）必须显式声明**，安装前展示给用户确认；
* 当前只支持**声明式插件**——不执行第三方代码，只贡献 UI 入口与元数据；
  执行型能力（如 ``command.run``）只允许"声明"，永远不会被自动执行。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import AppError

#: 宿主暴露的槽位 → 说明（参照 dsh 的 sidebar.footer.action / sidebar.settings）
SLOTS: dict[str, str] = {
    "sidebar.footer.action": "左侧任务栏底部的动作入口",
    "sidebar.settings": "左侧任务栏底部的设置区",
    "inspector.panel": "右侧检查器里的面板入口",
}

#: 能力白名单：未列出的能力一律拒绝安装（避免"声明了却没人管"的假承诺）
CAPABILITIES: dict[str, str] = {
    "ui.slot": "占用宿主槽位（侧栏/检查器入口）",
    "provider.register": "注册模型供应商（需自行填写 Key，工具不会代为保管第三方 Key）",
    "step.hook": "在步骤前后挂钩子（当前仅记录声明，不会执行）",
    "command.run": "请求执行命令（当前仅记录声明，永远不会自动执行）",
    "storage.local": "在插件目录下保存自己的数据",
}

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
TEXT_STRIP = re.compile(r"[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]")


def clean_text(value: Any, *, limit: int = 400) -> str:
    """统一清洗外部文本：去掉控制字符与双向控制符，并限制长度。"""
    text = TEXT_STRIP.sub("", str(value or "")).strip()
    return text[:limit]


class PluginContribution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    slot: str
    label: str = ""
    icon: str = ""
    #: 点击后的去处：宿主约定的动作名（如 "open.market"）或 https 链接
    action: str = ""
    url: str = ""

    @field_validator("slot")
    @classmethod
    def _check_slot(cls, value: str) -> str:
        if value not in SLOTS:
            raise AppError(
                f"插件占用了未知槽位：{value}",
                code="plugin_slot_unsupported",
                details={"slot": value, "supported": sorted(SLOTS)},
            )
        return value

    @field_validator("label", "icon", "action", "url")
    @classmethod
    def _clean(cls, value: str) -> str:
        return clean_text(value, limit=200)


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    license: str = ""
    capabilities: list[str] = Field(default_factory=list)
    contributions: list[PluginContribution] = Field(default_factory=list)
    #: 来源溯源（哪个目录源的哪个条目）
    origin: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        text = clean_text(value, limit=64)
        if not ID_PATTERN.match(text):
            raise AppError(
                f"插件 id 不合法：{text!r}（只允许小写字母/数字/. _ -，2-64 位）",
                code="plugin_id_invalid",
                details={"plugin_id": text},
            )
        return text

    @field_validator("capabilities")
    @classmethod
    def _check_capabilities(cls, value: list[str]) -> list[str]:
        cleaned = [clean_text(item, limit=64) for item in value if clean_text(item, limit=64)]
        unknown = [item for item in cleaned if item not in CAPABILITIES]
        if unknown:
            raise AppError(
                "插件声明了本工具不支持的能力：" + "、".join(unknown),
                code="plugin_capability_unsupported",
                details={"unsupported": unknown, "supported": sorted(CAPABILITIES)},
            )
        return sorted(set(cleaned))

    @field_validator("name", "description", "author", "homepage", "license")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        return clean_text(value, limit=500)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "license": self.license,
            "capabilities": [
                {"id": item, "description": CAPABILITIES.get(item, "")}
                for item in self.capabilities
            ],
            "contributions": [item.model_dump() for item in self.contributions],
            "origin": self.origin,
        }


class InstalledPlugin(BaseModel):
    model_config = ConfigDict(extra="ignore")

    manifest: PluginManifest
    enabled: bool = True
    installed_at: str = ""

    def describe(self) -> dict[str, Any]:
        payload = self.manifest.describe()
        payload["enabled"] = self.enabled
        payload["installed_at"] = self.installed_at
        return payload


class PluginStore:
    """本地插件仓库：``data/plugins/installed.json`` + 每个插件一个目录。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_file = self.root / "installed.json"
        self._lock = threading.RLock()
        self._plugins: dict[str, InstalledPlugin] = self._read()

    def _read(self) -> dict[str, InstalledPlugin]:
        if not self.index_file.is_file():
            return {}
        try:
            raw = json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        result: dict[str, InstalledPlugin] = {}
        for item in raw.get("plugins", []) if isinstance(raw, dict) else []:
            try:
                plugin = InstalledPlugin.model_validate(item)
            except Exception:  # noqa: BLE001 - 单条损坏不应导致整仓不可用
                continue
            result[plugin.manifest.id] = plugin
        return result

    def _write(self) -> None:
        payload = {
            "version": 1,
            "plugins": [
                plugin.model_dump() for plugin in sorted(self._plugins.values(), key=_sort_key)
            ],
        }
        tmp = self.index_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.index_file)

    def list(self) -> list[InstalledPlugin]:
        with self._lock:
            return [
                plugin.model_copy(deep=True)
                for plugin in sorted(self._plugins.values(), key=_sort_key)
            ]

    def get(self, plugin_id: str) -> InstalledPlugin | None:
        with self._lock:
            plugin = self._plugins.get(plugin_id)
            return plugin.model_copy(deep=True) if plugin else None

    def install(self, manifest: PluginManifest) -> InstalledPlugin:
        with self._lock:
            plugin = InstalledPlugin(
                manifest=manifest,
                enabled=True,
                installed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            self._plugins[manifest.id] = plugin
            (self.root / manifest.id).mkdir(parents=True, exist_ok=True)
            self._write()
            return plugin.model_copy(deep=True)

    def set_enabled(self, plugin_id: str, enabled: bool) -> InstalledPlugin:
        with self._lock:
            plugin = self._plugins.get(plugin_id)
            if plugin is None:
                raise AppError(
                    f"未安装该插件：{plugin_id}",
                    code="plugin_not_found",
                    details={"plugin_id": plugin_id},
                )
            plugin.enabled = enabled
            self._write()
            return plugin.model_copy(deep=True)

    def uninstall(self, plugin_id: str) -> None:
        with self._lock:
            if plugin_id not in self._plugins:
                raise AppError(
                    f"未安装该插件：{plugin_id}",
                    code="plugin_not_found",
                    details={"plugin_id": plugin_id},
                )
            del self._plugins[plugin_id]
            self._write()

    def footer_actions(self) -> list[dict[str, Any]]:
        """已启用插件贡献的侧栏底部入口（宿主渲染用）。"""
        actions: list[dict[str, Any]] = []
        for plugin in self.list():
            if not plugin.enabled:
                continue
            for contribution in plugin.manifest.contributions:
                if contribution.slot == "sidebar.footer.action":
                    actions.append(
                        {
                            "plugin_id": plugin.manifest.id,
                            "label": contribution.label
                            or plugin.manifest.name
                            or plugin.manifest.id,
                            "icon": contribution.icon,
                            "action": contribution.action,
                            "url": contribution.url,
                        }
                    )
        return actions

    def capability_summary(self) -> dict[str, str]:
        return dict(CAPABILITIES)

    def slot_summary(self) -> dict[str, str]:
        return dict(SLOTS)


def _sort_key(plugin: InstalledPlugin) -> tuple[str, str]:
    return (plugin.manifest.name or plugin.manifest.id, plugin.manifest.id)


PluginKind = Literal["declarative"]
