"""示例 MCP 服务器（stdio，换行分隔的 JSON-RPC 2.0）。

用途：让"MCP 功能"开箱可验证——能力中心里的「示例 MCP（本地）」预设就指向它。
提供两个无副作用工具：

* ``echo``     原样回显文本（验证调用链路）
* ``now``      返回当前时间（验证有返回值、有 schema）

它刻意保持零依赖、可离线跑；真实使用时把命令换成你要接的 MCP 服务器即可
（例如 npx @modelcontextprotocol/server-filesystem）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

TOOLS = [
    {
        "name": "echo",
        "description": "原样回显一段文本（用于验证 MCP 链路是否通）",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "now",
        "description": "返回当前本地时间（ISO 格式）",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def handle(message: dict) -> dict | None:
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "orchestrator-demo-mcp", "version": "1.0.0"},
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            text = str(arguments.get("text") or "")
            return {"content": [{"type": "text", "text": f"echo: {text}"}], "isError": False}
        if name == "now":
            return {
                "content": [{"type": "text", "text": datetime.now().isoformat(timespec="seconds")}],
                "isError": False,
            }
        return {
            "content": [{"type": "text", "text": f"未知工具：{name}"}],
            "isError": True,
        }
    return None


def main() -> None:
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except ValueError:
            continue
        is_notification = "id" not in message
        result = handle(message)
        if is_notification or result is None:
            if is_notification:
                continue
            payload = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32601, "message": f"不支持的方法：{message.get('method')}"},
            }
        else:
            payload = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
