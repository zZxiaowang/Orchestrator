"""MCP 客户端（Model Context Protocol）：把外部工具服务器接进编排器。

支持两种传输：

* **stdio**：本地进程，按换行分隔的 JSON-RPC 2.0 交互（initialize → tools/list → tools/call）；
* **Streamable HTTP**：POST JSON-RPC，响应可能是 JSON 也可能是 SSE（两种都解析）。

设计取舍：

* **每次操作起一个会话**（stdio 用完就关）：不长期占进程、不会留僵尸；代价是每步多一点启动开销
  （后续要做常驻会话时，只需替换 ``_StdioSession`` 的生命周期，接口不变）；
* 所有调用**带超时与输出截断**，失败一律收敛成结构化结果，不抛给主流程；
* 服务器本体是"会跑代码"的能力：能不能用由能力层决定（启用 / 作用域 / 是否已确认信任），
  这一层只负责"照做并把结果说清楚"。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.core.errors import AppError
from app.schemas.capability import Capability

#: 协议版本：跟主流的 2025-06-18 对齐，服务器不接受时用它自己返回的版本继续
PROTOCOL_VERSION = "2025-06-18"
INIT_TIMEOUT_SECONDS = 20.0
CALL_TIMEOUT_SECONDS = 60.0
MAX_RESULT_CHARS = 20000

#: 隐藏子进程窗口（Windows）：桌面版没有控制台，否则每起一个 MCP 服务器都会弹黑窗
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 常用 MCP 服务器预设：只给"怎么起"，不含任何密钥；装了默认**不启用、未确认**
MCP_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "demo",
        "name": "示例 MCP（本地，零依赖）",
        "description": "仓库自带的示例服务器：echo / now 两个工具，用来验证链路。",
        "transport": "stdio",
        "command": "python",
        "args": ["scripts/demo_mcp_server.py"],
        "requires": "",
    },
    {
        "id": "filesystem",
        "name": "filesystem（读/写本地文件）",
        "description": "按目录开放文件读写；建议把 cwd 指到项目工作区。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "requires": "需要 Node.js",
    },
    {
        "id": "git",
        "name": "git（仓库读操作）",
        "description": "查看提交、diff、分支等；--repository 指向要接的仓库。",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "."],
        "requires": "需要 uv / uvx",
    },
    {
        "id": "fetch",
        "name": "fetch（抓网页）",
        "description": "把网页抓成 markdown 给模型看。",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "requires": "需要 uv / uvx",
    },
    {
        "id": "sqlite",
        "name": "sqlite（查询数据库）",
        "description": "对指定 sqlite 文件做只读查询。",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "data.db"],
        "requires": "需要 uv / uvx",
    },
    {
        "id": "memory",
        "name": "memory（知识图谱记忆）",
        "description": "跨会话记住实体与关系。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "requires": "需要 Node.js",
    },
    {
        "id": "time",
        "name": "time（时间与时区）",
        "description": "取当前时间、做时区换算。",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-time"],
        "requires": "需要 uv / uvx",
    },
    {
        "id": "playwright",
        "name": "playwright（浏览器自动化）",
        "description": "打开页面、截图、点击；会下载浏览器内核。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "requires": "需要 Node.js",
    },
    {
        "id": "sequential-thinking",
        "name": "sequential-thinking（分步推理）",
        "description": "把复杂问题拆成可回退的多步推理。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "requires": "需要 Node.js",
    },
)


def preset_by_id(preset_id: str) -> dict[str, Any] | None:
    for item in MCP_PRESETS:
        if item["id"] == preset_id:
            return dict(item)
    return None


@dataclass
class McpTool:
    """服务器暴露的一个工具。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class McpResult:
    """一次工具调用的结果（会进步骤记录与审计）。"""

    ok: bool = False
    tool: str = ""
    capability_id: str = ""
    content: str = ""
    error: str = ""
    duration_ms: int = 0
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "tool": self.tool,
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
            "source": "mcp",
        }


class McpClient(Protocol):
    async def list_tools(self) -> list[McpTool]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResult: ...


def clip_result(text: str, *, limit: int = MAX_RESULT_CHARS) -> tuple[str, bool]:
    value = text or ""
    if len(value) <= limit:
        return value, False
    return value[:limit] + f"\n…（已截断，共 {len(value)} 字符）", True


def content_to_text(content: Any) -> str:
    """把 MCP 的 content 数组压成可读文本（text / 其它类型给出类型说明）。"""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False) if content else ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            kind = item.get("type")
            if kind == "text":
                parts.append(str(item.get("text") or ""))
            elif kind == "resource":
                resource = item.get("resource") or {}
                parts.append(f"[资源 {resource.get('uri', '')}] {resource.get('text', '')}")
            else:
                parts.append(
                    f"[{kind or '未知类型'}] " + json.dumps(item, ensure_ascii=False)[:400]
                )
        else:
            parts.append(str(item))
    return "\n".join(part for part in parts if part)


