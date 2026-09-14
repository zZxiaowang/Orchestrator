"""工作区：模型产出的文件改动在这里落地。

安全约束（执行段是"会写盘"的一段，必须收紧）：

* 只允许相对路径，拒绝绝对路径、Windows 盘符与 ``..`` 穿越；
* 拒绝写入 ``.git`` 等版本控制目录；
* 覆盖/删除前先备份到运行目录的 ``backup/``，可回溯；
* "查找-替换"必须先全部命中才写入，避免半途产生半成品文件。
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.errors import WorkspaceError
from app.schemas.step import FileEdit

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".next",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".pnpm-store",
}
FORBIDDEN_PARTS = {".git", ".hg", ".svn"}


@dataclass
class AppliedChange:
    path: str
    action: str
    additions: int
    deletions: int
    diff: str
    size: int
    changed: bool = True
    error: str = ""


class Workspace:
    def __init__(self, root: Path, *, backup_dir: Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.backup_dir = Path(backup_dir).resolve() if backup_dir else None

    # ── 路径 ──

    def resolve(self, relative: str) -> Path:
        raw = (relative or "").strip().replace("\\", "/")
        if not raw:
            raise WorkspaceError("文件路径为空。", details={"path": relative})
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            raise WorkspaceError(
                f"只接受工作区内的相对路径：{relative}",
                details={"path": relative, "workspace": str(self.root)},
            )
        parts = [p for p in PurePosixPath(raw).parts if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise WorkspaceError(
                f"路径包含向上穿越，已拒绝：{relative}",
                details={"path": relative},
            )
        if any(p in FORBIDDEN_PARTS for p in parts):
            raise WorkspaceError(
                f"拒绝写入版本控制目录：{relative}",
                details={"path": relative},
            )
        target = (self.root / Path(*parts)).resolve()
        if target != self.root and self.root not in target.parents:
            raise WorkspaceError(
                f"路径落在工作区之外，已拒绝：{relative}",
                details={"path": relative, "workspace": str(self.root)},
            )
        return target

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # ── 读写 ──

    def exists(self, relative: str) -> bool:
        return self.resolve(relative).is_file()

    def read(self, relative: str) -> str:
        path = self.resolve(relative)
        if not path.is_file():
            raise WorkspaceError(f"文件不存在：{relative}", details={"path": relative})
        return path.read_text(encoding="utf-8", errors="replace")

    def tree(self, limit: int = 120, max_depth: int = 3) -> list[str]:
        """列出工作区文件（相对路径，深度与条目数受限）。"""
        if not self.root.exists():
            return []
        results: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if len(results) >= limit:
                break
            relative = path.relative_to(self.root)
            if any(part in IGNORED_DIRS for part in relative.parts):
                continue
            if path.is_dir():
                continue
            if len(relative.parts) > max_depth:
                continue
            results.append(relative.as_posix())
        return results

    # ── 落地 ──

    def apply_edit(self, edit: FileEdit) -> AppliedChange:
        if not edit.valid:
            return AppliedChange(
                path=edit.path,
                action=edit.action,
                additions=0,
                deletions=0,
                diff="",
                size=0,
                changed=False,
                error="改动缺少内容或查找-替换片段。",
            )

        try:
            target = self.resolve(edit.path)
        except WorkspaceError as exc:
            return AppliedChange(
                path=edit.path,
                action=edit.action,
                additions=0,
                deletions=0,
                diff="",
                size=0,
                changed=False,
                error=exc.message,
            )

        relative = self.relative(target)
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        existed = target.is_file()

        try:
            after = self._render(edit, before, relative)
        except WorkspaceError as exc:
            return AppliedChange(
                path=relative,
                action="update" if existed else edit.action,
                additions=0,
                deletions=0,
                diff="",
                size=len(before.encode("utf-8")),
                changed=False,
                error=exc.message,
            )

        if existed and after == before:
            return AppliedChange(
                path=relative,
                action="unchanged",
                additions=0,
                deletions=0,
                diff="",
                size=len(before.encode("utf-8")),
                changed=False,
            )

        diff, additions, deletions = _unified_diff(before, after, relative)
        self._backup(target)

        if edit.action == "delete":
            if target.exists():
                target.unlink()
            return AppliedChange(
                path=relative,
                action="delete",
                additions=0,
                deletions=deletions,
                diff=diff,
                size=0,
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(after, encoding="utf-8")
        return AppliedChange(
            path=relative,
            action="update" if existed else "create",
            additions=additions,
            deletions=deletions,
            diff=diff,
            size=len(after.encode("utf-8")),
        )

    def _render(self, edit: FileEdit, before: str, relative: str) -> str:
        if edit.action == "delete":
            return ""
        if edit.action == "update" and not before and edit.content is None:
            raise WorkspaceError(
                f"要更新的文件不存在：{relative}",
                details={"path": relative},
            )
        if edit.edits:
            text = before
            for item in edit.edits:
                if not item.search:
                    raise WorkspaceError(
                        "查找-替换片段缺少 search 内容。",
                        details={"path": relative},
                    )
                if item.search not in text:
                    snippet = " ".join(item.search.split())[:80]
                    raise WorkspaceError(
                        f"未在文件中定位到待替换片段：{snippet}",
                        details={"path": relative},
                    )
                text = text.replace(item.search, item.replace, 1)
            return text
        return edit.content or ""

    def _backup(self, target: Path) -> None:
        if self.backup_dir is None or not target.is_file():
            return
        relative = self.relative(target)
        destination = self.backup_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, destination)


def _unified_diff(before: str, after: str, relative: str) -> tuple[str, int, int]:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="",
        )
    )
    text = "\n".join(line.rstrip("\n") for line in lines)
    additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return text, additions, deletions
