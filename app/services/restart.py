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
from pathlib import Path
from typing import Any

from app.core.errors import AppError

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)

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


def spawn_helper(project_root: Path, request_file: Path) -> None:
    """以"脱离当前进程"的方式启动辅助脚本：它活得比自己久。"""

    script = Path(project_root) / HELPER_SCRIPT
    if not script.is_file():
        raise AppError(
            f"找不到重启辅助脚本：{script}（更新到最新版本后再试）",
            code="restart_unsupported",
        )
    command = [
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
    try:
        subprocess.Popen(  # noqa: S603 - 固定参数，无 shell
            command,
            cwd=str(project_root),
            creationflags=_NO_WINDOW | _DETACHED,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AppError(f"无法启动重启脚本：{exc}", code="restart_failed") from exc


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
    spawn_helper(project_root, request_file)
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
