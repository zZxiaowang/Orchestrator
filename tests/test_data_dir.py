"""打包版的数据目录选择：不能让 exe 另起一份空配置。"""

from __future__ import annotations

import sys
from pathlib import Path

from app.core import config as config_module


def _fake_frozen(monkeypatch, exe_path: Path):
    monkeypatch.setattr(config_module, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", str(exe_path))
    monkeypatch.delenv("ORCHESTRATOR_DATA_DIR", raising=False)


def test_env_override_wins(tmp_path: Path, monkeypatch):
    _fake_frozen(monkeypatch, tmp_path / "dist" / "Orchestrator.exe")
    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(tmp_path / "custom"))
    assert config_module._data_root() == tmp_path / "custom"


def test_prefers_configured_data_beside_exe(tmp_path: Path, monkeypatch):
    exe_dir = tmp_path / "dist"
    (exe_dir / "data").mkdir(parents=True)
    (exe_dir / "data" / "settings.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "settings.json").write_text("{}", encoding="utf-8")
    _fake_frozen(monkeypatch, exe_dir / "Orchestrator.exe")

    assert config_module._data_root() == exe_dir / "data"


def test_falls_back_to_project_data_dir(tmp_path: Path, monkeypatch):
    """exe 放在 <项目>\\dist\\ 下时，应复用 <项目>\\data（里面有你的配置）。"""
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir(parents=True)
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "settings.json").write_text("{}", encoding="utf-8")
    _fake_frozen(monkeypatch, exe_dir / "Orchestrator.exe")

    assert config_module._data_root() == tmp_path / "data"


def test_creates_beside_exe_when_nothing_configured(tmp_path: Path, monkeypatch):
    exe_dir = tmp_path / "portable"
    exe_dir.mkdir(parents=True)
    _fake_frozen(monkeypatch, exe_dir / "Orchestrator.exe")

    assert config_module._data_root() == exe_dir / "data"
    # 目录本身在应用启动时才创建；这里只要求可写探测不报错
    assert config_module._dir_writable(exe_dir)
