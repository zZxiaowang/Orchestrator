"""应用入口：装配 FastAPI、静态界面与全局错误处理。"""

from __future__ import annotations

import hashlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router
from app.core.catalog import CatalogStore
from app.core.config import (
    DATA_DIR,
    RUNS_DIR,
    WEB_DIR,
    Settings,
    SettingsStore,
    get_settings,
    settings_store,
)
from app.core.errors import (
    AppError,
    ConfigurationError,
    NotFoundError,
    RelayError,
    WorkspaceError,
)
from app.core.logging import configure_logging, get_logger
from app.core.plugins import PluginStore
from app.services.events import EventBus
from app.services.orchestrator import Orchestrator
from app.services.storage import RunStore

logger = get_logger("app.main")

STATUS_BY_ERROR: tuple[tuple[type[AppError], int], ...] = (
    (NotFoundError, 404),
    (ConfigurationError, 400),
    (WorkspaceError, 400),
    (RelayError, 502),
)

#: 按错误码给出的更精确状态码（覆盖"按类型"的默认值）：
#: 这类失败不是请求本身写错了，而是**当前状态不允许**——409 更贴切。
STATUS_BY_CODE: dict[str, int] = {
    "run_busy": 409,
}


def _status_for(exc: AppError) -> int:
    if exc.code in STATUS_BY_CODE:
        return STATUS_BY_CODE[exc.code]
    for error_type, status in STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    # AppError 是我们自己的领域错误：默认按"客户端可修正"处理（400）。
    # 真正的服务端异常不是 AppError，会走 FastAPI 默认的 500。
    return 400


def create_app(
    *,
    transport=None,
    settings_provider=None,
    runs_dir=None,
    web_dir=None,
    settings_store_override: SettingsStore | None = None,
    plugins_dir=None,
) -> FastAPI:
    """构建应用。测试可注入 ``transport`` 与 ``settings_provider``。"""
    provider = settings_provider or get_settings
    store_dir = runs_dir or RUNS_DIR
    static_dir = web_dir or WEB_DIR
    settings_repo = settings_store_override or settings_store
    plugins_root = Path(plugins_dir) if plugins_dir else (DATA_DIR / "plugins")
    asset_version = _asset_version(static_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings: Settings = provider()
        configure_logging(settings.log_level)
        settings.validate_runtime()
        store_dir.mkdir(parents=True, exist_ok=True)

        app.state.settings_provider = provider
        app.state.settings_store = settings_repo
        app.state.relay_transport = transport
        app.state.plugin_store = PluginStore(plugins_root)
        app.state.catalog_store = CatalogStore(plugins_root)
        app.state.asset_version = asset_version
        app.state.bus = EventBus()
        app.state.orchestrator = Orchestrator(
            RunStore(store_dir),
            app.state.bus,
            settings_provider=provider,
            transport=transport,
        )
        # 上次进程被杀/重启时留下的"执行中"运行，启动时统一收敛为"已暂停"
        recovered = app.state.orchestrator.recover_interrupted()
        if recovered:
            logger.warning("已把 %d 个被中断的运行标记为暂停，可在界面点「继续执行」。", recovered)

        missing = settings.missing_endpoints()
        problems = settings.config_problems()
        for problem in problems:
            logger.warning("配置待修正：%s", problem)
        if missing:
            logger.warning(
                "以下端点尚未配置，界面会提示补全：%s",
                ", ".join(f"{e.label}({e.role})" for e in missing),
            )
        else:
            logger.info(
                "架构段=%s 执行段=%s",
                settings.resolve_architect().model,
                settings.resolve_editor().model,
            )
        yield

    app = FastAPI(
        title="架构-执行双模型编排器",
        description="GPT 产出纲领，DeepSeek V4 按纲领执行。",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=_status_for(exc), content={"error": exc.as_dict()})

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        """首页：注入资源版本号，避免浏览器复用旧缓存（改完前端不必强刷）。"""
        path = Path(static_dir) / "index.html"
        if not path.is_file():
            return HTMLResponse(
                "<h1>Orchestrator</h1><p>未找到 web/index.html</p>", status_code=404
            )
        html = path.read_text(encoding="utf-8")
        html = html.replace("__APP_VERSION__", __version__)
        html = html.replace("__ASSET_VERSION__", asset_version)
        return HTMLResponse(html)

    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app


def _asset_version(static_dir) -> str:
    digest = hashlib.sha256()
    directory = Path(static_dir)
    for name in ("index.html", "styles.css", "app.js"):
        file = directory / name
        if file.is_file():
            digest.update(file.read_bytes())
    return digest.hexdigest()[:8]


app = create_app()


def main() -> None:
    """命令行 / 打包后的入口。

    打包（PyInstaller）时必须直接传 app 对象而不是 "app.main:app" 字符串：
    字符串形式会触发"按模块名重新导入"，在冻结环境里容易走到错误的模块路径。
    """
    import threading
    import webbrowser

    import uvicorn

    from app.core.config import DATA_DIR, is_frozen

    _force_utf8_console()
    settings = get_settings()
    url = f"http://{settings.host}:{settings.port}"
    # 打包成"无控制台"的桌面版时 sys.stdout 为 None，print 会抛异常，统一兜底
    for line in (
        "=" * 62,
        "  架构-执行双模型编排器",
        f"  界面地址：{url}",
        f"  数据目录：{DATA_DIR}",
        "  停止服务：在本窗口按 Ctrl+C（或关闭窗口）",
        "=" * 62,
    ):
        _safe_print(line)

    if is_frozen():
        # 打包版通常是双击启动：稍等片刻自动打开浏览器
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=None,  # 用我们的日志配置，避免打包后找不到 logging 配置文件
    )
    uvicorn.Server(config).run()


def _force_utf8_console() -> None:
    """让打包版在 Windows 控制台（默认 GBK）里也能正确显示中文。

    冻结后没有 ``PYTHONUTF8`` 兜底，中文日志会变成乱码，所以这里主动把
    控制台代码页与标准流都切到 UTF-8。
    """
    import os
    import sys
    from contextlib import suppress

    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # noqa: F821 - Windows only
            ctypes.windll.kernel32.SetConsoleCP(65001)  # noqa: F821 - Windows only
        except Exception:  # noqa: BLE001 - 无控制台（重定向）时忽略
            pass
    for stream in (sys.stdout, sys.stderr):
        with suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _safe_print(message: str) -> None:
    """没有控制台（windowed 打包）时静默跳过，避免输出把程序搞崩。"""
    try:
        if sys.stdout is not None:
            print(message)
    except (AttributeError, ValueError, OSError):
        pass


if __name__ == "__main__":
    main()
