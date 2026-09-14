"""多套中转/直连配置（Provider）。

场景："中转网关 + 一个 Key" 和 "个人 Key 直连官方" 往往并存，
来回改地址和 Key 很容易改错。这里把每套配置存成一个 profile，切换只改 active 指针。

``kind`` 只影响界面提示与默认值（直连时给官方地址预设），
运行期逻辑完全一致：都是 OpenAI 兼容端点 + 模型名。
"""

from __future__ import annotations

import secrets
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ConfigurationError

ProviderKind = Literal["relay", "official"]
DEFAULT_PROVIDER_NAME = "默认配置"

#: 个人 Key 直连时的常用官方地址（仅作界面预设）
KIND_PRESETS: dict[str, list[dict[str, str]]] = {
    "relay": [
        {"label": "自定义中转", "base_url": ""},
    ],
    "official": [
        {"label": "OpenAI", "base_url": "https://api.openai.com/v1"},
        {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1"},
        {"label": "Moonshot", "base_url": "https://api.moonshot.cn/v1"},
        {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
        {"label": "自定义官方", "base_url": ""},
    ],
}


def new_provider_id() -> str:
    return f"p-{secrets.token_hex(4)}"


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_provider_id)
    name: str = ""
    kind: ProviderKind = "relay"
    base_url: str = ""
    api_key: str = ""
    wire_api: str = "chat_completions"
    architect_model: str = ""
    editor_model: str = ""

    def is_empty(self) -> bool:
        """没有任何有效配置（用于判断是否值得落盘成一条 profile）。"""
        return not any(
            str(value).strip()
            for value in (
                self.base_url,
                self.api_key,
                self.architect_model,
                self.editor_model,
            )
        )

    def display_name(self) -> str:
        return self.name.strip() or ("中转配置" if self.kind == "relay" else "直连配置")

    def describe(self) -> dict[str, Any]:
        """给界面的安全视图：绝不包含明文 Key。"""
        return {
            "id": self.id,
            "name": self.display_name(),
            "kind": self.kind,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "architect_model": self.architect_model,
            "editor_model": self.editor_model,
            "api_key_set": bool(self.api_key.strip()),
            "api_key_masked": mask_api_key(self.api_key),
            "complete": bool(
                self.base_url.strip()
                and self.api_key.strip()
                and self.architect_model.strip()
                and self.editor_model.strip()
            ),
        }


def mask_api_key(value: str) -> str:
    key = (value or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def profile_to_settings_fields(profile: ProviderProfile) -> dict[str, object]:
    """把 profile 映射成 :class:`~app.core.config.Settings` 的字段（空值不覆盖环境变量）。"""
    fields: dict[str, object] = {}
    if profile.base_url.strip():
        fields["relay_base_url"] = profile.base_url.strip()
    if profile.api_key.strip():
        fields["relay_api_key"] = profile.api_key.strip()
    if profile.wire_api.strip():
        fields["relay_wire_api"] = profile.wire_api.strip()
    if profile.architect_model.strip():
        fields["architect_model"] = profile.architect_model.strip()
    if profile.editor_model.strip():
        fields["editor_model"] = profile.editor_model.strip()
    return fields


def validate_profile(profile: ProviderProfile) -> None:
    """形状校验：地址必须是 http(s)，Key 不能是地址。"""
    from app.core.config import _validate_api_key, _validate_base_url

    _validate_base_url(profile.base_url, f"{profile.display_name()} 的 base_url")
    _validate_api_key(profile.api_key, f"{profile.display_name()} 的 API Key")
    if profile.wire_api not in ("chat_completions", "responses"):
        raise ConfigurationError(
            f"{profile.display_name()} 的协议 '{profile.wire_api}' 不受支持。",
            details={"field": "wire_api", "allowed": ["chat_completions", "responses"]},
        )
