"""受控命令执行：只跑白名单里的命令，只在工作区内跑。

为什么需要它：执行段写完文件之后**不知道自己写得对不对**。没有"跑一下测试、看报错、
再改"这一步，所谓"自开发"就只是盲写。这里把这件事收进受控边界内：

安全约定（缺一不可）：

1. **白名单前缀匹配**：只有 ``command_allowlist`` 里列出的前缀能被执行；
2. **不经过 shell**：命令拆成参数列表执行，``|``、``&``、``>``、``%VAR%`` 等一律拒绝；
3. **只在工作区内执行**：``cwd`` 固定为运行的工作区根目录；
4. **超时 + 输出截断**：默认 120 秒，保留头尾各 2000 字符（报错通常在尾部）；
5. **默认关闭**：``allow_command_execution`` 默认 false，开启是用户的显式动作。

不做的事：不做沙箱、不做任意命令执行、不跑 ``rm``/``git push`` 这类危险命令——
白名单是唯一的准入方式，用户可以随时把某条命令删掉。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: 隐藏子进程窗口（Windows）：桌面版没有控制台，否则每跑一条命令都会弹黑窗
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 允许出现在命令里的字符白名单之外的都拒绝（不经过 shell，但仍然挡住链式命令）
_SHELL_DANGER = re.compile(r"[|&;<>$`^%!]|&&|\|\||\r|\n")

#: 输出截断：头尾各留多少字符
HEAD_CHARS = 2000
TAIL_CHARS = 2000

#: 单条命令的输出上限（防止一条命令刷爆事件与 run.json）
MAX_OUTPUT_CHARS = 20000


@dataclass
class CommandResult:
    """一条命令的执行结果（会随步骤落盘，供界面与报告展示）。"""

    cmd: str
    ok: bool = False
    skipped: bool = False
    exit_code: int | None = None
    duration_ms: int = 0
    output: str = ""
    truncated: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "cmd": self.cmd,
            "ok": self.ok,
            "skipped": self.skipped,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "truncated": self.truncated,
            "error": self.error,
        }


def normalize_allowlist(values: Iterable[str] | str | None) -> list[str]:
    """把配置里的白名单收敛成干净的样式：去空白、去空行、去重、保序。"""

    if values is None:
        return []
    if isinstance(values, str):
        raw = re.split(r"[\r\n]+", values)
    else:
        raw = [str(item) for item in values]
    out: list[str] = []
    for item in raw:
        text = " ".join(str(item).split())
        if text and text not in out:
            out.append(text)
    return out


def _tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=False)
    except ValueError:
        return []


def check_command(cmd: str, allowlist: Sequence[str] | None, *, enabled: bool) -> str:
    """返回空字符串 = 可以执行；否则返回拒绝原因（给用户看的中文说明）。"""

    text = " ".join((cmd or "").split())
    if not text:
        return "命令为空。"
    if not enabled:
        return "命令执行未开启（设置里打开「允许执行验证命令」，并把命令加入白名单）。"
    if _SHELL_DANGER.search(text):
        return "命令里含有被禁止的 shell 特殊字符（| & ; > < $ ` % ! 换行），已拒绝。"
    entries = normalize_allowlist(allowlist)
    if not entries:
        return "白名单为空：请先在设置里填写允许执行的命令前缀。"
    for entry in entries:
        if text == entry or text.startswith(entry + " "):
            return ""
    return f"命令不在白名单内：{text[:80]}（白名单：{'、'.join(entries[:5])}）"


def run_command(
    cmd: str,
    *,
    cwd: Path,
    allowlist: Sequence[str] | None,
    enabled: bool,
    timeout: float = 120.0,
) -> CommandResult:
    """执行一条白名单内的命令。被拒绝/超时/异常都会返回结构化结果，不抛给调用方。"""

    text = " ".join((cmd or "").split())
    reason = check_command(text, allowlist, enabled=enabled)
    if reason:
        return CommandResult(cmd=text, ok=False, skipped=True, error=reason)

    argv = _tokens(text)
    if not argv:
        return CommandResult(cmd=text, ok=False, skipped=True, error="命令无法解析。")

    started = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 - 参数列表执行，不经过 shell
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout)),
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return CommandResult(
            cmd=text,
            ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"找不到可执行文件：{argv[0]}",
        )
    except subprocess.TimeoutExpired as exc:
        partial = _as_text(exc.stdout) + _as_text(exc.stderr)
        return CommandResult(
            cmd=text,
            ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            output=_clip_output(partial),
            error=f"命令超时（>{int(timeout)} 秒），已终止。",
        )
    except OSError as exc:
        return CommandResult(cmd=text, ok=False, error=f"命令执行失败：{exc}")

    combined = (completed.stdout or "") + (completed.stderr or "")
    output, truncated = _clip_output(combined)
    return CommandResult(
        cmd=text,
        ok=completed.returncode == 0,
        exit_code=completed.returncode,
        duration_ms=int((time.perf_counter() - started) * 1000),
        output=output,
        truncated=truncated,
    )


def run_allowed(
    commands: Sequence[str],
    *,
    cwd: Path,
    allowlist: Sequence[str] | None,
    enabled: bool,
    timeout: float = 120.0,
    limit: int = 4,
) -> list[CommandResult]:
    """按顺序跑多条命令（同一工作区，串行；一条失败不打断后续）。"""

    results: list[CommandResult] = []
    for cmd in list(commands)[: max(0, limit)]:
        results.append(
            run_command(cmd, cwd=cwd, allowlist=allowlist, enabled=enabled, timeout=timeout)
        )
    return results


def failure_block(results: Sequence[CommandResult], *, max_chars: int = 4000) -> str:
    """把失败的命令拼成回灌给执行段的说明（这是"按报错再改"的关键输入）。"""

    failed = [item for item in results if not item.ok]
    if not failed:
        return ""
    chunks: list[str] = []
    for item in failed:
        detail = item.error or f"退出码 {item.exit_code}"
        body = item.output[-TAIL_CHARS:] if item.output else "（没有输出）"
        chunks.append(f"### $ {item.cmd}\n{detail}\n```\n{body}\n```")
    text = "## 系统已经执行过这些命令，但**没有通过**\n" + "\n\n".join(chunks)
    return text[:max_chars]


def _clip_output(text: str) -> tuple[str, bool]:
    raw = text or ""
    if len(raw) > MAX_OUTPUT_CHARS:
        raw = raw[:MAX_OUTPUT_CHARS]
    if len(raw) <= HEAD_CHARS + TAIL_CHARS:
        return raw, len(text or "") > MAX_OUTPUT_CHARS
    skipped = len(raw) - HEAD_CHARS - TAIL_CHARS
    clipped = f"{raw[:HEAD_CHARS]}\n…（省略 {skipped} 字符）…\n{raw[-TAIL_CHARS:]}"
    return clipped, True


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")
