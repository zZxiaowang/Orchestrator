"""重新打包 + 重启自己。

为什么不能让程序自己干这件事：

* 打包第一步是结束 ``Orchestrator.exe``（Windows 上正在运行的文件会被锁住，
  PyInstaller 覆盖不了），而"结束自己"之后的代码根本不会继续执行；
* 所以这里只做三件事：**写重启请求 → 交给外部辅助脚本 → 自己退出**。
  等待进程退出、重新打包、拉起新实例都由 ``scripts/restart.ps1`` 完成。

不丢数据：运行记录在每次状态变化时都已落盘；新实例启动时会执行
``recover_interrupted()``，把中断的运行收敛成"已暂停"，用户可以点「继续执行」接着跑。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.core.errors import AppError

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)
#: 脱离父进程的 Job Object。
#: PyInstaller 单文件版会把子进程放进一个 Job，父进程一退出就把同 Job 的子进程一起杀掉——
#: 这正是"点了重启、窗口没了、程序也没起来"的原因。带这个标志创建的子进程可以活下来。
_BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)

#: 等待辅助脚本"报到"的最长时间（它会先写一行日志）。
#: 直接启动等待 6 秒（典型冷启动 1~3 秒）；改用 WMI 兜底时再等 10 秒。
HELPER_SIGNAL_TIMEOUT = 6.0
HELPER_SIGNAL_TIMEOUT_WMI = 10.0
#: 辅助脚本启动后写的第一行标记
HELPER_SIGNAL_MARK = "restart requested"

#: 辅助脚本相对项目根的位置
HELPER_SCRIPT = Path("scripts") / "restart.ps1"


def request_path(data_dir: Path) -> Path:
    return Path(data_dir) / "restart-request.json"


def build_request(
    *,
    data_dir: Path,
    project_root: Path,
    frozen: bool,
    rebuild: bool,
    host: str,
    port: int,
) -> dict[str, Any]:
    """描述"重启成什么样"：辅助脚本照着它拉起新实例。"""

    if frozen:
        launch_file = str(Path(sys.executable))
        launch_args: list[str] = []
    else:
        launch_file = str(sys.executable)
        launch_args = ["-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)]
    return {
        "pid": os.getpid(),
        "frozen": bool(frozen),
        "rebuild": bool(rebuild),
        "package_script": str(Path(project_root) / "scripts" / "package.ps1"),
        "log_file": str(Path(data_dir) / "logs" / "restart.log"),
        "launch_file": launch_file,
        "launch_args": launch_args,
        "cwd": str(project_root),
        "env": {"ORCHESTRATOR_DATA_DIR": str(data_dir)},
    }


def write_request(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = request_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def helper_available(project_root: Path) -> bool:
    return (Path(project_root) / HELPER_SCRIPT).is_file()


def helper_command(project_root: Path, request_file: Path) -> list[str]:
    """辅助脚本的启动命令（固定参数，不经过 shell）。"""

    script = Path(project_root) / HELPER_SCRIPT
    return [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script),
        "-Request",
        str(request_file),
    ]


def spawn_helper(project_root: Path, request_file: Path) -> None:
    """以"脱离当前进程与 Job"的方式启动辅助脚本：它必须活得比自己久。"""

    script = Path(project_root) / HELPER_SCRIPT
    if not script.is_file():
        raise AppError(
            f"找不到重启辅助脚本：{script}（更新到最新版本后再试）",
            code="restart_unsupported",
        )
    command = helper_command(project_root, request_file)
    last_error = ""
    for flags, label in (
        (_NO_WINDOW | _DETACHED | _BREAKAWAY, "breakaway"),
        (_NO_WINDOW | _DETACHED, "detached"),
    ):
        try:
            subprocess.Popen(  # noqa: S603 - 固定参数，无 shell
                command,
                cwd=str(project_root),
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError as exc:
            last_error = f"{label}: {exc}"
            continue
    raise AppError(f"无法启动重启脚本：{last_error}", code="restart_failed")


def spawn_helper_via_wmi(project_root: Path, request_file: Path) -> None:
    """兜底：让 Windows 自己在 Job 之外创建进程。

    有些环境不允许 BREAKAWAY（Job 没开 BREAKAWAY_OK）。这时用 WMI 创建：
    进程由系统服务拉起，不属于我们的 Job，父进程退出也影响不到它。
    """

    command = " ".join(
        f'"{part}"' if " " in part else part for part in helper_command(project_root, request_file)
    )
    escaped = command.replace("'", "''")
    script = f"(Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{escaped}'}}).ProcessId"
    try:
        completed = subprocess.run(  # noqa: S603 - 固定参数，不经过 shell
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppError(f"无法通过 WMI 启动重启脚本：{exc}", code="restart_failed") from exc
    if completed.returncode != 0:
        raise AppError(
            f"无法通过 WMI 启动重启脚本：{(completed.stderr or completed.stdout or '').strip()[:200]}",
            code="restart_failed",
        )


def helper_signal_count(log_file: Path) -> int:
    """辅助脚本"报到"了几次（日志里出现几行 restart requested）。

    用**计数**而不是"文件字节偏移"：日志里有中文，字节长度与字符长度不同，
    拿字节偏移去切字符串会切错位置——曾经因此把"已经起来了"误判成"没起来"。
    """

    path = Path(log_file)
    if not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return text.count(HELPER_SIGNAL_MARK)


def helper_signaled(log_file: Path, *, minimum: int = 1) -> bool:
    """辅助脚本是否至少报到过 ``minimum`` 次。"""

    return helper_signal_count(log_file) >= max(1, int(minimum))


def wait_for_helper(
    log_file: Path, *, minimum: int = 1, timeout: float = HELPER_SIGNAL_TIMEOUT
) -> bool:
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        if helper_signaled(log_file, minimum=minimum):
            return True
        time.sleep(0.25)
    return False


def perform_restart(
    *,
    data_dir: Path,
    project_root: Path,
    rebuild: bool,
    host: str,
    port: int,
) -> dict[str, Any]:
    """写好请求、拉起辅助脚本，并返回给界面的说明（真正的退出由调用方安排）。"""

    from app.core.config import is_frozen

    payload = build_request(
        data_dir=data_dir,
        project_root=project_root,
        frozen=is_frozen(),
        rebuild=rebuild,
        host=host,
        port=port,
    )
    request_file = write_request(data_dir, payload)
    log_file = Path(str(payload["log_file"]))
    # 记下"这次之前报到过几次"，之后要求计数增加——比字节偏移可靠
    seen_before = helper_signal_count(log_file)

    spawn_helper(project_root, request_file)
    if not wait_for_helper(log_file, minimum=seen_before + 1, timeout=HELPER_SIGNAL_TIMEOUT):
        # 直接启动没成功（多半是被父进程的 Job 一起带走了）：改用 WMI 在 Job 之外拉起
        spawn_helper_via_wmi(project_root, request_file)
        if not wait_for_helper(
            log_file, minimum=seen_before + 1, timeout=HELPER_SIGNAL_TIMEOUT_WMI
        ):
            # 放弃之前把请求文件撤掉：免得辅助脚本晚一步醒来，
            # 在 60 秒后把还活着的我们杀掉（那对用户是"莫名其妙被重启"）
            request_file.unlink(missing_ok=True)
            raise AppError(
                "重启脚本没能启动，当前程序先不退出了（以免你失去界面）。"
                f"可以手动重启：关闭本程序，然后运行 {project_root}\\start-client.cmd",
                code="restart_failed",
            )
    return {
        "request": str(request_file),
        "log": str(payload["log_file"]),
        "rebuild": bool(rebuild),
        "frozen": bool(payload["frozen"]),
        "detail": (
            "正在重新打包并重启：当前窗口几秒内会关闭，完成后会自动打开新版本。"
            if rebuild
            else "正在重启：当前窗口几秒内会关闭，完成后会自动重新打开。"
        ),
    }
