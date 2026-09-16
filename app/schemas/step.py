"""执行段（DeepSeek V4）每一步的产出结构。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderUsage(BaseModel):
    """提供方返回的 token 用量。

    兼容两种命名：Chat Completions 的 ``prompt_tokens`` / ``completion_tokens``
    与 Responses 的 ``input_tokens`` / ``output_tokens``。
    **缺失即未知**：字段保持 ``None``，不得写成 0；0 只在提供方明确返回 0 时使用。
    """

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for source, target in (
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
        ):
            if data.get(target) is None and data.get(source) is not None:
                data[target] = data[source]
        if data.get("total_tokens") is None:
            prompt = data.get("prompt_tokens")
            completion = data.get("completion_tokens")
            if isinstance(prompt, int) and isinstance(completion, int):
                data["total_tokens"] = prompt + completion
        return data


class SearchReplace(BaseModel):
    """按"查找-替换"定位改动，避免整文件重写带来的意外覆盖。"""

    model_config = ConfigDict(extra="ignore")

    search: str = ""
    replace: str = ""


class FileEdit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = ""
    action: Literal["create", "update", "delete"] = "create"
    content: str | None = None
    edits: list[SearchReplace] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"path": value, "action": "create"}
        if isinstance(value, dict):
            data = dict(value)
            action = str(data.get("action") or "").lower()
            if not action:
                action = "update" if data.get("edits") else "create"
            data["action"] = action if action in {"create", "update", "delete"} else "create"
            raw_edits = data.get("edits") or data.get("patches") or []
            if isinstance(raw_edits, dict):
                raw_edits = [raw_edits]
            data["edits"] = [coerced for coerced in map(_coerce_edit, raw_edits) if coerced]
            return data
        return value

    @property
    def valid(self) -> bool:
        if not self.path.strip():
            return False
        if self.action == "delete":
            return True
        if self.edits:
            return True
        return self.content is not None


class CommandSuggestion(BaseModel):
    """命令建议：默认只展示，不自动执行。"""

    model_config = ConfigDict(extra="ignore")

    cmd: str = ""
    why: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"cmd": value}
        return value


class StepOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    #: 交给下一步的接力说明（≤300 字）：改了哪些文件、定下了什么、还欠什么
    handoff: str = ""
    #: 执行段发现上下文不足时，可以按路径索取文件（下一轮会补给它）
    need_files: list[str] = Field(default_factory=list)
    need_reason: str = ""
    #: 模型请求调用外部工具（MCP）：应用执行后把结果回灌进同一步
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""
    files: list[FileEdit] = Field(default_factory=list)
    commands: list[CommandSuggestion] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    #: 提供方返回的 token 用量；由服务层在解析后回填，模型自己返回时被忽略
    usage: ProviderUsage | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data: dict[str, Any] = dict(value)
        for key in ("files", "commands", "notes"):
            item = data.get(key)
            if item is None:
                data[key] = []
            elif not isinstance(item, list):
                data[key] = [item]
        raw_need = data.get("need_files") or data.get("needs") or []
        if isinstance(raw_need, str):
            raw_need = [raw_need]
        data["need_files"] = [str(item) for item in raw_need if str(item).strip()]
        raw_calls = data.get("tool_calls") or data.get("tools") or []
        if isinstance(raw_calls, dict):
            raw_calls = [raw_calls]
        calls: list[dict[str, Any]] = []
        for item in raw_calls if isinstance(raw_calls, list) else []:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or item.get("name") or "").strip()
            if not tool:
                continue
            calls.append(
                {
                    "capability_id": str(
                        item.get("capability_id") or item.get("server") or ""
                    ).strip(),
                    "tool": tool,
                    "arguments": item.get("arguments")
                    if isinstance(item.get("arguments"), dict)
                    else {},
                    "reason": str(item.get("reason") or ""),
                }
            )
        data["tool_calls"] = calls
        data["notes"] = [str(n) for n in data["notes"]]
        if data.get("need_reason") is None:
            data["need_reason"] = ""
        if data.get("blocked_reason") and not data.get("block_reason"):
            data["block_reason"] = data["blocked_reason"]
        return data


def _coerce_edit(item: Any) -> dict[str, str] | None:
    """把 ``SearchReplace`` 实例或 dict 统一成可校验的字典。"""
    if isinstance(item, SearchReplace):
        return {"search": item.search, "replace": item.replace}
    if isinstance(item, dict):
        return {
            "search": str(item.get("search", "")),
            "replace": str(item.get("replace", "")),
        }
    if hasattr(item, "search") and hasattr(item, "replace"):
        return {"search": str(item.search), "replace": str(item.replace)}
    return None
