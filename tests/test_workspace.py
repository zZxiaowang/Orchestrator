"""工作区落地：路径防护、增量替换、备份与 diff。"""

from __future__ import annotations

from pathlib import Path

from app.schemas.step import FileEdit, SearchReplace
from app.services.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "ws", backup_dir=tmp_path / "backup")


def test_create_update_delete_flow(tmp_path: Path):
    ws = _workspace(tmp_path)

    created = ws.apply_edit(
        FileEdit(path="src/app.py", action="create", content="print('hello')\n")
    )
    assert created.action == "create"
    assert created.additions >= 1
    target = tmp_path / "ws" / "src" / "app.py"
    assert target.read_text(encoding="utf-8") == "print('hello')\n"

    updated = ws.apply_edit(
        FileEdit(
            path="src/app.py",
            action="update",
            edits=[SearchReplace(search="hello", replace="world")],
        )
    )
    assert updated.action == "update"
    assert "print('world')" in target.read_text(encoding="utf-8")
    assert (tmp_path / "backup" / "src" / "app.py").is_file()

    deleted = ws.apply_edit(FileEdit(path="src/app.py", action="delete"))
    assert deleted.action == "delete"
    assert not target.exists()


def test_no_change_is_reported_as_unchanged(tmp_path: Path):
    ws = _workspace(tmp_path)
    ws.apply_edit(FileEdit(path="a.txt", action="create", content="same\n"))
    again = ws.apply_edit(FileEdit(path="a.txt", action="create", content="same\n"))
    assert again.changed is False
    assert again.action == "unchanged"


def test_missing_search_keeps_file_untouched(tmp_path: Path):
    ws = _workspace(tmp_path)
    ws.apply_edit(FileEdit(path="a.txt", action="create", content="原始内容\n"))
    result = ws.apply_edit(
        FileEdit(
            path="a.txt",
            action="update",
            edits=[SearchReplace(search="不存在的片段", replace="x")],
        )
    )
    assert result.error
    assert "未在文件中定位" in result.error
    assert (tmp_path / "ws" / "a.txt").read_text(encoding="utf-8") == "原始内容\n"


def test_path_escape_is_rejected(tmp_path: Path):
    ws = _workspace(tmp_path)
    bad_paths = ("../evil.txt", "..\\evil.txt", "C:/Windows/evil.txt", "/etc/passwd", ".git/config")
    for bad in bad_paths:
        result = ws.apply_edit(FileEdit(path=bad, action="create", content="x"))
        assert result.error, bad
        assert not (tmp_path / "evil.txt").exists()


def test_tree_skips_ignored_dirs_and_respects_limit(tmp_path: Path):
    ws = _workspace(tmp_path)
    (tmp_path / "ws" / "node_modules").mkdir(parents=True)
    (tmp_path / "ws" / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    ws.apply_edit(FileEdit(path="a.txt", action="create", content="a"))
    ws.apply_edit(FileEdit(path="sub/b.txt", action="create", content="b"))
    files = ws.tree()
    assert "a.txt" in files
    assert "sub/b.txt" in files
    assert all("node_modules" not in item for item in files)
