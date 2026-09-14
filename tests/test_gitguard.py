"""步骤级 git 快照与回滚：只动这一步碰过的文件，其他改动一律不碰。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.services.gitguard import exists_at_head, is_repo, revert_paths, snapshot


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "测试"),
        ("config", "user.email", "test@example.com"),
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "kept.txt").write_text("原始内容\n", encoding="utf-8")
    (path / ".gitignore").write_text(".logs/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


def test_snapshot_reports_head_and_clean_state(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    snap = snapshot(repo)
    assert is_repo(repo) is True
    assert snap.usable is True
    assert snap.branch == "main"
    assert snap.dirty is False

    (repo / "kept.txt").write_text("改过了\n", encoding="utf-8")
    assert snapshot(repo).dirty is True


def test_snapshot_on_non_repo_is_unusable_not_crashing(tmp_path: Path):
    snap = snapshot(tmp_path)
    assert snap.usable is False
    assert snap.error


def test_revert_restores_modified_and_removes_created(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "kept.txt").write_text("被这一步改坏了\n", encoding="utf-8")
    (repo / "new_file.md").write_text("这一步新建的\n", encoding="utf-8")

    result = revert_paths(repo, ["kept.txt", "new_file.md"])
    assert result.restored == ["kept.txt"]
    assert result.removed == ["new_file.md"]
    assert (repo / "kept.txt").read_text(encoding="utf-8") == "原始内容\n"
    assert not (repo / "new_file.md").exists()


def test_revert_does_not_touch_unrelated_changes(tmp_path: Path):
    """用户自己在别处的未提交改动绝不能被回滚连带清掉。"""

    repo = _init_repo(tmp_path / "repo")
    (repo / "user_work.txt").write_text("用户自己写的，还没提交\n", encoding="utf-8")
    (repo / "kept.txt").write_text("agent 改的\n", encoding="utf-8")

    revert_paths(repo, ["kept.txt"])

    assert (repo / "user_work.txt").is_file()
    assert (repo / "user_work.txt").read_text(encoding="utf-8") == "用户自己写的，还没提交\n"
    assert (repo / "kept.txt").read_text(encoding="utf-8") == "原始内容\n"


def test_revert_rejects_path_escape(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text("不该被删\n", encoding="utf-8")

    result = revert_paths(repo, ["../outside.txt"])
    assert outside.is_file()
    assert "../outside.txt" in result.skipped


def test_exists_at_head_distinguishes_new_files(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    assert exists_at_head(repo, "kept.txt") is True
    assert exists_at_head(repo, "brand_new.txt") is False
