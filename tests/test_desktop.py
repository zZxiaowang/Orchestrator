"""桌面客户端（原生窗口）入口：不依赖真实 GUI 的可测部分。"""

from __future__ import annotations

import socket
import sys
import time
import types
from pathlib import Path
from urllib.request import urlopen

from app import desktop
from app.core import config as config_module


def test_pick_free_port_is_bindable():
    port = desktop.pick_free_port()
    assert 1024 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))  # 能绑定说明确实空闲


def test_wait_for_server_false_when_nothing_listens():
    port = desktop.pick_free_port()
    started = time.time()
    assert desktop.wait_for_server(f"http://127.0.0.1:{port}/", timeout=1.0) is False
    assert time.time() - started >= 1.0


def test_wait_for_server_true_for_live_backend():
    port = desktop.pick_free_port()
    server, thread = desktop.start_backend("127.0.0.1", port)
    try:
        assert desktop.wait_for_server(f"http://127.0.0.1:{port}/api/v1/health")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_parser_defaults_and_flags():
    parser = desktop.build_parser()
    args = parser.parse_args([])
    assert args.port == 0 and args.selftest == 0.0 and args.server is False

    args = parser.parse_args(["--selftest", "3", "--port", "9000", "--debug"])
    assert args.selftest == 3.0 and args.port == 9000 and args.debug is True

    # --selftest 不带数值时用默认秒数
    assert parser.parse_args(["--selftest"]).selftest == 6.0
    assert parser.parse_args(["--server"]).server is True


class _FakeWindow:
    def __init__(self, title, url, **kwargs):
        self.title = title
        self.url = url
        self.kwargs = kwargs
        self.destroyed = False
        self.health_ok: bool | None = None

    def destroy(self) -> None:
        self.destroyed = True

    def evaluate_js(self, script: str) -> bool:
        """假窗口：模拟"界面已渲染"，让自检逻辑能跑完。"""
        if "hasProxy" in script:
            return {
                "opened": True,
                # 新的 Git 面板：默认只留主操作（按钮不再随文件数暴涨）
                "buttons": 5,
                "fileRows": 3,
                "hasProxy": True,
                "hasBranch": True,
                "hasAuto": True,
            }
        return "document.querySelector('.app')" in script


class _FakeWebview(types.ModuleType):
    """假 webview：记录窗口参数，并在"事件循环"里顺带做一次健康检查。"""

    def __init__(self) -> None:
        super().__init__("webview")
        self.window: _FakeWindow | None = None
        self.debug = False

    def create_window(self, title, url, **kwargs):  # noqa: ANN001 - 与 pywebview 一致
        self.window = _FakeWindow(title, url, **kwargs)
        return self.window

    def start(self, debug: bool = False) -> None:  # noqa: ANN001
        self.debug = debug
        assert self.window is not None
        try:
            with urlopen(f"{self.window.url}/api/v1/health", timeout=5) as response:
                self.window.health_ok = response.status == 200
        except OSError:
            self.window.health_ok = False
        deadline = time.time() + 15
        while not self.window.destroyed and time.time() < deadline:
            time.sleep(0.05)


def test_desktop_selftest_flow_without_real_window(tmp_path: Path, monkeypatch):
    """自检模式：起后端 → 建窗口 → 健康检查 → 自动关窗 → 退出码 0。"""
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
    fake = _FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", fake)

    code = desktop.main(["--selftest", "0.5"])

    assert code == 0
    window = fake.window
    assert window is not None
    assert window.title.startswith("Orchestrator")
    assert window.url.startswith("http://127.0.0.1:")
    assert window.health_ok is True
    assert window.destroyed is True
    # 窗口尺寸符合桌面客户端设定
    assert window.kwargs["width"] == desktop.DEFAULT_WIDTH
    assert window.kwargs["min_size"] == desktop.MIN_SIZE
    # 日志写进数据目录（桌面版无控制台）
    assert (tmp_path / "data" / "logs" / "orchestrator.log").is_file()


def test_desktop_reports_missing_webview_dependency(tmp_path: Path, monkeypatch):
    """没装 pywebview 时给出可操作的提示，并停掉后端线程。"""
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setitem(sys.modules, "webview", None)  # import webview 会失败

    assert desktop.main(["--selftest", "0.1"]) == 3