class _StdioSession:
    """一个 stdio 会话：起进程、按行收发 JSON-RPC、用完关掉。"""

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._command = command
        self._args = list(args or [])
        self._env = env or {}
        self._cwd = cwd or None
        self._next_id = 1
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self.stderr_tail: str = ""

    def __enter__(self) -> _StdioSession:
        executable = shutil.which(self._command) or self._command
        try:
            self._process = subprocess.Popen(  # noqa: S603 - 命令来自用户显式配置的 MCP 服务器
                [executable, *self._args],
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env={**os.environ, **self._env, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise AppError(
                f"找不到 MCP 服务器命令：{self._command}", code="mcp_server_missing"
            ) from exc
        except OSError as exc:
            raise AppError(f"启动 MCP 服务器失败：{exc}", code="mcp_server_failed") from exc
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        process = self._process
        if process is None:
            return
        with _suppress_all():
            if process.stdin:
                process.stdin.close()
        with _suppress_all():
            process.terminate()
            process.wait(timeout=5)
        if process.poll() is None:  # pragma: no cover - 卡住的服务器
            with _suppress_all():
                process.kill()
        for stream in (process.stdout, process.stderr):
            with _suppress_all():
                stream.close() if stream else None
        self._process = None

    # ── 收发 ──

    def _pump_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._lines.put(line)

    def _pump_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self.stderr_tail = (self.stderr_tail + line)[-2000:]

    def request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None:
            raise AppError("MCP 会话已经关闭。", code="mcp_session_closed")
        message_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params}
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise AppError(f"写入 MCP 服务器失败：{exc}", code="mcp_write_failed") from exc
        return self._await_response(message_id, timeout=timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        process = self._process
        if process is None or process.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        with _suppress_all():
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _await_response(self, message_id: int, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(1.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppError(
                    f"MCP 服务器响应超时（{int(timeout)} 秒）。",
                    code="mcp_timeout",
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise AppError(
                    f"MCP 服务器响应超时（{int(timeout)} 秒）。", code="mcp_timeout"
                ) from None
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue  # 服务器往 stdout 打了非 JSON 的日志，跳过
            if message.get("id") != message_id:
                continue  # 通知或其它响应
            if "error" in message:
                error = message.get("error") or {}
                raise AppError(
                    f"MCP 服务器返回错误：{error.get('message') or error}",
                    code="mcp_server_error",
                    details={"mcp_error": error},
                )
            return message.get("result") or {}


class StdioMcpClient:
    """stdio 传输的 MCP 客户端（每次 list/call 起一个会话）。"""

    def __init__(self, capability: Capability) -> None:
        meta = capability.meta or {}
        self.capability_id = capability.id
        self.command = str(meta.get("command") or "").strip()
        raw_args = meta.get("args") or []
        self.args = [str(item) for item in (raw_args if isinstance(raw_args, list) else [raw_args])]
        env = meta.get("env") or {}
        self.env = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        self.cwd = str(meta.get("cwd") or "").strip() or None
        if not self.command:
            raise AppError("stdio 传输必须配置 command。", code="invalid_mcp_server")

    async def list_tools(self) -> list[McpTool]:
        return await _run_in_thread(self._list_tools_sync)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResult:
        return await _run_in_thread(self._call_tool_sync, name, arguments)

    # ── 同步实现（放在线程里跑，避免阻塞事件循环）──

    def _list_tools_sync(self) -> list[McpTool]:
        with _StdioSession(self.command, self.args, self.env, self.cwd) as session:
            _handshake(session)
            result = session.request("tools/list", {}, timeout=INIT_TIMEOUT_SECONDS)
            return _parse_tools(result)

    def _call_tool_sync(self, name: str, arguments: dict[str, Any]) -> McpResult:
        started = time.perf_counter()
        try:
            with _StdioSession(self.command, self.args, self.env, self.cwd) as session:
                _handshake(session)
                result = session.request(
                    "tools/call",
                    {"name": name, "arguments": arguments or {}},
                    timeout=CALL_TIMEOUT_SECONDS,
                )
                text, truncated = clip_result(content_to_text(result.get("content")))
                is_error = bool(result.get("isError"))
                return McpResult(
                    ok=not is_error,
                    tool=name,
                    capability_id=self.capability_id,
                    content=text,
                    # 服务器说 isError 时，把它的说明当错误原因（否则调用方只看到一句"失败"）
                    error=(text or "工具返回 isError=true") if is_error else "",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    truncated=truncated,
                )
        except AppError as exc:
            return McpResult(
                ok=False,
                tool=name,
                capability_id=self.capability_id,
                error=exc.message,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )


class HttpMcpClient:
    """Streamable HTTP 传输的 MCP 客户端（JSON 与 SSE 两种响应都解析）。"""

    def __init__(self, capability: Capability, *, transport: Any = None) -> None:
        meta = capability.meta or {}
        self.capability_id = capability.id
        self.url = str(meta.get("url") or "").strip()
        headers = meta.get("headers") or {}
        self.headers = (
            {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
        )
        self.transport = transport
        if not self.url:
            raise AppError("HTTP 传输必须配置 url。", code="invalid_mcp_server")

    async def list_tools(self) -> list[McpTool]:
        result = await self._request("tools/list", {})
        return _parse_tools(result)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResult:
        started = time.perf_counter()
        try:
            result = await self._request("tools/call", {"name": name, "arguments": arguments or {}})
            text, truncated = clip_result(content_to_text(result.get("content")))
            is_error = bool(result.get("isError"))
            return McpResult(
                ok=not is_error,
                tool=name,
                capability_id=self.capability_id,
                content=text,
                error=(text or "工具返回 isError=true") if is_error else "",
                duration_ms=int((time.perf_counter() - started) * 1000),
                truncated=truncated,
            )
        except AppError as exc:
            return McpResult(
                ok=False,
                tool=name,
                capability_id=self.capability_id,
                error=exc.message,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        session_id = ""
        async with httpx.AsyncClient(
            timeout=CALL_TIMEOUT_SECONDS, transport=self.transport, follow_redirects=True
        ) as client:
            init = await self._post(
                client,
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _init_params()},
                {},
            )
            _, result, session_id = init
            seconds = await self._post(
                client,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"Mcp-Session-Id": session_id} if session_id else {},
            )
            del seconds, result
            response = await self._post(
                client,
                {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
                {"Mcp-Session-Id": session_id} if session_id else {},
            )
            return response[1]

    async def _post(
        self,
        client: httpx.AsyncClient,
        message: dict[str, Any],
        extra_headers: dict[str, str],
    ) -> tuple[int, dict[str, Any], str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
            **extra_headers,
        }
        try:
            response = await client.post(self.url, json=message, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(
                f"访问 MCP 服务器失败：{self.url}（{exc.__class__.__name__}: {exc}）",
                code="mcp_http_failed",
            ) from exc
        session_id = response.headers.get("mcp-session-id", "")
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            payload = _parse_sse(response.text)
        else:
            try:
                payload = response.json()
            except ValueError as exc:
                raise AppError("MCP 服务器返回的不是 JSON。", code="mcp_bad_response") from exc
        if "error" in payload:
            error = payload.get("error") or {}
            raise AppError(
                f"MCP 服务器返回错误：{error.get('message') or error}", code="mcp_server_error"
            )
        return response.status_code, payload.get("result") or {}, session_id


def _init_params() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "orchestrator", "version": "0.1.0"},
    }


def _handshake(session: _StdioSession) -> None:
    session.request("initialize", _init_params(), timeout=INIT_TIMEOUT_SECONDS)
    session.notify("notifications/initialized")


def _parse_tools(result: dict[str, Any]) -> list[McpTool]:
    tools: list[McpTool] = []
    for item in result.get("tools") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        tools.append(
            McpTool(
                name=str(item["name"]),
                description=str(item.get("description") or ""),
                input_schema=item.get("inputSchema") or item.get("input_schema") or {},
            )
        )
    return tools


def _parse_sse(text: str) -> dict[str, Any]:
    """从 SSE 流里取出最后一条带 result/error 的 data 负载。"""

    payload: dict[str, Any] = {}
    for block in (text or "").split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[len("data:") :].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict) and ("result" in message or "error" in message):
                payload = message
    return payload


async def _run_in_thread(func, *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(func, *args)


class _suppress_all:
    """局部用的"忽略所有异常"上下文（关进程时不该再抛）。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return True


def build_client(capability: Capability, *, transport: Any = None) -> McpClient:
    """按 ``meta.transport`` 造客户端（默认 stdio）。"""

    meta = capability.meta or {}
    kind = str(meta.get("transport") or "stdio").lower()
    if kind in ("http", "https", "streamable-http", "sse"):
        return HttpMcpClient(capability, transport=transport)
    return StdioMcpClient(capability)


async def list_tools(capability: Capability, *, transport: Any = None) -> list[McpTool]:
    return await build_client(capability, transport=transport).list_tools()


async def call_tool(
    capability: Capability, tool: str, arguments: dict[str, Any], *, transport: Any = None
) -> McpResult:
    return await build_client(capability, transport=transport).call_tool(tool, arguments)


def server_dir_for(root: Path, capability_id: str) -> Path:
    """MCP 服务器自己的数据目录（需要落盘的服务器可以用它当 cwd）。"""

    path = Path(root) / "mcp" / capability_id
    path.mkdir(parents=True, exist_ok=True)
    return path
