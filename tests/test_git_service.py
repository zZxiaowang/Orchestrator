"""简化版 Git 服务：状态 / 差异 / 提交 / 历史 / 自动提交脚本。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.services.git_service import GitError, GitService


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "测试"),
        ("config", "user.email", "test@example.com"),
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    # 和真实项目一样忽略 .logs：自动提交会把日志写在被操作仓库的 .logs 下，
    # 若该目录不被忽略，仓库在提交后会立刻变脏。先提交一次，让仓库从干净状态开始。
    (path / ".gitignore").write_text(".logs/\n", encoding="utf-8")
    for args in (("add", ".gitignore"), ("commit", "-m", "chore: 初始化测试仓库")):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def test_status_lists_changes_with_labels(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    service = GitService(repo)
    assert service.status()["is_repo"] is True

    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    status = service.status()
    assert status["clean"] is False
    entry = status["files"][0]
    assert entry["path"] == "a.txt"
    assert entry["label"] == "?"  # 未跟踪
    assert entry["staged"] is False


def test_diff_commit_log_flow(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    service = GitService(repo)
    (repo / "a.txt").write_text("第一行\n", encoding="utf-8")

    # 未跟踪文件的差异用内容片段呈现
    assert "第一行" in service.diff("a.txt")

    result = service.commit("feat: 初始提交")
    assert result["committed"] is True
    assert result["commit"]["subject"] == "feat: 初始提交"
    assert service.status()["clean"] is True

    # 修改后再提交，历史里应有两笔
    (repo / "a.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    assert "+第二行" in service.diff("a.txt")
    service.commit("fix: 追加一行")
    commits = service.log(10)
    assert [item["subject"] for item in commits][:2] == ["fix: 追加一行", "feat: 初始提交"]
    assert service.branches()["all"] == ["main"]


def test_commit_requires_message_and_skips_empty(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    service = GitService(repo)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(GitError):
        service.commit("   ")

    service.commit("chore: 第一次")
    again = service.commit("chore: 没有改动")
    assert again["committed"] is False
    assert "没有需要提交的改动" in again["reason"]


def test_stage_and_unstage(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    service = GitService(repo)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    (repo / "b.txt").write_text("y\n", encoding="utf-8")

    service.stage(["a.txt"])
    by_path = {item["path"]: item for item in service.status()["files"]}
    assert by_path["a.txt"]["staged"] is True
    assert by_path["b.txt"]["staged"] is False

    service.unstage(["a.txt"])
    by_path = {item["path"]: item for item in service.status()["files"]}
    assert by_path["a.txt"]["staged"] is False


def test_path_escape_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    service = GitService(repo)
    for bad in ("../outside.txt", "..\\outside.txt", "C:/Windows/x.txt"):
        with pytest.raises(GitError):
            service.diff(bad)


def test_non_repo_reports_cleanly(tmp_path: Path):
    service = GitService(tmp_path / "not-a-repo")
    status = service.status()
    assert status["is_repo"] is False
    assert str(service.repo) in status["repo"]


def test_auto_commit_script_commits_changes(tmp_path: Path):
    """自动提交脚本：有改动就提交，无改动就跳过（用临时仓库验证，不动真实项目）。"""
    repo = _init_repo(tmp_path / "repo")
    service = GitService(repo)
    (repo / "auto.txt").write_text("每日自动提交\n", encoding="utf-8")

    first = service.run_auto_commit_now()
    assert first["ok"] is True, first["output"]
    assert "已提交 1 个文件" in first["output"]
    subjects = [item["subject"] for item in service.log(5)]
    assert subjects and subjects[0].startswith("chore(auto): 每日自动提交")
    assert service.status()["clean"] is True

    # 日志必须写在**被操作的仓库**里。以前它跟着脚本位置走，导致用临时仓库
    # 跑这个脚本会把"已提交"写进真实项目的日志，看起来像真实仓库被自动提交了。
    log_file = repo / ".logs" / "auto-commit.log"
    assert log_file.is_file(), "自动提交日志应当落在被操作仓库的 .logs 下"
    assert "已提交 1 个文件" in log_file.read_text(encoding="utf-8")

    second = service.run_auto_commit_now()
    assert "无改动，跳过提交" in second["output"]


def test_every_spawn_hides_the_console_window(tmp_path: Path, monkeypatch):
    """桌面版没有控制台：任何 git/自动提交子进程都必须隐藏窗口。

    否则每次面板刷新（3 条 git 命令）都会弹黑窗——用户看到的就是
    「提交个 git 为什么要不断开窗口」。
    """

    repo = _init_repo(tmp_path / "repo")
    service = GitService(repo)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")

    seen: list[dict] = []
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.append(dict(kwargs))
        return real_run(*args, **kwargs)

    monkeypatch.setattr("app.services.git_service.subprocess.run", spy)
    service.status()
    service.run_auto_commit_now()

    assert seen, "本用例应当至少触发一次子进程"
    if os.name == "nt":
        expected = subprocess.CREATE_NO_WINDOW
        assert all(item.get("creationflags") == expected for item in seen), [
            item.get("creationflags") for item in seen
        ]
    else:
        assert all("creationflags" not in item for item in seen)


def test_auto_commit_launcher_does_not_open_a_window(tmp_path: Path, monkeypatch):
    """启动项必须是"静默运行"的形式，否则每次登录都弹黑窗。"""

    repo = _init_repo(tmp_path / "repo")
    service = GitService(repo)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    status = service.enable_auto_commit(push=False)
    launcher = service.startup_launcher()
    assert status["enabled"] is True
    assert launcher.suffix == ".vbs", "启动项应当是 .vbs（wscript 不创建控制台窗口）"

    content = launcher.read_bytes()
    assert content.decode("ascii")  # 纯 ASCII：系统按 ANSI 读启动项，中文会变乱码命令
    text = content.decode("ascii")
    assert 'CreateObject("WScript.Shell")' in text
    assert ", 0, False" in text  # 0 = 不显示窗口，False = 不等待
    assert str(service.auto_commit_script()) in text
    assert b"\r\r\n" not in content, "文本模式写文件会把 \\r\\n 变成 \\r\\r\\n"

    # 旧版本留下的 .cmd 启动项会被清掉（它每次登录都弹窗）
    legacy = service.legacy_startup_launcher()
    legacy.write_text("@echo off\r\n", encoding="ascii")
    service.enable_auto_commit(push=True)
    assert not legacy.is_file()
    assert " push" in launcher.read_text(encoding="ascii")

    assert service.disable_auto_commit()["enabled"] is False
    assert not launcher.is_file()
