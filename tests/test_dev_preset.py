"""自开发预设：一键配置质量门白名单 + 开发模式指向本仓库。"""

from __future__ import annotations

from pathlib import Path

from app.api.routes import DEV_QUALITY_GATES
from tests.conftest import FakeRelay
from tests.test_api_flow import build_client


def test_system_info_reports_project_root_and_gates(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        info = client.get("/api/v1/system/info").json()

    assert info["project_root"]
    assert info["data_dir"]
    assert isinstance(info["frozen"], bool)
    assert isinstance(info["is_git_repo"], bool)
    assert info["quality_gates"] == list(DEV_QUALITY_GATES)
    assert any("pytest" in item for item in info["quality_gates"])


def test_dev_preset_enables_commands_and_whitelists_quality_gates(tmp_path: Path):
    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        before = client.get("/api/v1/settings").json()
        assert before["allow_command_execution"] is False
        assert before["command_allowlist"] == []

        applied = client.post("/api/v1/system/dev-preset", json={}).json()
        assert applied["allow_command_execution"] is True
        assert applied["command_allowlist"] == list(DEV_QUALITY_GATES)
        assert applied["step_command_rounds"] == 2

    # 再开一个"从配置库读取"的实例，确认是真的落盘而不只是回显
    # （build_client 默认注入固定配置快照，这里要显式读配置库）
    with build_client(tmp_path, relay, use_store_provider=True) as client:
        after = client.get("/api/v1/settings").json()
        assert after["command_allowlist"] == list(DEV_QUALITY_GATES)
        assert after["allow_command_execution"] is True


def test_quality_gate_entries_are_prefix_matchable_commands():
    """白名单是按前缀匹配的：这些前缀必须能命中带参数的真实命令。"""

    from app.services.commands import check_command

    cases = [
        ("python -m pytest -q", "python -m pytest"),
        ("python -m ruff check .", "python -m ruff"),
        ("node scripts/ui_check.mjs --url http://127.0.0.1:8788", "node scripts/ui_check.mjs"),
    ]
    for command, prefix in cases:
        assert prefix in DEV_QUALITY_GATES
        assert check_command(command, list(DEV_QUALITY_GATES), enabled=True) == ""

    # 危险命令依然进不来：不在白名单里，且含 shell 字符
    assert check_command("rm -rf .", list(DEV_QUALITY_GATES), enabled=True) != ""
    assert check_command("python -m pytest && del *", list(DEV_QUALITY_GATES), enabled=True) != ""
