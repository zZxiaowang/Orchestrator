"""PyInstaller 打包入口。

默认走**桌面客户端**（原生窗口），传 ``--server`` 则退回"服务器 + 浏览器"形态。
两种形态共用同一套后端与界面，只是外壳不同。
"""

from __future__ import annotations

import multiprocessing
import sys

from app.desktop import main as desktop_main

if __name__ == "__main__":
    # 打包后若出现子进程（如未来启用 worker），避免 Windows 上重复启动
    multiprocessing.freeze_support()
    raise SystemExit(desktop_main(sys.argv[1:]))
