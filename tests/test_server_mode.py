"""服务器形态入口：命令行给的 host / port 必须真的生效。

回归来源：`dist\\Orchestrator.exe --server --port 8791` 曾经被静默忽略——
桌面入口收下了参数，转到服务器形态时却没传下去，实际仍监听配置里的 8787，
用户以为换端口成功了，其实没有。这类"静默失效"必须由测试钉住。
"""

from __future__ import annotations

import pytest
import uvicorn

import app.main as main_module
from app import desktop
from app.core.config import Settings


class _CapturingServer:
    """替身：只记录 uvicorn 拿到的监听地址，不真的起服务。"""

    instances: list[_CapturingServer] = []

    def __init__(self, config) -> None:  # noqa: ANN001 - 与 uvicorn.Server 一致
        self.config = config
        self.ran = False
        _CapturingServer.instances.append(self)

    @classmethod
    def last(cls) -> _CapturingServer:
        assert cls.instances, "uvicorn 服务器没有被创建"
        return cls.instances[-1]

    def run(self) -> None:
        self.ran = True


@pytest.fixture()
def server_probe(monkeypatch):
    _CapturingServer.instances = []
    monkeypatch.setattr(uvicorn, "Server", _CapturingServer)
    # 不要真的去改控制台代码页（那是给打包版双击启动用的）
    monkeypatch.setattr(main_module, "_force_utf8_console", lambda: None)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            host="127.0.0.1",
            port=8787,
            relay_base_url="https://relay.test/v1",
            relay_api_key="sk-test-1234567890",
        ),
    )
    return _CapturingServer


def test_server_mode_forwards_cli_port_and_host(server_probe):
    assert desktop.main(["--server", "--host", "127.0.0.1", "--port", "8791"]) == 0
    server = server_probe.last()
    assert server.config.port == 8791
    assert server.config.host == "127.0.0.1"
    assert server.ran is True


def test_server_mode_uses_cli_port_with_settings_host(server_probe):
    desktop.main(["--server", "--port", "8791"])
    server = server_probe.last()
    assert server.config.port == 8791
    # 没给 --host 时仍然跟随配置
    assert server.config.host == "127.0.0.1"


def test_server_mode_without_flags_falls_back_to_settings(server_probe):
    desktop.main(["--server"])
    server = server_probe.last()
    assert (server.config.host, server.config.port) == ("127.0.0.1", 8787)


def test_server_main_accepts_explicit_bind_address(server_probe):
    """`python -m app.main` 的等价调用：不传参数就用配置。"""
    main_module.main(port=9000)
    assert server_probe.last().config.port == 9000


def test_parser_keeps_port_alongside_server_flag():
    args = desktop.build_parser().parse_args(["--server", "--port", "8791"])
    assert args.server is True
    assert args.port == 8791
