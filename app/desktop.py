"""桌面客户端入口：把同一个后端装进**原生窗口**（不是浏览器标签页）。

和 Codex 这类客户端一致的形态：

* 双击即出一个独立应用窗口（Windows 用系统 WebView2 渲染），没有地址栏、没有标签页；
* 后端（FastAPI/uvicorn）跑在后台线程，只监听本机回环地址；
* 关掉窗口 = 退出程序，后端线程随之停止；
* 端口默认自动挑一个空闲端口，避免和已在运行的服务实例抢端口。

仍然保留"服务器 + 浏览器"形态：``python -m app.main`` 或 ``Orchestrator.exe --server``。
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import threading
import time
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from app.core import config as config_module
from app.core.config import get_settings

logger = logging.getLogger("app.desktop")

# ---------------------------------------------------------------------------
# 左侧栏信息架构（第 3 步交付）：一级入口只有「普通对话」与「项目」
#
# 上位契约：docs/project-navigation-contract.md。架构 / 计划 / 执行 / 步骤 / 验证 / 日志 /
# 设置全部下沉到「项目」内部二级模块，不再作为左侧栏一级入口出现。
# ---------------------------------------------------------------------------

#: 左侧栏一级区域的**全部**内容，顺序固定。
SIDEBAR_PRIMARY_ENTRIES: tuple[dict[str, str], ...] = (
    {"id": "chat", "label": "普通对话", "route": "#/chat"},
    {"id": "projects", "label": "项目", "route": "#/projects"},
)

#: 项目内部二级模块：只有选中项目之后才渲染，普通对话上下文里一律隐藏。
PROJECT_SECONDARY_MODULES: tuple[dict[str, str], ...] = (
    {"id": "overview", "label": "概览"},
    {"id": "architecture", "label": "架构"},
    {"id": "plan", "label": "计划"},
    {"id": "execution", "label": "执行"},
    {"id": "verification", "label": "验证"},
    {"id": "logs", "label": "日志"},
    {"id": "settings", "label": "设置"},
)

#: 普通对话上下文里必须藏起来的工程概念（与二级模块同名）。
PROJECT_ONLY_SECTIONS: tuple[str, ...] = tuple(item["id"] for item in PROJECT_SECONDARY_MODULES)

#: 页面侧渲染脚本：把两级导航应用到已加载的界面上。
#: 一级区域只画「普通对话」与「项目」；二级区域只有选中项目后才画，
#: 普通对话工作区既不画也不返回任何项目执行控制。
SIDEBAR_NAVIGATION_JS = """
(() => {
  const PRIMARY = __PRIMARY__;
  const PROJECT_MODULES = __MODULES__;

  const sidebar = document.getElementById('sidebar')
    || document.querySelector('.sidebar')
    || document.querySelector('aside');
  if (!sidebar) { return { ok: false, reason: 'sidebar-not-found' }; }

  // 一级区域只保留「普通对话」与「项目」。
  let primaryHost = sidebar.querySelector('[data-nav-primary]');
  if (!primaryHost) {
    primaryHost = document.createElement('nav');
    primaryHost.className = 'nav-primary-group';
    primaryHost.setAttribute('data-nav-primary', '');
    sidebar.prepend(primaryHost);
  }
  primaryHost.innerHTML = PRIMARY.map((item) =>
    '<a class="nav-primary" data-nav="' + item.id + '" href="' + item.route + '">' + item.label + '</a>'
  ).join('');

  const hash = location.hash || '#/chat';
  const projectMatch = hash.match(/#\\/projects\\/([^\\/?#]+)/);
  const selectedProjectId = projectMatch ? decodeURIComponent(projectMatch[1]) : null;
  const inChat = !hash.startsWith('#/projects');

  // 二级区域只属于项目：普通对话里不渲染。
  let moduleHost = sidebar.querySelector('[data-project-modules]');
  if (!moduleHost) {
    moduleHost = document.createElement('nav');
    moduleHost.className = 'nav-project-modules';
    moduleHost.setAttribute('data-project-modules', '');
    sidebar.appendChild(moduleHost);
  }
  moduleHost.hidden = inChat || !selectedProjectId;
  moduleHost.dataset.project = selectedProjectId || '';
  if (moduleHost.hidden) {
    moduleHost.innerHTML = '';
    moduleHost.dataset.active = '';
  } else {
    const current = hash.split('?')[0].split('/').pop();
    moduleHost.innerHTML = PROJECT_MODULES.map((item) =>
      '<a class="nav-module" data-module="' + item.id + '" href="#/projects/'
        + selectedProjectId + '/' + item.id + '">' + item.label + '</a>'
    ).join('');
    moduleHost.dataset.active = PROJECT_MODULES.some((m) => m.id === current) ? current : 'overview';
  }

  // 兼容历史标记：把残留的工程概念一级入口摘掉。
  sidebar.querySelectorAll('.nav-primary').forEach((el) => {
    if (!PRIMARY.some((item) => item.id === el.dataset.nav)) { el.remove(); }
  });

  return {
    ok: true,
    primary: PRIMARY.map((item) => item.id),
    visibleModules: moduleHost.hidden ? [] : PROJECT_MODULES.map((item) => item.id),
    selectedProjectId: selectedProjectId,
    workspace: inChat ? 'chat' : 'projects'
  };
})();
""".replace("__PRIMARY__", json.dumps(list(SIDEBAR_PRIMARY_ENTRIES), ensure_ascii=False)).replace(
    "__MODULES__", json.dumps(list(PROJECT_SECONDARY_MODULES), ensure_ascii=False)
)


def sidebar_primary_entries() -> list[dict[str, str]]:
    """左侧栏一级入口。除返回值里的两项之外，任何东西都不算一级入口。"""
    return [dict(item) for item in SIDEBAR_PRIMARY_ENTRIES]


def project_secondary_modules() -> list[dict[str, str]]:
    """项目内部二级模块（选中项目后才可见）。"""
    return [dict(item) for item in PROJECT_SECONDARY_MODULES]


def apply_sidebar_navigation(window) -> dict[str, object]:
    """把两级导航应用到已加载的窗口，并回读页面侧探针结果。"""
    try:
        result = window.evaluate_js(SIDEBAR_NAVIGATION_JS)
    except Exception as exc:  # pragma: no cover - 需要真实窗口
        logger.warning("[导航] 注入左侧栏失败：%s", exc)
        return {"ok": False, "reason": str(exc)}
    if not isinstance(result, dict):
        logger.warning("[导航] 左侧栏探针返回异常：%r", result)
        return {"ok": False, "reason": "bad-probe"}
    expected = [item["id"] for item in SIDEBAR_PRIMARY_ENTRIES]
    if list(result.get("primary") or []) != expected:
        logger.warning("[导航] 一级入口不是 %s：%s", expected, result.get("primary"))
    return result


WINDOW_TITLE = "Orchestrator · 架构-执行双模型编排器"
DEFAULT_WIDTH = 1360
DEFAULT_HEIGHT = 880
MIN_SIZE = (1000, 640)

#: 在真实窗口里逐个点击关键按钮，验证"点了到底有没有反应"
CLICK_PROBE_JS = """
(() => {
  const errors = [];
  const results = {};
  const click = (id) => {
    const el = document.getElementById(id);
    if (!el) { results[id] = 'missing'; return false; }
    try { el.click(); results[id] = 'ok'; return true; }
    catch (e) { errors.push(id + ': ' + String(e && e.message)); results[id] = 'threw'; return false; }
  };
  const hidden = (id) => {
    const el = document.getElementById(id);
    return el ? el.hidden : null;
  };

  click('market-btn');
  results.marketOpened = hidden('market-modal') === false;
  click('market-close');
  results.marketClosed = hidden('market-modal') === true;

  click('settings-btn');
  results.settingsOpened = hidden('settings-modal') === false;
  click('settings-cancel');
  results.settingsClosed = hidden('settings-modal') === true;

  click('sidebar-toggle');
  results.sidebarCollapsed = document.getElementById('sidebar').dataset.wide === 'false';
  click('sidebar-toggle');
  results.sidebarRestored = document.getElementById('sidebar').dataset.wide === 'true';

  click('updates-btn');
  // 命令面板：事件要派发到 document（派发到 window 不会触发 document 上的监听器）
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
  results.paletteOpened = hidden('palette-modal') === false;
  const paletteItems = document.querySelectorAll('#palette-list .palette-item').length;
  results.paletteItems = paletteItems;
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  results.paletteClosed = hidden('palette-modal') === true;

  results.runItems = document.querySelectorAll('.run-item').length;
  results.copyButtons = document.querySelectorAll('.copy-btn').length;
  return { errors, results };
})()
"""


def pick_free_port(host: str = "127.0.0.1") -> int:
    """让操作系统分配一个空闲端口（避免与其他实例冲突）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wait_for_server(url: str, *, timeout: float = 25.0, interval: float = 0.25) -> bool:
    """等后端可访问（窗口先出现也不会白屏）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310 - 只访问本机回环地址
                if response.status == 200:
                    return True
        except (URLError, OSError, ValueError):
            time.sleep(interval)
    return False


def configure_logging(log_dir: Path, *, level: str = "INFO") -> None:
    """桌面版没有控制台：日志写文件（同时保留 stderr，便于命令行调试）。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_dir / "orchestrator.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def start_backend(host: str, port: int) -> tuple[object, threading.Thread]:
    """在后台线程启动 uvicorn，返回 (server, thread)。"""
    import uvicorn

    from app.main import app

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        log_config=None,
        access_log=False,  # 桌面客户端不需要刷访问日志
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="orchestrator-backend", daemon=True)
    thread.start()
    return server, thread


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="架构-执行双模型编排器（桌面客户端）",
    )
    parser.add_argument("--host", default="", help="监听地址，默认取配置（127.0.0.1）")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="固定端口；默认自动挑选空闲端口，避免与已运行实例冲突",
    )
    parser.add_argument("--debug", action="store_true", help="打开窗口开发者工具")
    parser.add_argument(
        "--selftest",
        type=float,
        nargs="?",
        const=6.0,
        default=0.0,
        metavar="SECONDS",
        help="自检模式：开窗后等待若干秒再自动关闭并输出结果（用于打包验证）",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="改用「服务器 + 浏览器」形态（等同 python -m app.main）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()

    if args.server:
        from app.main import main as server_main

        server_main()
        return 0

    data_dir = config_module.DATA_DIR  # 惰性读取：便于测试/打包注入
    configure_logging(data_dir / "logs", level=settings.log_level)
    host = args.host or settings.host
    port = args.port or pick_free_port(host)
    url = f"http://{host}:{port}"

    server, _thread = start_backend(host, port)
    if not wait_for_server(f"{url}/api/v1/health"):
        logger.error("后端未能在预期时间内启动，退出。")
        return 2
    logger.info("桌面客户端已就绪：%s（数据目录 %s）", url, data_dir)

    try:
        import webview
    except ImportError:  # pragma: no cover - 未安装桌面依赖
        logger.error(
            "未安装桌面依赖（pywebview）。可执行：pip install pywebview pythonnet；"
            "或用 --server 以浏览器形态启动。"
        )
        server.should_exit = True  # type: ignore[attr-defined]
        return 3

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        min_size=MIN_SIZE,
        background_color="#ffffff",
        text_select=True,
    )

    if args.selftest:
        _schedule_selftest(window, url, seconds=float(args.selftest))

    exit_code = 0
    try:
        webview.start(debug=args.debug)
    except Exception as exc:  # noqa: BLE001 - 例如缺少 WebView2 运行时
        logger.error("窗口启动失败：%s", exc)
        logger.error("可安装 Microsoft Edge WebView2 Runtime 后重试，或改用 --server 形态。")
        exit_code = 4
    finally:
        server.should_exit = True  # type: ignore[attr-defined]
        with suppress(Exception):
            _thread.join(timeout=10)
        logger.info("已退出。")
    if config_module.is_frozen():
        # 打包版：确保双击关闭窗口后进程立刻结束，不被残留的非守护线程拖住
        import os

        os._exit(exit_code)
    return exit_code


