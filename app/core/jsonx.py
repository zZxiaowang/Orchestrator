"""从模型输出里稳健地取出 JSON。

模型（尤其非原生 JSON 模式的网关）经常在 JSON 前后附带解释文字或包裹
Markdown 代码块，这里统一做容错提取，避免把解析细节散落到各服务里。
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """返回第一个可解析的 JSON 对象；失败返回 ``None``。"""
    if not text:
        return None

    for candidate in _candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return {"items": parsed}
    return None


def _candidates(text: str) -> list[str]:
    out: list[str] = []
    stripped = text.strip()
    out.append(stripped)

    for match in _FENCE_RE.finditer(text):
        body = match.group(1).strip()
        if body:
            out.append(body)

    balanced = _first_balanced_object(text)
    if balanced:
        out.append(balanced)
    return out


def _first_balanced_object(text: str) -> str | None:
    """扫描出第一个括号配平的 ``{...}`` 片段，忽略字符串内的括号。"""
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
