# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用法（推荐走 scripts/package.ps1）：
    set ORCHESTRATOR_PACKAGE_ONEDIR=0 && python -m PyInstaller packaging/orchestrator.spec
    set ORCHESTRATOR_PACKAGE_ONEDIR=1 && python -m PyInstaller packaging/orchestrator.spec

要点：
* ``web/`` 与 ``.env.example`` 作为数据文件一起打包（冻结后由 config 解析到 _MEIPASS）；
* uvicorn / httpx / anyio 的部分实现是运行时动态导入，必须写进 hiddenimports；
* **不要**把 data 目录打进去：数据必须落在用户可写的位置（见 app/core/config.py）。
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
ONEDIR = os.environ.get("ORCHESTRATOR_PACKAGE_ONEDIR", "0") == "1"

HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
    "httpx._transports.default",
    "email.mime.multipart",
    "email.mime.text",
    # 桌面客户端（WebView2 原生窗口）
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr",
    "clr_loader",
]

# 桌面版默认"无控制台"（更像客户端）；打包脚本传 --console 时保留控制台便于排查
CONSOLE = os.environ.get("ORCHESTRATOR_PACKAGE_CONSOLE", "0") == "1"

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "exe_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "web"), "web"),
        (str(ROOT / ".env.example"), "."),
    ],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "polars", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

if ONEDIR:
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="Orchestrator",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=CONSOLE,
        disable_windowed_traceback=False,
        version=str(ROOT / "packaging" / "version_info.txt"),
    )
    COLLECT(  # noqa: F821
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="Orchestrator",
    )
else:
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="Orchestrator",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=CONSOLE,
        disable_windowed_traceback=False,
        version=str(ROOT / "packaging" / "version_info.txt"),
    )