def _schedule_selftest(window, url: str, *, seconds: float) -> None:
    """自检：确认后端可用 + 窗口已创建 + **界面真的渲染出来了**，然后自动关闭。"""

    def worker() -> None:
        ok = wait_for_server(f"{url}/api/v1/health", timeout=15)
        logger.info("[自检] 后端健康检查：%s", "PASS" if ok else "FAIL")
        logger.info("[自检] 窗口对象：%s", "PASS" if window is not None else "FAIL")

        # 等页面加载完，再问一次窗口里的 DOM：确认不是白屏
        rendered = False
        deadline = time.time() + 15
        while time.time() < deadline:
            with suppress(Exception):
                rendered = bool(
                    window.evaluate_js(
                        "Boolean(document.querySelector('.app') && document.getElementById('sidebar'))"
                    )
                )
            if rendered:
                break
            time.sleep(0.3)
        logger.info("[自检] 窗口内界面渲染：%s", "PASS" if rendered else "FAIL")
        if not rendered:
            logger.error("[自检] 窗口里没有渲染出界面（可能 WebView2 加载失败）。")

        clicks_ok = False
        if rendered:
            # 等任务列表加载完再点，避免"探针比界面快"造成误判
            deadline_runs = time.time() + 10
            while time.time() < deadline_runs:
                with suppress(Exception):
                    if window.evaluate_js(
                        "document.querySelectorAll('.run-item').length"
                    ) or window.evaluate_js("Boolean(document.querySelector('.run-search'))"):
                        break
                time.sleep(0.3)
            probe = window.evaluate_js(CLICK_PROBE_JS) or {}
            results = probe.get("results", {}) if isinstance(probe, dict) else {}
            errors = probe.get("errors", []) if isinstance(probe, dict) else []
            for name, value in results.items():
                logger.info("[自检] 点击探针 %s = %s", name, value)
            for item in errors:
                logger.error("[自检] 点击抛出异常：%s", item)
            clicks_ok = (
                results.get("marketOpened") is True
                and results.get("marketClosed") is True
                and results.get("settingsOpened") is True
                and results.get("settingsClosed") is True
                and results.get("sidebarCollapsed") is True
                and results.get("sidebarRestored") is True
                and results.get("paletteOpened") is True
                and results.get("paletteClosed") is True
                and (results.get("paletteItems") or 0) > 0
                and not errors
            )
            logger.info("[自检] 点击链路：%s", "PASS" if clicks_ok else "FAIL")

            # Git 面板是异步渲染的：点击后要等一会儿再数按钮
            with suppress(Exception):
                window.evaluate_js("document.getElementById('git-btn').click()")
            git_state = {}
            # 面板内容要等几次接口返回才渲染完（打包版首次请求更慢），这里轮询
            deadline_git = time.time() + 12
            while time.time() < deadline_git:
                with suppress(Exception):
                    git_state = (
                        window.evaluate_js(
                            "({ opened: !document.getElementById('git-modal').hidden,"
                            # 只数**真正能看到**的按钮：折叠 <details> 里的不算按钮墙
                            # （Chrome 用 content-visibility 折叠，offsetParent 判断不了，
                            #  这里沿祖先链找关闭的 details）
                            " buttons: Array.from(document.querySelectorAll('#git-body button')).filter((el) => {"
                            "   for (let n = el.parentElement; n; n = n.parentElement) {"
                            "     if (n.tagName === 'DETAILS' && !n.open &&"
                            "         !n.querySelector(':scope > summary')?.contains(el)) return false;"
                            "   }"
                            "   return el.getBoundingClientRect().height > 0; }).length,"
                            " fileRows: document.querySelectorAll('.git-file').length,"
                            # 用 textContent：这三块默认折叠在 <details> 里，
                            # innerText 只看渲染内容，会误判成"缺失"
                            " hasProxy: document.body.textContent.includes('网络代理'),"
                            " hasBranch: document.body.textContent.includes('分支'),"
                            " hasAuto: document.body.textContent.includes('每日开机自动提交') })"
                        )
                        or {}
                    )
                # 主操作（刷新/拉取/推送/提交 + 批量操作）应当齐备；
                # 按钮数不再随文件数暴涨——这是"按钮墙"的回归防线
                if (git_state.get("buttons") or 0) >= 3 and (git_state.get("buttons") or 0) <= 12:
                    break
                time.sleep(0.4)
            logger.info("[自检] Git 面板：%s", git_state)
            git_ok = (
                git_state.get("opened") is True
                and 3 <= (git_state.get("buttons") or 0) <= 12
                and git_state.get("hasProxy") is True
                and git_state.get("hasBranch") is True
                and git_state.get("hasAuto") is True
            )
            logger.info("[自检] Git 面板按钮齐全：%s", "PASS" if git_ok else "FAIL")
            with suppress(Exception):
                window.evaluate_js("document.getElementById('git-close').click()")
            clicks_ok = clicks_ok and git_ok

        time.sleep(max(0.0, seconds))
        with suppress(Exception):
            window.destroy()
        exit_code = 0 if (ok and rendered and clicks_ok) else 1
        logger.info("[自检] 已自动关闭窗口，退出码 %d", exit_code)

    threading.Thread(target=worker, name="orchestrator-selftest", daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
