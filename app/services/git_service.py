"""简化版 Git 服务（参照 IDEA 的 Git 工具窗，但只保留最常用能力）。

覆盖：状态/差异/暂存/提交/推送/拉取/历史/分支，以及**每日开机自动提交**开关
（通过 Windows 计划任务的 ONLOGON 触发器实现）。

安全约定：

* 全部走 ``subprocess`` 的参数列表调用，**不使用 shell**，避免命令注入；
* 所有路径都限定在仓库目录内；
* 自动提交**默认只提交本地**，推送需要显式开启（开机时弹凭证窗口很烦）。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AppError

TASK_NAME = "Orchestrator-DailyAutoCommit"
GIT_TIMEOUT = 60


class GitError(AppError):
    """Git 命令失败。"""

    code = "git_error"


@dataclass(frozen=True)
class ChangedFile:
    path: str
    index_status: str  # 暂存区状态（A/M/D/R/?）
    work_status: str  # 工作区状态

    @property
    def staged(self) -> bool:
        return self.index_status not in (" ", "?")

    @property
    def label(self) -> str:
        """给界面用的单字母标签（IDEA 风格：A 新增 / M 修改 / D 删除 / R 重命名 / ? 未跟踪）。"""
        for status in (self.index_status, self.work_status):
            if status in ("A", "M", "D", "R", "C", "U"):
                return "M" if status == "U" else status
        return "?" if self.index_status == "?" else "M"


class GitService:
    def __init__(self, repo: Path, *, task_name: str = TASK_NAME) -> None:
        self.repo = Path(repo).resolve()
        self.task_name = task_name
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key: str, ttl: float, factory):
        """短缓存：面板一次操作会触发多次读取，避免重复拉 git 子进程（Windows 上很贵）。"""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = factory()
        self._cache[key] = (now, value)
        return value

    def invalidate(self) -> None:
        self._cache.clear()

    # ── 基础 ──

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise GitError("未找到 git 命令，请先安装 Git for Windows。") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git 命令超时：git {' '.join(args)}") from exc
        if check and result.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} 失败：{(result.stderr or result.stdout).strip()[:300]}",
                details={"args": list(args), "returncode": result.returncode},
            )
        return result

    def is_repo(self) -> bool:
        return self._run("rev-parse", "--is-inside-work-tree", check=False).returncode == 0

    # ── 状态 ──

    def status(self) -> dict[str, object]:
        if not self.is_repo():
            return {"is_repo": False, "repo": str(self.repo)}

        # 一次 status --branch 同时拿到：分支、上游、ahead/behind、文件列表
        raw = self._run("status", "--porcelain", "-z", "--branch", check=False).stdout
        branch = "（detached）"
        upstream = ""
        ahead = behind = 0
        entries: list[str] = []
        for item in (part for part in raw.split("\0") if part):
            if item.startswith("## "):
                header = item[3:]
                if "..." in header:
                    branch, _, rest = header.partition("...")
                    upstream = rest.split(" ")[0]
                else:
                    branch = header.split(" ")[0]
                match = re.search(r"ahead (\d+)", header)
                if match:
                    ahead = int(match.group(1))
                match = re.search(r"behind (\d+)", header)
                if match:
                    behind = int(match.group(1))
            else:
                entries.append(item)

        remote = self._cached(
            "remote",
            5.0,
            lambda: self._run("remote", "get-url", "origin", check=False).stdout.strip(),
        )

        files = [
            {
                "path": item.path,
                "index_status": item.index_status,
                "work_status": item.work_status,
                "staged": item.staged,
                "label": item.label,
            }
            for item in self._parse_status_entries(entries)
        ]
        return {
            "is_repo": True,
            "repo": str(self.repo),
            "branch": branch,
            "remote": remote,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "files": files,
            "clean": not files,
            "last_commit": self._last_commit(),
        }

    def changed_files(self) -> list[ChangedFile]:
        raw = self._run("status", "--porcelain", "-z", check=False).stdout
        return self._parse_status_entries([item for item in raw.split("\0") if item])

    @staticmethod
    def _parse_status_entries(parts: list[str]) -> list[ChangedFile]:
        if not parts:
            return []
        files: list[ChangedFile] = []
        index = 0
        while index < len(parts):
            entry = parts[index]
            code = entry[:2]
            path = entry[3:]
            # 重命名会额外跟一个原路径项，跳过它
            if code[0] in ("R", "C") and index + 1 < len(parts):
                index += 1
            files.append(ChangedFile(path=path, index_status=code[0], work_status=code[1]))
            index += 1
        return files

    def _last_commit(self) -> dict[str, str]:
        line = self._run(
            "log",
            "-1",
            "--pretty=%h%x1f%an%x1f%ad%x1f%s",
            "--date=format:%Y-%m-%d %H:%M",
            check=False,
        ).stdout.strip()
        if not line:
            return {}
        parts = line.split("\x1f")
        if len(parts) < 4:
            return {}
        return {"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}

    def log(self, limit: int = 30) -> list[dict[str, str]]:
        out = self._run(
            "log",
            f"-{max(1, min(limit, 200))}",
            "--pretty=%h%x1f%an%x1f%ad%x1f%s",
            "--date=format:%Y-%m-%d %H:%M",
            check=False,
        ).stdout
        commits: list[dict[str, str]] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 4:
                commits.append(
                    {"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}
                )
        return commits

    def branches(self) -> dict[str, object]:
        out = self._run("branch", "--format=%(refname:short)", check=False).stdout
        current = self._run("branch", "--show-current").stdout.strip()
        return {
            "current": current,
            "all": [line.strip() for line in out.splitlines() if line.strip()],
        }

    def checkout_branch(self, name: str, *, create: bool = False) -> dict[str, object]:
        """切换分支（create=True 时新建并切换）。"""
        branch = (name or "").strip()
        if not branch or any(ch in branch for ch in " ~^:?*[\\"):
            raise GitError(f"分支名不合法：{name}", details={"branch": branch})
        args = ["checkout", "-b", branch] if create else ["checkout", branch]
        result = self._run(*args, check=False)
        return {
            "ok": result.returncode == 0,
            "stdout": (result.stdout or "").strip()[:400],
            "stderr": (result.stderr or "").strip()[:400],
            "hint": ""
            if result.returncode == 0
            else "切换失败：可能有未提交改动会冲突，请先提交或丢弃。",
            "branches": self.branches(),
        }

    def discard(self, paths: list[str]) -> dict[str, object]:
        """丢弃改动（已跟踪文件回滚到 HEAD；未跟踪文件删除）。破坏性操作，界面需二次确认。"""
        if not paths:
            raise GitError("请先选择要丢弃的文件。")
        status_by_path = {item.path: item for item in self.changed_files()}
        tracked: list[str] = []
        untracked: list[str] = []
        for path in paths:
            safe = self._relative(path)
            item = status_by_path.get(safe)
            (untracked if item and item.label == "?" else tracked).append(safe)
        if tracked:
            self._run("checkout", "--", *tracked, check=False)
        if untracked:
            self._run("clean", "-fd", "--", *untracked, check=False)
        return {"discarded": tracked + untracked, "status": self.status()}

    def show_commit(self, commit: str) -> dict[str, object]:
        """查看某次提交的改动（历史列表点开时用）。"""
        target = (commit or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{4,40}", target):
            raise GitError(f"非法的提交号：{commit}", details={"commit": target})
        meta = self._run(
            "show",
            "-s",
            "--pretty=%h%x1f%an%x1f%ad%x1f%s",
            "--date=format:%Y-%m-%d %H:%M",
            target,
            check=False,
        ).stdout.strip()
        diff = self._run("show", "--no-color", "--unified=3", target, check=False).stdout
        parts = meta.split("\x1f")
        return {
            "commit": {
                "hash": parts[0] if parts else target,
                "author": parts[1] if len(parts) > 1 else "",
                "date": parts[2] if len(parts) > 2 else "",
                "subject": parts[3] if len(parts) > 3 else "",
            },
            "diff": diff[:200_000],
        }

    # ── 差异 ──

    def diff(self, path: str, *, staged: bool = False) -> str:
        safe = self._relative(path)
        args = ["diff", "--no-color", "--unified=3"]
        if staged:
            args.append("--cached")
        args.extend(["--", safe])
        text = self._run(*args, check=False).stdout
        # 未跟踪文件没有 diff：直接给出内容片段，方便界面预览
        if not text and not staged and (self.repo / safe).is_file():
            content = (self.repo / safe).read_text(encoding="utf-8", errors="replace")
            snippet = "\n".join(f"+{line}" for line in content.splitlines()[:400])
            return f"（未跟踪文件，以下为当前内容）\n{snippet}"
        return text[:200_000]

    # ── 暂存 / 提交 ──

    def stage(self, paths: list[str]) -> None:
        targets = [self._relative(item) for item in paths] or ["."]
        self._run("add", "--", *targets)

    def unstage(self, paths: list[str]) -> None:
        targets = [self._relative(item) for item in paths] or ["."]
        self._run("reset", "-q", "HEAD", "--", *targets, check=False)

    def commit(self, message: str, *, paths: list[str] | None = None, add_all: bool = True) -> dict:
        text = (message or "").strip()
        if not text:
            raise GitError("提交信息不能为空。")
        if paths:
            self.stage(paths)
        elif add_all:
            self._run("add", "-A")
        if not self.changed_files():
            return {"committed": False, "reason": "没有需要提交的改动。"}
        self._run("commit", "-m", text)
        return {"committed": True, "commit": self._last_commit()}

    # ── 远端 ──

    def push(self) -> dict:
        result = self._run("push", check=False)
        ok = result.returncode == 0
        return {
            "ok": ok,
            "stdout": (result.stdout or "").strip()[:500],
            "stderr": (result.stderr or "").strip()[:500],
            "hint": ""
            if ok
            else "推送失败：检查远端地址、网络或凭证（token 是否过期/权限是否包含 Contents: write）。",
        }

    def pull(self) -> dict:
        result = self._run("pull", "--ff-only", check=False)
        ok = result.returncode == 0
        return {
            "ok": ok,
            "stdout": (result.stdout or "").strip()[:500],
            "stderr": (result.stderr or "").strip()[:500],
            "hint": "" if ok else "拉取失败：本地与远端可能已分叉，请先提交或手动处理。",
        }

    # ── 每日开机自动提交（Windows 计划任务）──

    # ── 代理（Windows 系统代理与 git 不通用，这里显式管理）──

    PROXY_KEY = "http.https://github.com/.proxy"

    def system_proxy(self) -> str:
        """读取 Windows「Internet 选项」里的代理（git 默认不读它）。"""
        if os.name != "nt":
            return ""
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            if not int(enabled) or not server:
                return ""
            server = str(server)
            if "=" in server:  # 形如 http=127.0.0.1:10809;https=...
                parts = dict(item.split("=", 1) for item in server.split(";") if "=" in item)
                server = parts.get("https") or parts.get("http") or ""
            if server and not server.startswith("http"):
                server = f"http://{server}"
            return server
        except (OSError, ValueError, ImportError):
            return ""

    def proxy_status(self) -> dict[str, object]:
        def build() -> dict[str, object]:
            scoped = self._run(
                "config", "--global", "--get", self.PROXY_KEY, check=False
            ).stdout.strip()
            generic = self._run(
                "config", "--global", "--get", "http.proxy", check=False
            ).stdout.strip()
            system = self.system_proxy()
            return {
                "git_proxy": scoped or generic,
                "scoped": bool(scoped),
                "generic": generic,
                "system_proxy": system,
                "active": bool(scoped or generic),
                "suggestion": ""
                if (scoped or generic)
                else (f"系统开着代理 {system}，但 git 不走它——建议一键配置。" if system else ""),
                "target": "https://github.com/",
            }

        return self._cached("proxy", 10.0, build)  # type: ignore[return-value]

    def set_proxy(self, proxy_url: str) -> dict[str, object]:
        value = (proxy_url or "").strip()
        if not value:
            raise GitError("代理地址不能为空。")
        if "://" not in value:
            value = f"http://{value}"
        scheme, _, rest = value.partition("://")
        if scheme not in ("http", "https", "socks5", "socks5h") or not rest:
            raise GitError(f"代理地址格式不正确：{proxy_url}（示例 http://127.0.0.1:10809）")
        self._run("config", "--global", self.PROXY_KEY, value)
        # 之前手动设过全局代理时，一并清掉，避免两处配置打架
        self._run("config", "--global", "--unset", "http.proxy", check=False)
        self.invalidate()
        return self.proxy_status()

    def clear_proxy(self) -> dict[str, object]:
        self._run("config", "--global", "--unset", self.PROXY_KEY, check=False)
        self.invalidate()
        return self.proxy_status()

    def auto_commit_script(self) -> Path:
        return Path(__file__).resolve().parents[2] / "scripts" / "auto-commit.cmd"

    def startup_launcher(self) -> Path:
        """启动文件夹里的启动项（登录时自动运行，无需管理员权限）。"""
        appdata = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
            / "Orchestrator-AutoCommit.cmd"
        )

    def auto_commit_status(self) -> dict[str, object]:
        launcher = self.startup_launcher()
        enabled = launcher.is_file()
        push = False
        if enabled:
            push = "push" in launcher.read_text(encoding="utf-8", errors="replace")
        return {
            "supported": True,
            "enabled": enabled,
            "mechanism": "startup-folder",
            "launcher": str(launcher),
            "script": str(self.auto_commit_script()),
            "push": push,
            "detail": "登录 Windows 时自动提交（可在「任务管理器 → 启动」里看到）"
            if enabled
            else "未开启：登录时不会自动提交。",
        }

    def enable_auto_commit(self, *, push: bool = False) -> dict[str, object]:
        script = self.auto_commit_script()
        if not script.is_file():
            raise GitError(f"找不到自动提交脚本：{script}")
        launcher = self.startup_launcher()
        launcher.parent.mkdir(parents=True, exist_ok=True)
        mode = "push" if push else "local"
        launcher.write_text(
            "@echo off\r\n"
            "REM 由编排器生成：登录 Windows 时自动提交 Git（删除本文件即关闭）\r\n"
            f'call "{script}" {mode}\r\n',
            encoding="utf-8",
        )
        status = self.auto_commit_status()
        if not status["enabled"]:
            raise GitError(f"写入启动项失败：{launcher}")
        return status

    def disable_auto_commit(self) -> dict[str, object]:
        launcher = self.startup_launcher()
        if launcher.is_file():
            launcher.unlink()
        return self.auto_commit_status()

    def run_auto_commit_now(self) -> dict[str, object]:
        """立刻执行一次自动提交逻辑（不依赖计划任务，用于验证开关效果）。"""
        result = subprocess.run(
            ["cmd", "/c", str(self.auto_commit_script()), "local"],
            cwd=str(self.repo),
            env={**os.environ, "ORCHESTRATOR_AUTOCOMMIT_REPO": str(self.repo)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        output = (result.stdout or "").strip() + (
            ("\n" + result.stderr.strip()) if result.stderr else ""
        )
        return {"ok": result.returncode == 0, "output": output[-1200:]}

    # ── 工具 ──

    def _relative(self, path: str) -> str:
        cleaned = (path or "").strip().replace("\\", "/")
        # 明确拒绝绝对路径与盘符（否则会在仓库里造出名为 "C:" 的目录）
        if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
            raise GitError(f"只接受仓库内的相对路径：{path}", details={"path": path})
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        if not cleaned or ".." in Path(cleaned).parts:
            raise GitError(f"非法路径：{path}", details={"path": path})
        target = (self.repo / cleaned).resolve()
        if target != self.repo and self.repo not in target.parents:
            raise GitError(f"路径超出仓库范围：{path}", details={"path": path})
        return cleaned


def looks_like_repo(path: Path) -> bool:
    return (Path(path) / ".git").exists() or bool(re.search(r"\.git$", str(path)))
