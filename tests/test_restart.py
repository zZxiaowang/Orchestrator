"""打包重启：请求文件、辅助脚本、以及"必须显式确认"的守门逻辑。

真正的重启会结束进程，所以这里只测可控的部分（绝不真的重启）。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.restart import build_request, helper_available, request_path, write_request
from tests.conftest import FakeRelay
from tests.test_api_flow import build_client


def test_request_describes_how_to_relaunch(tmp_path: Path):
    data_dir = tmp_path / "data"
    payload = build_request(
        data_dir=data_dir,
        project_root=tmp_path / "proj",
        frozen=True,
        rebuild=True,
        host="127.0.0.1",
        port=8787,
    )
    assert payload["pid"] > 0
    assert payload["rebuild"] is True
    assert payload["frozen"] is True
    assert payload["launch_args"] == []  # 打包版直接启动 exe
    assert payload["env"]["ORCHESTRATOR_DATA_DIR"] == str(data_dir)
    assert payload["package_script"].endswith("package.ps1")


def test_source_mode_relaunches_uvicorn_with_same_port(tmp_path: Path):
    payload = build_request(
        data_dir=tmp_path / "data",
        project_root=tmp_path / "proj",
        frozen=False,
        rebuild=False,
        host="127.0.0.1",
        port=8787,
    )
    assert payload["launch_args"] == [
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8787",
    ]


def test_write_request_is_json_and_replaces_previous(tmp_path: Path):
    data_dir = tmp_path / "data"
    first = write_request(data_dir, {"pid": 1})
    second = write_request(data_dir, {"pid": 2})
    assert first == second == request_path(data_dir)
    assert json.loads(second.read_text(encoding="utf-8")) == {"pid": 2}


def test_helper_script_exists_in_repository():
    root = Path(__file__).resolve().parents[1]
    assert helper_available(root) is True
    script = (root / "scripts" / "restart.ps1").read_bytes()
    # 纯 ASCII：Windows PowerShell 按 ANSI 读脚本，中文会变乱码命令
    assert script.decode("ascii")
    text = script.decode("ascii")
    assert "-WindowStyle" in text and "Start-Process" in text
    assert "Get-Process -Id $spec.pid" in text  # 等旧进程退出再打包


def test_restart_requires_explicit_confirmation(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        response = client.post("/api/v1/system/restart", json={"rebuild": True})
        assert response.status_code == 400
        assert "confirm" in response.json()["error"]["message"]


def test_restart_is_refused_under_pytest(tmp_path: Path):
    """即使确认了，测试环境也绝不允许真的重启自己。"""

    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        response = client.post("/api/v1/system/restart", json={"rebuild": True, "confirm": True})
        assert response.status_code == 400
        assert "测试环境" in response.json()["error"]["message"]
