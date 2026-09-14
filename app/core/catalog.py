"""插件市场目录（catalog）：来源注册 + 拉取 + 归一化。

契约参照 anywhere-labs/dsh-desktop 的 ``dsh-community-market``：

* 用户注册的是**目录清单 URL**（HTTPS），选择状态留在本地；
* 清单声明 ``manifestVersion / providerId / attribution / transport / query``；
* 宿主为每条来源生成自己的 ``source_record_id``（不信任提供方给的标识）；
* 每个条目都带回溯信息 ``provenance``，界面必须能显示"这条来自哪个源"。

与 dsh 的差异：本工具当前只支持**声明式插件**，因此额外接受一个可选的
``contributes`` 字段（声明占用哪些宿主槽位），并且不下载、不执行任何插件代码。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import AppError
from app.core.plugins import (
    CAPABILITIES,
    SLOTS,
    PluginContribution,
    PluginManifest,
    clean_text,
)

MANIFEST_VERSION = "1.0.0"
MAX_SNAPSHOT_BYTES = 512 * 1024
MAX_ITEMS = 200
FETCH_TIMEOUT = 20.0


class Attribution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    url: str = ""
    notice: str = ""


class Transport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["https-json", "builtin"] = "https-json"
    endpoint: str = ""
    method: Literal["GET", "POST"] = "GET"


class CatalogSourceManifest(BaseModel):
    """目录来源清单（对应 dsh 的 catalog-source.schema.json）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    manifest_version: str = Field(MANIFEST_VERSION, alias="manifestVersion")
    provider_id: str = Field("", alias="providerId")
    name: str = ""
    description: str = ""
    homepage: str = ""
    attribution: Attribution = Field(default_factory=Attribution)
    transport: Transport = Field(default_factory=Transport)
    query: dict[str, Any] = Field(default_factory=dict)

    @field_validator("manifest_version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if value != MANIFEST_VERSION:
            raise AppError(
                f"不支持的目录清单版本：{value}（当前支持 {MANIFEST_VERSION}）",
                code="catalog_manifest_version_unsupported",
                details={"manifest_version": value, "supported": MANIFEST_VERSION},
            )
        return value

    @field_validator("provider_id")
    @classmethod
    def _check_provider_id(cls, value: str) -> str:
        text = clean_text(value, limit=128)
        parts = text.split(".")
        if len(parts) < 2 or not all(
            part and all(ch.isalnum() or ch in "-_" for ch in part) for part in parts
        ):
            raise AppError(
                f"providerId 不合法：{text!r}（建议用反向域名，如 org.example.catalog）",
                code="catalog_provider_id_invalid",
                details={"provider_id": text},
            )
        return text.lower()


class CatalogItem(BaseModel):
    """归一化后的目录条目（对应 dsh 的 catalog-snapshot item）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    display_name: str = ""
    summary: str = ""
    homepage: str = ""
    latest_version: str = ""
    license: str = ""
    categories: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    repository_url: str = ""
    package_registry: str = ""
    package_name: str = ""
    publisher_name: str = ""
    publisher_url: str = ""
    #: 不透明图标引用（宿主管理），Renderer 永远拿不到远程 URL 或本地路径
    icon_ref: str = ""
    icon_role: str = ""
    icon_alt: str = ""
    capabilities_required: list[str] = Field(default_factory=list)
    capabilities_optional: list[str] = Field(default_factory=list)
    api_version: str = ""
    hosts: list[str] = Field(default_factory=list)
    updated_at: str = ""
    contributions: list[PluginContribution] = Field(default_factory=list)
    provenance: dict[str, str] = Field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.display_name or self.name or self.id

    def to_manifest(self) -> PluginManifest:
        """把目录条目转成可安装的插件清单（声明式）。"""
        return PluginManifest(
            id=self.id,
            name=self.title,
            version=self.latest_version or "0.0.0",
            description=self.summary,
            author=self.publisher_name,
            homepage=self.homepage or self.repository_url,
            license=self.license,
            capabilities=list(self.capabilities_required),
            contributions=list(self.contributions),
            origin={
                "provider_id": self.provenance.get("provider_id", ""),
                "source_record_id": self.provenance.get("source_record_id", ""),
                "item_id": self.provenance.get("item_id", self.id),
                "homepage": self.homepage,
            },
        )

    def describe(self, *, installed: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "summary": self.summary,
            "homepage": self.homepage,
            "latest_version": self.latest_version,
            "license": self.license,
            "categories": self.categories,
            "keywords": self.keywords,
            "publisher": {"name": self.publisher_name, "url": self.publisher_url},
            "repository_url": self.repository_url,
            "package": {"registry": self.package_registry, "name": self.package_name},
            "icon": {"ref": self.icon_ref, "role": self.icon_role, "alt": self.icon_alt},
            "capabilities": {
                "required": [
                    {"id": item, "description": CAPABILITIES.get(item, "")}
                    for item in self.capabilities_required
                ],
                "optional": [
                    {"id": item, "description": CAPABILITIES.get(item, "")}
                    for item in self.capabilities_optional
                ],
            },
            "compatibility": {"api_version": self.api_version, "hosts": self.hosts},
            "updated_at": self.updated_at,
            "contributions": [item.model_dump() for item in self.contributions],
            "provenance": self.provenance,
            "installed": installed,
        }


class CatalogSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    #: 宿主生成的本地身份（不采信提供方自报的 id 作为主键）
    source_record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    manifest: CatalogSourceManifest
    builtin: bool = False
    added_at: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "source_record_id": self.source_record_id,
            "builtin": self.builtin,
            "added_at": self.added_at,
            "provider_id": self.manifest.provider_id,
            "name": self.manifest.name or self.manifest.provider_id,
            "description": self.manifest.description,
            "homepage": self.manifest.homepage,
            "attribution": self.manifest.attribution.model_dump(),
            "transport": self.manifest.transport.model_dump(),
        }


#: 内置示例目录：让市场在离线/未配置来源时也可用（内容仅为声明式插件）
BUILTIN_ITEMS: list[dict[str, Any]] = [
    {
        "id": "run-statistics",
        "name": "run-statistics",
        "displayName": "运行统计增强",
        "summary": "在右侧检查器加入统计面板入口：按步骤展示耗时、上下文占用与重试次数。",
        "latestVersion": "1.0.0",
        "license": "MIT",
        "categories": ["observability"],
        "keywords": ["metrics", "token"],
        "publisher": {"name": "Orchestrator Labs"},
        "capabilities": {"required": ["ui.slot"], "optional": ["storage.local"]},
        "contributes": [
            {"slot": "inspector.panel", "label": "统计", "icon": "▤", "action": "open.metrics"}
        ],
    },
    {
        "id": "night-batch",
        "name": "night-batch",
        "displayName": "夜间批量执行",
        "summary": "在左侧任务栏底部加入批量入口，用于排队跑多个纲领（分批交付）。",
        "latestVersion": "0.3.1",
        "license": "Apache-2.0",
        "categories": ["automation"],
        "keywords": ["batch", "queue"],
        "publisher": {"name": "Orchestrator Labs"},
        "capabilities": {"required": ["ui.slot", "step.hook"], "optional": []},
        "contributes": [
            {"slot": "sidebar.footer.action", "label": "批量", "icon": "≡", "action": "open.batch"}
        ],
    },
    {
        "id": "command-audit",
        "name": "command-audit",
        "displayName": "命令白名单审查",
        "summary": "审查执行段给出的命令建议；本插件只声明能力，不会执行任何命令。",
        "latestVersion": "0.2.0",
        "license": "MIT",
        "categories": ["security"],
        "keywords": ["audit", "policy"],
        "publisher": {"name": "Community"},
        "capabilities": {"required": ["command.run"], "optional": []},
        "contributes": [],
    },
]


class CatalogStore:
    """目录来源本地注册表：``data/plugins/sources.json``。"""

    def __init__(self, root: Path, *, http: httpx.Client | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "sources.json"
        self._lock = threading.RLock()
        self._http = http
        self._sources: list[CatalogSource] = self._read()
        if not any(item.builtin for item in self._sources):
            self._sources.insert(0, self._builtin_source())
            self._write()

    # ── 读写 ──

    @staticmethod
    def _builtin_source() -> CatalogSource:
        return CatalogSource(
            manifest=CatalogSourceManifest(
                manifestVersion=MANIFEST_VERSION,
                providerId="builtin.orchestrator.local",
                name="内置示例目录",
                description="随工具附带的示例插件（仅供演示市场流程，均为声明式插件）。",
                attribution=Attribution(name="Orchestrator", url="https://github.com/"),
                transport=Transport(kind="builtin", endpoint="builtin://sample", method="GET"),
            ),
            builtin=True,
            added_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

    def _read(self) -> list[CatalogSource]:
        if not self.file.is_file():
            return []
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = raw.get("sources", []) if isinstance(raw, dict) else []
        sources: list[CatalogSource] = []
        for item in items:
            try:
                sources.append(CatalogSource.model_validate(item))
            except Exception:  # noqa: BLE001 - 单条损坏跳过
                continue
        return sources

    def _write(self) -> None:
        payload = {"version": 1, "sources": [item.model_dump() for item in self._sources]}
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.file)

    def list(self) -> list[CatalogSource]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._sources]

    def get(self, source_record_id: str) -> CatalogSource:
        for item in self.list():
            if item.source_record_id == source_record_id:
                return item
        raise AppError(
            f"未注册该目录来源：{source_record_id}",
            code="catalog_source_not_found",
            details={"source_record_id": source_record_id},
        )

    # ── 注册来源：从清单 URL 拉取并校验 ──

    def add_source(self, manifest_url: str) -> CatalogSource:
        url = clean_text(manifest_url, limit=2048)
        if not url.lower().startswith("https://"):
            raise AppError(
                "目录清单地址必须是 https:// 开头（市场只接受 HTTPS 来源）。",
                code="catalog_source_insecure",
                details={"manifest_url": url},
            )
        payload = self._fetch_json(url, method="GET")
        manifest = CatalogSourceManifest.model_validate(payload)
        if (
            manifest.transport.kind == "https-json"
            and not manifest.transport.endpoint.lower().startswith("https://")
        ):
            raise AppError(
                "清单里的 transport.endpoint 必须是 https:// 地址。",
                code="catalog_endpoint_insecure",
                details={"endpoint": manifest.transport.endpoint},
            )
        source = CatalogSource(
            manifest=manifest,
            builtin=False,
            added_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        with self._lock:
            self._sources = [
                item for item in self._sources if item.manifest.provider_id != manifest.provider_id
            ]
            self._sources.append(source)
            self._write()
        return source

    def remove_source(self, source_record_id: str) -> None:
        with self._lock:
            target = next(
                (item for item in self._sources if item.source_record_id == source_record_id),
                None,
            )
            if target is None:
                raise AppError(
                    f"未注册该目录来源：{source_record_id}",
                    code="catalog_source_not_found",
                    details={"source_record_id": source_record_id},
                )
            if target.builtin:
                raise AppError(
                    "内置示例目录不可删除。",
                    code="catalog_source_builtin",
                    details={"source_record_id": source_record_id},
                )
            self._sources = [
                item for item in self._sources if item.source_record_id != source_record_id
            ]
            self._write()

    # ── 拉取条目 ──

    def items(
        self,
        *,
        source_record_id: str = "",
        query: str = "",
        category: str = "",
        capability: str = "",
        limit: int = 50,
    ) -> list[CatalogItem]:
        limit = max(1, min(int(limit or 50), MAX_ITEMS))
        needle = clean_text(query, limit=200).lower()
        category_filter = clean_text(category, limit=64).lower()
        capability_filter = clean_text(capability, limit=64)
        results: list[CatalogItem] = []
        for source in self.list():
            if source_record_id and source.source_record_id != source_record_id:
                continue
            raw_items = self._load_items(source)
            for raw in raw_items:
                try:
                    item = normalize_item(raw, source=source)
                except AppError:
                    continue
                if (
                    needle
                    and needle
                    not in " ".join(
                        [item.id, item.name, item.display_name, item.summary, *item.keywords]
                    ).lower()
                ):
                    continue
                if category_filter and category_filter not in [c.lower() for c in item.categories]:
                    continue
                if capability_filter and capability_filter not in (
                    item.capabilities_required + item.capabilities_optional
                ):
                    continue
                results.append(item)
        results.sort(key=lambda item: (item.title.lower(), item.id))
        return results[:limit]

    def _load_items(self, source: CatalogSource) -> list[dict[str, Any]]:
        if source.builtin:
            return list(BUILTIN_ITEMS)
        transport = source.manifest.transport
        payload = self._fetch_json(
            transport.endpoint, method=transport.method, params=source.manifest.query or None
        )
        for key in ("items", "data", "plugins"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)][:MAX_ITEMS]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)][:MAX_ITEMS]
        raise AppError(
            "目录响应里没有找到条目列表（期望 items / data 数组）。",
            code="catalog_snapshot_invalid",
            details={"provider_id": source.manifest.provider_id},
        )

    def _fetch_json(
        self, url: str, *, method: str = "GET", params: dict[str, Any] | None = None
    ) -> Any:
        client = self._http or httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True)
        owns_client = self._http is None
        try:
            response = client.request(method, url, params=params)
            if response.status_code != 200:
                raise AppError(
                    f"目录请求失败：HTTP {response.status_code}",
                    code="catalog_fetch_failed",
                    details={"url": url, "status": response.status_code},
                )
            if len(response.content) > MAX_SNAPSHOT_BYTES:
                raise AppError(
                    f"目录响应过大（>{MAX_SNAPSHOT_BYTES // 1024}KB），已拒绝。",
                    code="catalog_snapshot_too_large",
                    details={"url": url, "bytes": len(response.content)},
                )
            return response.json()
        except httpx.HTTPError as exc:
            raise AppError(
                f"目录拉取失败：{exc.__class__.__name__}",
                code="catalog_fetch_failed",
                details={"url": url, "hint": "检查网络或该目录是否可访问。"},
            ) from exc
        except ValueError as exc:
            raise AppError(
                "目录返回的不是合法 JSON。",
                code="catalog_snapshot_invalid",
                details={"url": url},
            ) from exc
        finally:
            if owns_client:
                client.close()


def normalize_item(raw: dict[str, Any], *, source: CatalogSource) -> CatalogItem:
    """把提供方的任意条目形状收敛成统一的 CatalogItem（含清洗与白名单校验）。"""
    capabilities = raw.get("capabilities")
    required: list[str] = []
    optional: list[str] = []
    if isinstance(capabilities, dict):
        required = [clean_text(item, limit=64) for item in capabilities.get("required", []) or []]
        optional = [clean_text(item, limit=64) for item in capabilities.get("optional", []) or []]
    elif isinstance(capabilities, list):
        required = [clean_text(item, limit=64) for item in capabilities]

    contributions: list[PluginContribution] = []
    for item in raw.get("contributes", []) or []:
        try:
            contributions.append(PluginContribution.model_validate(item))
        except Exception:  # noqa: BLE001 - 槽位不合法就直接不采纳该贡献
            continue

    repository = raw.get("repository") if isinstance(raw.get("repository"), dict) else {}
    package = raw.get("package") if isinstance(raw.get("package"), dict) else {}
    publisher = raw.get("publisher") if isinstance(raw.get("publisher"), dict) else {}
    media = raw.get("media") if isinstance(raw.get("media"), dict) else {}
    icon = media.get("icon") if isinstance(media.get("icon"), dict) else {}
    compatibility = raw.get("compatibility") if isinstance(raw.get("compatibility"), dict) else {}

    item_id = clean_text(raw.get("id") or raw.get("name"), limit=64)
    if not item_id:
        raise AppError("目录条目缺少 id。", code="catalog_item_invalid", details={})

    unknown_caps = [cap for cap in required if cap not in CAPABILITIES]
    if unknown_caps:
        # 不支持的必需能力 → 该条目不可安装，但仍可展示（标为不兼容）
        pass

    return CatalogItem(
        id=item_id,
        name=clean_text(raw.get("name"), limit=120),
        display_name=clean_text(raw.get("displayName") or raw.get("display_name"), limit=120),
        summary=clean_text(raw.get("summary") or raw.get("description"), limit=500),
        homepage=clean_text(raw.get("homepage"), limit=2048),
        latest_version=clean_text(raw.get("latestVersion") or raw.get("version"), limit=64),
        license=clean_text(raw.get("license"), limit=64),
        categories=[clean_text(value, limit=64) for value in (raw.get("categories") or [])][:20],
        keywords=[clean_text(value, limit=64) for value in (raw.get("keywords") or [])][:32],
        repository_url=clean_text(repository.get("url"), limit=2048),
        package_registry=clean_text(package.get("registry"), limit=64),
        package_name=clean_text(package.get("name"), limit=160),
        publisher_name=clean_text(publisher.get("name"), limit=120),
        publisher_url=clean_text(publisher.get("url"), limit=2048),
        icon_ref=clean_text(icon.get("assetRef"), limit=64),
        icon_role=clean_text(icon.get("role"), limit=32),
        icon_alt=clean_text(icon.get("alt"), limit=240),
        capabilities_required=sorted(set(required)),
        capabilities_optional=sorted(set(optional)),
        api_version=clean_text(compatibility.get("apiVersion"), limit=32),
        hosts=[clean_text(value, limit=64) for value in (compatibility.get("hosts") or [])][:8],
        updated_at=clean_text(raw.get("updatedAt"), limit=64),
        contributions=contributions[:8],
        provenance={
            "source_record_id": source.source_record_id,
            "provider_id": source.manifest.provider_id,
            "item_id": item_id,
        },
    )


def supported_capabilities() -> dict[str, Any]:
    return {
        "capabilities": [
            {"id": key, "description": value} for key, value in sorted(CAPABILITIES.items())
        ],
        "slots": [{"id": key, "description": value} for key, value in sorted(SLOTS.items())],
    }
