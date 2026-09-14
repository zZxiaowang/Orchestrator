"""运行配置。

优先级：``data/settings.json``（界面保存）> 环境变量 / ``.env`` > 默认值。

约定：

* 一个中转网关（base_url + api_key）可以同时提供 GPT 与 DeepSeek；
* 若架构段与执行段走**不同**的中转，可分别配置 ``ARCHITECT_*`` / ``EDITOR_*``，
  留空即回落到公共的 ``RELAY_*``；
* 缺少 key 不阻止服务启动，只把对应端点标记为未配置，由界面给出提示。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigurationError, NotFoundError
from app.core.providers import (
    DEFAULT_PROVIDER_NAME,
    ProviderProfile,
    new_provider_id,
    profile_to_settings_fields,
    validate_profile,
)

ORCHESTRATOR_ROOT = Path(__file__).resolve().parents[2]


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打出的可执行文件里。"""
    return bool(getattr(sys, "frozen", False))


def _resource_root() -> Path:
    """只读资源（web/、.env.example）所在目录。

    onefile 打包时资源被解压到 ``sys._MEIPASS``；onedir 时就在 exe 同级。
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return ORCHESTRATOR_ROOT


def _dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _data_root() -> Path:
    """可写数据目录。

    优先顺序：

    1. ``ORCHESTRATOR_DATA_DIR`` 环境变量（显式指定）；
    2. **已经有配置的** ``exe 同级 data/``（便携版长期使用的位置）；
    3. **已经有配置的** ``exe 上一级 data/``：打包产物通常放在 <项目>\\dist\\，
       这条规则让双击 exe 直接用上项目里那份配置，而不是另起一份空配置；
    4. ``exe 同级 data/``（可写时）；
    5. ``%LOCALAPPDATA%\\Orchestrator\\data``（装到 Program Files 等只读位置时兜底）。

    绝不能用 ``_MEIPASS``：那是临时解压目录，退出即被删除，运行记录会丢。
    """
    override = os.getenv("ORCHESTRATOR_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if is_frozen():
        beside_exe = Path(sys.executable).resolve().parent
        candidates = [beside_exe / "data", beside_exe.parent / "data"]
        # 先认"已经配好的"那一个（有 settings.json），避免打包版另起一份空配置
        for candidate in candidates:
            if (candidate / "settings.json").is_file():
                return candidate
        if _dir_writable(beside_exe):
            return beside_exe / "data"
        local = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home())
        return Path(local) / "Orchestrator" / "data"
    return ORCHESTRATOR_ROOT / "data"


#: ``.env`` 的查找位置：源码运行在项目根；打包后放在 exe 旁边（便携配置）
CONFIG_ROOT = Path(sys.executable).resolve().parent if is_frozen() else ORCHESTRATOR_ROOT
RESOURCE_ROOT = _resource_root()
DATA_DIR = _data_root()
SETTINGS_FILE = DATA_DIR / "settings.json"
RUNS_DIR = DATA_DIR / "runs"
WEB_DIR = RESOURCE_ROOT / "web"

SUPPORTED_WIRE_APIS = ("chat_completions", "responses")
DEFAULT_ARCHITECT_MODEL = "gpt-5"
DEFAULT_EDITOR_MODEL = "deepseek-v4"

#: 允许通过界面覆盖的字段（其余保持部署侧只读）
OVERRIDABLE_FIELDS = (
    "relay_base_url",
    "relay_api_key",
    "relay_wire_api",
    "architect_model",
    "architect_base_url",
    "architect_api_key",
    "architect_wire_api",
    "editor_model",
    "editor_base_url",
    "editor_api_key",
    "editor_wire_api",
    "max_plan_steps",
    "allow_command_execution",
    "command_allowlist",
    "command_timeout_seconds",
    "step_command_rounds",
    "request_timeout_seconds",
    "context_budget_chars",
    "file_context_max_chars",
    "files_context_max_chars",
    "tree_context_max_chars",
    "completed_log_max_chars",
    "brief_max_chars",
    "step_fetch_rounds",
    "step_file_fetch_limit",
    # 主备降级（留空 = 不启用自动降级）
    "architect_backup_base_url",
    "architect_backup_api_key",
    "architect_backup_wire_api",
    "architect_backup_model",
    "architect_backup_label",
    "editor_backup_base_url",
    "editor_backup_api_key",
    "editor_backup_wire_api",
    "editor_backup_model",
    "editor_backup_label",
    "fallback_max_attempts",
)


def _ignore_saved_settings() -> bool:
    """演示/测试模式：只读忽略 ``data/settings.json``，且保存时不覆盖它。"""
    return os.getenv("ORCHESTRATOR_IGNORE_SAVED_SETTINGS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 固定读取 .env（绝对路径）：源码运行时是项目根，打包后是 exe 旁边；
        # 双击启动、从任意目录启动都不会读错文件。
        env_file=(CONFIG_ROOT / ".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 应用 ──
    app_env: str = "development"
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8787

    # ── 中转网关（公共）──
    relay_base_url: str = ""
    relay_api_key: str = ""
    relay_wire_api: str = "chat_completions"

    # ── 架构段（GPT）──
    architect_model: str = DEFAULT_ARCHITECT_MODEL
    architect_base_url: str = ""
    architect_api_key: str = ""
    architect_wire_api: str = ""

    # ── 执行段（DeepSeek V4）──
    editor_model: str = DEFAULT_EDITOR_MODEL
    editor_base_url: str = ""
    editor_api_key: str = ""
    editor_wire_api: str = ""

    # ── 行为 ──
    request_timeout_seconds: float = 300.0
    max_plan_steps: int = 8
    #: 默认**不**自动执行模型给出的命令，避免不可控副作用
    allow_command_execution: bool = False
    #: 允许执行的命令前缀白名单（逐行一条）。为空 = 什么都不执行。
    command_allowlist: list[str] = Field(default_factory=list)
    #: 单条命令的超时（秒）
    command_timeout_seconds: float = 120.0
    #: 命令失败后，允许在**同一步**内回灌报错让执行段继续修的轮数
    step_command_rounds: int = 2
    #: 执行段可读的工作区文件树条目上限，避免上下文爆炸
    context_tree_limit: int = 120
    #: 单步上下文字符预算（超出后按优先级裁剪，而不是让模型自己压缩）
    context_budget_chars: int = 24000
    #: 单个文件注入上下文的字符上限（超出改为"结构索引 + 头尾节选"）
    file_context_max_chars: int = 2400
    #: 文件段总字符上限（否则"每文件 6k"叠加后可能比旧实现更费 token）
    files_context_max_chars: int = 3000
    #: 工作区文件树注入的字符上限
    tree_context_max_chars: int = 2000
    #: 已完成步骤交接日志的字符上限
    completed_log_max_chars: int = 1200
    #: 前期沟通简报的注入上限（架构段/执行段共享）
    brief_max_chars: int = 4000
    #: 允许执行段"按需索取文件"的轮次上限
    step_fetch_rounds: int = 3
    #: 每轮最多取回的文件数
    step_file_fetch_limit: int = 6
    #: Git 面板操作的仓库目录；留空 = 本项目目录
    git_dir: str = ""

    # ── 主备降级（备用字段全空 = 不启用自动降级）──
    #: 备用端点必须 base_url + api_key 成对填写，只填一半会在保存时被拦下
    architect_backup_base_url: str = ""
    architect_backup_api_key: str = ""
    architect_backup_wire_api: str = ""
    architect_backup_model: str = ""
    #: 界面/事件里展示的备用配置别名（留空显示「备用」）
    architect_backup_label: str = ""
    editor_backup_base_url: str = ""
    editor_backup_api_key: str = ""
    editor_backup_wire_api: str = ""
    editor_backup_model: str = ""
    editor_backup_label: str = ""
    #: 一次模型逻辑调用的总尝试次数硬上限（主用 + 备用，禁止循环切换）
    fallback_max_attempts: int = 3

    def resolve_architect(self) -> Endpoint:
        return Endpoint(
            role="architect",
            label="架构段",
            base_url=self.architect_base_url or self.relay_base_url,
            api_key=self.architect_api_key or self.relay_api_key,
            wire_api=self.architect_wire_api or self.relay_wire_api,
            model=self.architect_model,
        )

    def resolve_editor(self) -> Endpoint:
        return Endpoint(
            role="editor",
            label="执行段",
            base_url=self.editor_base_url or self.relay_base_url,
            api_key=self.editor_api_key or self.relay_api_key,
            wire_api=self.editor_wire_api or self.relay_wire_api,
            model=self.editor_model,
        )

    def validate_runtime(self) -> None:
        """启动期校验：只拒绝**自相矛盾**的配置，绝不因为填错地址/Key 就起不来。

        填错地址/Key 属于"待修正的配置"，由 :meth:`config_problems` 报告给界面，
        由 :meth:`validate_for_save` 在保存时拦住。
        """
        for endpoint in (self.resolve_architect(), self.resolve_editor()):
            if endpoint.wire_api not in SUPPORTED_WIRE_APIS:
                raise ConfigurationError(
                    f"{endpoint.label} WIRE_API='{endpoint.wire_api}' 不受支持。"
                    f"可选值：{', '.join(SUPPORTED_WIRE_APIS)}",
                    details={"field": f"{endpoint.role}_wire_api"},
                )
            if not endpoint.model.strip():
                raise ConfigurationError(
                    f"{endpoint.label} 未配置模型名。",
                    details={"field": f"{endpoint.role}_model"},
                )

        if not 1 <= self.max_plan_steps <= 20:
            raise ConfigurationError(
                "MAX_PLAN_STEPS 必须在 1~20 之间。",
                details={"field": "max_plan_steps", "value": self.max_plan_steps},
            )

        if not 5 <= self.request_timeout_seconds <= 3600:
            raise ConfigurationError(
                "REQUEST_TIMEOUT_SECONDS 必须在 5~3600 之间。",
                details={
                    "field": "request_timeout_seconds",
                    "value": self.request_timeout_seconds,
                },
            )

    def validate_for_save(self) -> None:
        """保存期校验：启动期规则 + 地址/Key 的形状检查。"""
        self.validate_runtime()
        _validate_base_url(self.relay_base_url, "RELAY_BASE_URL")
        _validate_base_url(self.architect_base_url, "ARCHITECT_BASE_URL")
        _validate_base_url(self.editor_base_url, "EDITOR_BASE_URL")
        _validate_api_key(self.relay_api_key, "RELAY_API_KEY")
        _validate_api_key(self.architect_api_key, "ARCHITECT_API_KEY")
        _validate_api_key(self.editor_api_key, "EDITOR_API_KEY")

    def config_problems(self) -> list[str]:
        """不抛异常地收集配置问题，供界面提示（例如 Key 栏里填了地址）。"""
        problems: list[str] = []
        for value, field in (
            (self.relay_base_url, "RELAY_BASE_URL"),
            (self.architect_base_url, "ARCHITECT_BASE_URL"),
            (self.editor_base_url, "EDITOR_BASE_URL"),
            (self.relay_api_key, "RELAY_API_KEY"),
            (self.architect_api_key, "ARCHITECT_API_KEY"),
            (self.editor_api_key, "EDITOR_API_KEY"),
        ):
            checker = _validate_api_key if field.endswith("API_KEY") else _validate_base_url
            try:
                checker(value, field)
            except ConfigurationError as exc:
                problems.append(exc.message)
        return problems

    def missing_endpoints(self) -> list[Endpoint]:
        return [
            endpoint
            for endpoint in (self.resolve_architect(), self.resolve_editor())
            if not endpoint.configured
        ]


@dataclass(frozen=True)
class Endpoint:
    """一个 OpenAI 兼容端点（可能指向中转网关）。"""

    role: str
    label: str
    base_url: str
    api_key: str
    wire_api: str
    model: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())

    @property
    def host(self) -> str:
        if not self.base_url:
            return ""
        parsed = urlparse(self.base_url if "//" in self.base_url else f"https://{self.base_url}")
        return parsed.netloc or parsed.path

    def describe(self) -> dict[str, object]:
        return {
            "role": self.role,
            "label": self.label,
            "model": self.model,
            "wire_api": self.wire_api,
            "base_url": self.base_url,
            "host": self.host,
            "api_key_set": bool(self.api_key.strip()),
            "configured": self.configured,
        }


#: 属于"某套配置（Provider）"的字段
PROVIDER_FIELDS = (
    "relay_base_url",
    "relay_api_key",
    "relay_wire_api",
    "architect_model",
    "editor_model",
)
#: Settings 字段名 → ProviderProfile 字段名（两套命名不同，必须显式映射）
SETTINGS_TO_PROFILE = {
    "relay_base_url": "base_url",
    "relay_api_key": "api_key",
    "relay_wire_api": "wire_api",
    "architect_model": "architect_model",
    "editor_model": "editor_model",
}
#: 全局选项，与具体使用哪套配置无关
GLOBAL_FIELDS = tuple(field for field in OVERRIDABLE_FIELDS if field not in PROVIDER_FIELDS)
STATE_VERSION = 3

#: 每一段（架构/执行）各自可以独立指定 Provider 与模型
ROLE_FIELDS: dict[str, tuple[str, str, str, str]] = {
    "architect": (
        "architect_base_url",
        "architect_api_key",
        "architect_wire_api",
        "architect_model",
    ),
    "editor": (
        "editor_base_url",
        "editor_api_key",
        "editor_wire_api",
        "editor_model",
    ),
}


class SettingsStore:
    """配置仓库：多套 Provider（中转 / 个人 Key 直连）+ 全局选项。

    文件结构（``data/settings.json``）::

        {
          "version": 2,
          "active_provider_id": "p-xxxx",
          "providers": [{"id": ..., "name": "中转", "kind": "relay", ...}],
          "globals": {"max_plan_steps": 8, ...}
        }

    旧的扁平格式会自动迁移成一条名为"默认配置"的 Provider，配置不会丢。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or SETTINGS_FILE
        self._lock = threading.RLock()
        self._migrated = False
        self._state = self._read()
        self._cached: Settings | None = None
        # 迁移只在内存里完成；下一次保存时自然写成新格式，
        # 避免"仅仅导入配置模块"就改写用户文件（测试/脚本也会 import）。
        self._state = self._ensure_env_provider(self._state)

    @staticmethod
    def _ensure_env_provider(state: dict[str, object]) -> dict[str, object]:
        """没有任何已保存配置时，把环境变量/``.env`` 里的配置显形为一套可编辑配置。

        否则"用 .env 配好、打开设置却空着"会让人以为没配置。这里只在内存里生成，
        用户真的保存时才落盘（演示模式下始终不落盘）。
        """
        if state.get("providers"):
            return state
        env_settings = Settings()
        if not (env_settings.relay_base_url.strip() or env_settings.relay_api_key.strip()):
            return state
        profile = ProviderProfile(
            name="环境变量配置",
            kind="relay",
            base_url=env_settings.relay_base_url.strip(),
            api_key=env_settings.relay_api_key.strip(),
            wire_api=env_settings.relay_wire_api,
            architect_model=env_settings.architect_model,
            editor_model=env_settings.editor_model,
        )
        state = dict(state)
        state["providers"] = [profile]
        state["active_provider_id"] = profile.id
        return state

    # ── 读盘与迁移 ──

    def _empty_state(self) -> dict[str, object]:
        return {
            "version": STATE_VERSION,
            "active_provider_id": "",
            "providers": [],
            "routes": {},
            "globals": {},
        }

    def _read(self) -> dict[str, object]:
        # 演示/测试模式：忽略界面保存过的配置，保证环境变量生效（start-demo.cmd 会设置）
        if _ignore_saved_settings():
            return self._empty_state()
        if not self._path.exists():
            return self._empty_state()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_state()
        if not isinstance(raw, dict):
            return self._empty_state()

        if isinstance(raw.get("providers"), list):
            providers = [
                ProviderProfile.model_validate(item)
                for item in raw["providers"]
                if isinstance(item, dict)
            ]
            globals_ = {
                key: value
                for key, value in (raw.get("globals") or {}).items()
                if key in GLOBAL_FIELDS
            }
            active = str(raw.get("active_provider_id") or "")
            if providers and active not in {profile.id for profile in providers}:
                active = providers[0].id
            return {
                "version": STATE_VERSION,
                "active_provider_id": active,
                "providers": providers,
                "routes": raw.get("routes") if isinstance(raw.get("routes"), dict) else {},
                "globals": globals_,
            }
        self._migrated = True
        return self._migrate_legacy(raw)

    @staticmethod
    def _migrate_legacy(raw: dict[str, object]) -> dict[str, object]:
        """v1 扁平格式 → v2：``relay_*`` 收进一条 Provider，其余作为全局选项。"""
        profile = ProviderProfile(
            name=DEFAULT_PROVIDER_NAME,
            kind="relay",
            base_url=str(raw.get("relay_base_url") or ""),
            api_key=str(raw.get("relay_api_key") or ""),
            wire_api=str(raw.get("relay_wire_api") or "chat_completions"),
            architect_model=str(raw.get("architect_model") or ""),
            editor_model=str(raw.get("editor_model") or ""),
        )
        providers = [] if profile.is_empty() else [profile]
        return {
            "version": STATE_VERSION,
            "active_provider_id": profile.id if providers else "",
            "providers": providers,
            "routes": {},
            "globals": {key: value for key, value in raw.items() if key in GLOBAL_FIELDS},
        }

    # ── 读取当前生效配置 ──

    def get(self) -> Settings:
        with self._lock:
            if self._cached is None:
                self._cached = self._build_settings(self._state)
            return self._cached

    @staticmethod
    def _build_settings(state: dict[str, object]) -> Settings:
        fields: dict[str, object] = dict(state.get("globals") or {})  # type: ignore[arg-type]
        profile = SettingsStore._active_profile(state)
        if profile is not None:
            fields.update(profile_to_settings_fields(profile))
        # 分段路由：让架构段与执行段可以各自指向不同的 Provider
        routes = state.get("routes") or {}
        providers_by_id = {
            item.id: item
            for item in (state.get("providers") or [])  # type: ignore[union-attr]
        }
        for role, (url_field, key_field, wire_field, model_field) in ROLE_FIELDS.items():
            route = routes.get(role) if isinstance(routes, dict) else None
            if not isinstance(route, dict):
                continue
            target = providers_by_id.get(str(route.get("provider_id") or ""))
            if target is None:
                continue
            if target.base_url.strip():
                fields[url_field] = target.base_url.strip()
            if target.api_key.strip():
                fields[key_field] = target.api_key.strip()
            if target.wire_api.strip():
                fields[wire_field] = target.wire_api.strip()
            model = str(route.get("model") or "").strip()
            if not model:
                model = (
                    target.architect_model if role == "architect" else target.editor_model
                ).strip()
            if model:
                fields[model_field] = model
        return Settings(**fields)  # type: ignore[arg-type]

    @staticmethod
    def _active_profile(state: dict[str, object]) -> ProviderProfile | None:
        providers: list[ProviderProfile] = list(state.get("providers") or [])  # type: ignore[arg-type]
        active_id = str(state.get("active_provider_id") or "")
        for profile in providers:
            if profile.id == active_id:
                return profile
        return providers[0] if providers else None

    # ── Provider 管理 ──

    def providers(self) -> list[ProviderProfile]:
        with self._lock:
            return [
                profile.model_copy(deep=True)
                for profile in (self._state.get("providers") or [])  # type: ignore[union-attr]
            ]

    def active_provider(self) -> ProviderProfile | None:
        with self._lock:
            profile = self._active_profile(self._state)
            return profile.model_copy(deep=True) if profile else None

    def create_provider(self, data: dict[str, object], *, activate: bool = True) -> ProviderProfile:
        with self._lock:
            state = self._clone(self._state)
            profile = ProviderProfile.model_validate({**data, "id": new_provider_id()})
            if profile.api_key:
                profile.api_key = normalize_api_key(profile.api_key)
            validate_profile(profile)
            providers: list[ProviderProfile] = state["providers"]  # type: ignore[assignment]
            providers.append(profile)
            if activate or not state.get("active_provider_id"):
                state["active_provider_id"] = profile.id
            self._commit(state)
            return profile.model_copy(deep=True)

    def update_provider(self, provider_id: str, data: dict[str, object]) -> ProviderProfile:
        with self._lock:
            state = self._clone(self._state)
            profile = self._find(state, provider_id)
            patch = dict(data)
            if "api_key" in patch:
                raw_key = str(patch["api_key"] or "")
                if not raw_key.strip():
                    patch.pop("api_key")  # 留空表示不修改，避免误清空
                else:
                    patch["api_key"] = normalize_api_key(raw_key)
            updated = ProviderProfile.model_validate({**profile.model_dump(), **patch})
            validate_profile(updated)
            state["providers"] = [
                updated if item.id == provider_id else item
                for item in state["providers"]  # type: ignore[union-attr]
            ]
            self._commit(state)
            return updated.model_copy(deep=True)

    def delete_provider(self, provider_id: str) -> Settings:
        with self._lock:
            state = self._clone(self._state)
            before: list[ProviderProfile] = list(state["providers"])  # type: ignore[arg-type]
            providers = [item for item in before if item.id != provider_id]
            if len(providers) == len(before):
                raise NotFoundError(
                    f"未找到该配置：{provider_id}", details={"provider_id": provider_id}
                )
            state["providers"] = providers
            if state.get("active_provider_id") == provider_id:
                state["active_provider_id"] = providers[0].id if providers else ""
            return self._commit(state)

    def activate_provider(self, provider_id: str) -> Settings:
        with self._lock:
            state = self._clone(self._state)
            profile = self._find(state, provider_id)
            validate_profile(profile)  # 激活即使用，必须拦住明显写错的配置
            state["active_provider_id"] = provider_id
            # 侧栏"切换配置"的语义是"两段都用这套"，因此清掉分段路由；
            # 想分段请在设置里显式开启分段模式。
            state["routes"] = {}
            return self._commit(state)

    def routes(self) -> dict[str, object]:
        with self._lock:
            return dict(self._state.get("routes") or {})  # type: ignore[arg-type]

    def set_routes(self, routes: dict[str, object]) -> Settings:
        """设置分段路由：``{"architect": {"provider_id": ..., "model": ...}, ...}``。

        传空 provider_id 表示该段"跟随当前配置"。
        """
        with self._lock:
            state = self._clone(self._state)
            cleaned: dict[str, dict[str, str]] = {}
            for role in ROLE_FIELDS:
                raw = routes.get(role)
                if not isinstance(raw, dict):
                    continue
                provider_id = str(raw.get("provider_id") or "").strip()
                if not provider_id:
                    continue
                self._find(state, provider_id)  # 不存在直接 NotFound
                cleaned[role] = {
                    "provider_id": provider_id,
                    "model": str(raw.get("model") or "").strip(),
                }
            state["routes"] = cleaned
            return self._commit(state)

    def _find(self, state: dict[str, object], provider_id: str) -> ProviderProfile:
        for profile in state.get("providers") or []:  # type: ignore[union-attr]
            if profile.id == provider_id:
                return profile
        raise NotFoundError(f"未找到该配置：{provider_id}", details={"provider_id": provider_id})

    @staticmethod
    def _clone(state: dict[str, object]) -> dict[str, object]:
        return {
            "version": STATE_VERSION,
            "active_provider_id": state.get("active_provider_id") or "",
            "providers": [
                profile.model_copy(deep=True)
                for profile in (state.get("providers") or [])  # type: ignore[union-attr]
            ],
            "routes": {
                role: dict(route)
                for role, route in (state.get("routes") or {}).items()  # type: ignore[union-attr]
                if isinstance(route, dict)
            },
            "globals": dict(state.get("globals") or {}),  # type: ignore[arg-type]
        }

    # ── 兼容旧接口：扁平 patch（改当前 Provider + 全局选项） ──

    def update(self, patch: dict[str, object]) -> Settings:
        with self._lock:
            state = self._clone(self._state)
            provider_patch: dict[str, object] = {}
            for key, value in patch.items():
                if key in PROVIDER_FIELDS:
                    if key.endswith("_api_key") and isinstance(value, str):
                        if not value.strip():
                            continue  # 空字符串视为"不修改"，避免误清空
                        value = normalize_api_key(value)
                    provider_patch[key] = value
                elif key in GLOBAL_FIELDS:
                    globals_ = dict(state.get("globals") or {})  # type: ignore[arg-type]
                    globals_[key] = value
                    state["globals"] = globals_

            if provider_patch:
                current = self._active_profile(state) or ProviderProfile(name=DEFAULT_PROVIDER_NAME)
                mapped = {
                    SETTINGS_TO_PROFILE[key]: value
                    for key, value in provider_patch.items()
                    if key in SETTINGS_TO_PROFILE
                }
                updated = ProviderProfile.model_validate({**current.model_dump(), **mapped})
                validate_profile(updated)
                providers: list[ProviderProfile] = [
                    updated if item.id == current.id else item
                    for item in (state.get("providers") or [])  # type: ignore[union-attr]
                ]
                if not any(item.id == updated.id for item in providers):
                    providers.append(updated)
                state["providers"] = providers
                state["active_provider_id"] = updated.id
            return self._commit(state)

    # ── 落盘 ──

    def _commit(self, state: dict[str, object]) -> Settings:
        candidate = self._build_settings(state)
        candidate.validate_for_save()
        # 演示模式只在本进程内生效：绝不覆盖用户真实保存的配置
        if not _ignore_saved_settings():
            self._write_state(state)
        self._state = state
        self._cached = candidate
        return candidate

    def _write_state(self, state: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "active_provider_id": state.get("active_provider_id") or "",
            "providers": [
                profile.model_dump()
                for profile in (state.get("providers") or [])  # type: ignore[union-attr]
            ],
            "routes": state.get("routes") or {},
            "globals": state.get("globals") or {},
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def reset_cache(self) -> None:
        with self._lock:
            self._state = self._read()
            self._cached = None


settings_store = SettingsStore()


def get_settings() -> Settings:
    return settings_store.get()


def project_root() -> Path:
    """项目根目录（Git 面板默认操作的仓库）。

    * 源码运行：就是项目目录；
    * 打包运行：``_MEIPASS`` 只是临时解压目录，不是仓库；此时取**数据目录的上一级**
      （便携布局 ``<项目>\\dist\\Orchestrator.exe`` + ``<项目>\\data`` → ``<项目>``）。
    """
    if not is_frozen():
        return ORCHESTRATOR_ROOT
    candidate = DATA_DIR.parent
    if (candidate / ".git").exists():
        return candidate
    return CONFIG_ROOT


def _validate_base_url(value: str, field: str) -> None:
    """留空表示"未配置"；一旦填写，就必须是完整的 http/https 地址。

    否则会出现"配置看似就绪、运行时才报 UnsupportedProtocol"这种难排查的故障。
    """
    raw = (value or "").strip()
    if not raw:
        return
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigurationError(
            f"{field} 必须是完整的 http/https 地址，例如 https://your-relay.example.com/v1"
            f"（当前值：{raw}）",
            details={"field": field, "value": raw},
        )


def normalize_api_key(value: str) -> str:
    """去掉粘贴 Key 时常见的多余字符：首尾空白、包裹的引号、误带的 Bearer 前缀。"""
    key = (value or "").strip().strip("\"'`").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def _validate_api_key(value: str, field: str) -> None:
    """最常见的误填：把中转地址粘到了 Key 栏，网关只会回一句 Invalid token。

    这种错误必须在保存时就拦下来，并明确告诉用户该填哪一栏。
    """
    key = normalize_api_key(value)
    if not key:
        return
    lowered = key.lower()
    if lowered.startswith(("http://", "https://")) or "://" in lowered:
        raise ConfigurationError(
            f"{field} 看起来填成了**地址**，不是密钥：地址请填在上一栏 base_url，"
            f"Key 通常形如 sk-xxxxxxxx（当前值以 {key[:8]}… 开头）",
            details={"field": field, "looks_like_url": True},
        )
    if any(ch.isspace() for ch in key):
        raise ConfigurationError(
            f"{field} 含有空白字符，粘贴时可能带入了换行或空格，请重新复制。",
            details={"field": field},
        )
