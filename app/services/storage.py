"""运行记录持久化（每运行一个目录，读写原子替换）。"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.core.errors import NotFoundError
from app.schemas.run import Run


def new_run_id() -> str:
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


class RunStore:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def run_dir(self, run_id: str) -> Path:
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
        if safe != run_id or not safe:
            raise NotFoundError(f"运行 ID 非法：{run_id}", details={"run_id": run_id})
        return self.runs_dir / safe

    def workspace_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "workspace"

    def backup_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "backup"

    def save(self, run: Run) -> None:
        """原子写入 ``run.json``：先写同目录临时文件并落盘，再整体替换。

        任何一步失败（序列化、写盘、替换）都不会让 ``run.json`` 变成半写状态：
        失败时清理临时文件，并原样保留上一份可解析的记录。
        """
        with self._lock:
            run.updated_at = datetime.now(UTC)
            directory = self.run_dir(run.id)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "run.json"
            tmp = directory / "run.json.tmp"
            try:
                payload = json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2)
                with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.replace(target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

    def exists(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "run.json").is_file()

    def load(self, run_id: str) -> Run:
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            raise NotFoundError(f"未找到该运行：{run_id}", details={"run_id": run_id})
        return Run.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(
        self,
        *,
        include_archived: bool = False,
        query: str = "",
        project_id: str | None = None,
        kind: str | None = None,
        context_type: str | None = None,
    ) -> list[dict[str, object]]:
        """列出运行摘要。

        ``project_id`` / ``kind`` 用于按项目边界与上下文类型过滤——左侧栏「项目 → 运行记录」
        只能看到本项目的运行，普通对话只列 ``kind == "chat"`` 的会话。
        """

        summaries = [
            run.summarize()
            for run in self.iter_runs(
                include_archived=include_archived,
                query=query,
                project_id=project_id,
                kind=kind,
                context_type=context_type,
            )
        ]
        # 置顶优先，其次按更新时间倒序（Codex 式任务列表）
        summaries.sort(
            key=lambda item: (bool(item.get("pinned")), str(item.get("updated_at"))),
            reverse=True,
        )
        return summaries

    def iter_runs(
        self,
        *,
        include_archived: bool = False,
        query: str = "",
        project_id: str | None = None,
        kind: str | None = None,
        context_type: str | None = None,
    ) -> list[Run]:
        """按过滤条件取出**完整运行记录**（项目模块装配需要纲领、步骤与验证明细）。

        ``project_id`` 只匹配 **项目上下文** 的运行：普通对话不属于任何项目，
        即使它的 ``project_id`` 还是默认值，也不该出现在项目的运行列表里。
        """

        runs: list[Run] = []
        for path in self.runs_dir.glob("*/run.json"):
            try:
                run = Run.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if run.archived and not include_archived:
                continue
            if project_id is not None and (
                run.context_type != "project" or run.project_id != project_id
            ):
                continue
            if kind is not None and run.kind != kind:
                continue
            if context_type is not None and run.context_type != context_type:
                continue
            if query and query.lower() not in f"{run.title}\n{run.task}".lower():
                continue
            runs.append(run)
        return runs

    def delete(self, run_id: str) -> None:
        """物理删除一条运行（含工作区与备份）。只给"删掉自己的对话"这类场景用。"""

        directory = self.run_dir(run_id)
        if not directory.is_dir():
            raise NotFoundError(f"未找到该运行：{run_id}", details={"run_id": run_id})
        with self._lock:
            shutil.rmtree(directory, ignore_errors=False)
