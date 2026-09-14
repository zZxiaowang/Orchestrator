"""步骤级 git 快照与回滚：自开发的安全网。

为什么需要它：让 Orchestrator 改自己的源码时，最大风险不是"改错一个文件"，
而是**改错了还退不回去**。每一步开始前在 git 里留一个锚点，出错就按锚点把这
一步碰过的文件精确还原——只动这一步碰过的文件，不碰用户其他未提交的改动。

设计取舍：

* **只回滚这一步碰过的路径**（来自 ``RunStep.files``），不做 ``git reset --hard``：
  后者会把用户在别处的工作一起清掉，属于不可接受的副作用；
* 不是 git 仓库/没有 git 命令时，返回"不可用"而不是报错——工作区回滚还有
  ``backup/`` 目录兜底（``Workspace`` 覆盖写入前会备份原件）；
* 只读 git 命令（``rev-parse`` / ``cat-file``）用于判断"这个文件在快照点是否存在"。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GIT_TIMEOUT = 30


@dataclass
class GitSnapshot:
    """一步开始前的仓库状态锚点。"""

    head: str = ""
    branch: str = ""
    dirty: bool = False
    error: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.head) and not self.error

    def as_dict(self) -> dict[str, object]:
        return {
            "head": self.head,
            "branch": self.branch,
            "dirty": self.dirty,
            "error": self.error,
        }


@dataclass
class RevertResult:
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "restored": self.restored,
            "removed": self.removed,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{exc.__class__.__name__}: {exc}"
    return completed.returncode, (completed.stdout or completed.stderr or "").strip()


def snapshot(repo: Path) -> GitSnapshot:
    """记录当前 HEAD 与分支；不是仓库/没有 git 时返回带 error 的空快照。"""

    code, head = _git(repo, "rev-parse", "HEAD")
    if code != 0:
        return GitSnapshot(error=head or "当前目录不是 git 仓库（或还没有任何提交）")
    _, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    status_code, status = _git(repo, "status", "--porcelain")
    return GitSnapshot(
        head=head,
        branch=branch,
        dirty=bool(status.strip()) if status_code == 0 else False,
    )


def exists_at_head(repo: Path, path: str) -> bool:
    """该路径在快照点（HEAD）是否存在：决定回滚时是"还原"还是"删除"。"""

    code, _ = _git(repo, "cat-file", "-e", f"HEAD:{path}")
    return code == 0


def revert_paths(repo: Path, paths: list[str]) -> RevertResult:
    """把指定路径还原到 HEAD：原本存在的还原，原本不存在的删掉。

    只处理传入的路径，其他未提交改动一律不碰。
    """

    result = RevertResult()
    root = Path(repo).resolve()
    for raw in paths:
        # 注意：不能用 lstrip("./") 归一化 —— 它会把 "../outside.txt" 削成
        # "outside.txt"，等于把路径穿越检查静默绕过去。
        relative = safe_relative(raw)
        if relative is None:
            result.skipped.append(str(raw))
            continue
        target = (root / relative).resolve()
        if root != target and root not in target.parents:
            result.skipped.append(relative)
            continue

        if exists_at_head(root, relative):
            code, output = _git(root, "checkout", "HEAD", "--", relative)
            if code == 0:
                result.restored.append(relative)
            else:
                result.errors.append(f"{relative}: {output[:160]}")
            continue

        # 快照点不存在 → 这一步新建的，回滚就是删掉它
        try:
            if target.is_file():
                target.unlink()
                result.removed.append(relative)
            else:
                result.skipped.append(relative)
        except OSError as exc:
            result.errors.append(f"{relative}: {exc}")
    return result


def is_repo(repo: Path) -> bool:
    code, _ = _git(repo, "rev-parse", "--is-inside-work-tree")
    return code == 0


def safe_relative(raw: str) -> str | None:
    """把用户/模型给的路径收敛成仓库内的相对路径；越界或盘符路径返回 None。

    只剥掉前导 ``./``，**不**用 ``lstrip("./")``——后者会把 ``../x`` 削成 ``x``，
    让路径穿越检查形同虚设。
    """

    text = str(raw or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/"):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    if ":" in parts[0]:  # Windows 盘符，如 C:/Windows
        return None
    return "/".join(parts)
