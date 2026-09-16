"""项目仓库：项目是「架构 / 执行 / 计划 / 步骤 / 验证 / 日志 / 设置」的唯一容器。

为什么单独一份存储：运行记录是"一次做了什么的账本"，项目是"长期存在的容器"（名字、
工作区绑定、状态、最近活动）。两者生命周期不同，混在一起会让"项目"退化成运行记录的别名
——那正是上一轮只做出界面壳子的原因。

约定（对齐 ``docs/project-context-contract.md``）：

* ``project_id`` 是稳定 ID，不含 ``project:`` 前缀；前缀只出现在 ``context_id`` 里；
* 项目与工作区**一一绑定**（一个项目一个根目录），运行默认落在这个根目录里；
* 历史数据回落 ``project:default``，所以我们保证默认项目始终存在，不做数据迁移。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.core.errors import AppError, NotFoundError
from app.schemas.project import (
    PROJECT_ID_PATTERN,
    Project,
    ProjectContextResolver,
    ProjectStatus,
    ProjectWorkspace,
)

#: 存储格式版本（将来改结构时用于迁移判断）
STATE_VERSION = 1

_ID_RE = re.compile(PROJECT_ID_PATTERN)

#: 默认项目的展示名；历史运行（没有 project_id 的记录）都归到它下面
DEFAULT_PROJECT_NAME = "默认项目"


def _now() -> datetime:
    return datetime.now(UTC)


def slugify_project_id(name: str) -> str:
    """从项目名生成合法 ID；生成不出来（全中文等）时退回随机短号。"""

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-._").lower()
    slug = slug[:48].strip("-._")
    if not slug or not _ID_RE.match(slug):
        return f"p-{secrets.token_hex(3)}"
    return slug


class ProjectStore:
    """``data/projects.json`` 的读写：原子替换，坏数据不静默丢。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self.ensure_default()

    @property
    def path(self) -> Path:
        return self._path

    # ── 读写 ──

    def _read(self) -> dict[str, Project]:
        if not self._path.is_file():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        raw = payload.get("projects") if isinstance(payload, dict) else payload
        projects: dict[str, Project] = {}
        for item in raw or []:
            try:
                project = Project.model_validate(item)
            except Exception:  # noqa: BLE001 - 单条坏数据不该让整个仓库不可用
                continue
            projects[project.project_id] = project
        return projects

    def _write(self, projects: dict[str, Project]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "projects": [
                item.model_dump(mode="json")
                for item in sorted(projects.values(), key=lambda p: p.project_id)
            ],
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # ── 查询 ──

    def ensure_default(self) -> Project:
        """保证默认项目存在：历史运行（缺 project_id）需要一个明确归属。"""

        with self._lock:
            projects = self._read()
            existing = projects.get("default")
            if existing is not None and existing.name.strip():
                return existing
            project = existing or Project(
                project_id="default",
                name=DEFAULT_PROJECT_NAME,
                description="未指定项目的历史运行与默认落地位置",
                created_at=_now(),
                updated_at=_now(),
                last_activity_at=_now(),
            )
            projects[project.project_id] = project
            self._write(projects)
            return project

    def list(self, *, include_archived: bool = False, query: str = "") -> list[Project]:
        """按最近活动倒序列出项目（默认项目排最后）。"""

        with self._lock:
            projects = list(self._read().values())
        keyword = (query or "").strip().lower()
        out: list[Project] = []
        for project in projects:
            if not include_archived and project.status is not ProjectStatus.ACTIVE:
                continue
            if keyword and keyword not in f"{project.name}\n{project.project_id}".lower():
                continue
            out.append(project)
        out.sort(
            key=lambda item: (item.project_id != "default", item.last_activity_at),
            reverse=True,
        )
        return out

    def get(self, project_id: str) -> Project | None:
        with self._lock:
            return self._read().get(str(project_id or "").strip())

    def require(self, project_id: str) -> Project:
        normalized = str(project_id or "").strip()
        if not normalized:
            raise AppError("缺少 project_id", code="missing_project_context")
        project = self.get(normalized)
        if project is None or project.status is ProjectStatus.DELETED:
            raise NotFoundError(
                f"项目不存在：{normalized}",
                code="project_not_found",
                details={"project_id": normalized},
            )
        return project

    def resolver(self) -> ProjectContextResolver:
        """给契约层的上下文解析用（跨项目读取会被它拒掉）。"""

        with self._lock:
            projects = dict(self._read())
        return ProjectContextResolver(projects)

    # ── 写入 ──

    def create(
        self,
        name: str,
        *,
        root_path: str | Path | None = None,
        description: str = "",
        project_id: str = "",
    ) -> Project:
        title = (name or "").strip()
        if not title:
            raise AppError("项目名称不能为空。", code="invalid_request")

        with self._lock:
            projects = self._read()
            candidate = (project_id or "").strip() or slugify_project_id(title)
            if not _ID_RE.match(candidate):
                raise AppError(
                    f"项目 ID 不合法：{candidate}（只允许字母、数字、. _ -，且以字母或数字开头）",
                    code="invalid_project_id",
                )
            if candidate in projects:
                raise AppError(f"项目已存在：{candidate}", code="project_exists")

            workspace = self._bind_workspace(candidate, root_path)
            project = Project(
                project_id=candidate,
                name=title,
                description=(description or "").strip() or None,
                workspace=workspace,
                created_at=_now(),
                updated_at=_now(),
                last_activity_at=_now(),
            )
            projects[candidate] = project
            self._write(projects)
            return project

    def _bind_workspace(self, project_id: str, root_path: str | Path | None) -> ProjectWorkspace:
        """把项目绑到一个工作区根目录；没给就分配 ``data/projects/<id>/workspace``。"""

        raw = str(root_path or "").strip()
        if raw:
            target = Path(raw).expanduser().resolve()
            if target.parent == target:
                raise AppError("拒绝把磁盘根目录作为项目工作区。", code="invalid_project_workspace")
            if target.exists() and not target.is_dir():
                raise AppError(f"工作区必须是目录：{target}", code="invalid_project_workspace")
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AppError(
                    f"无法创建项目工作区：{target}（{exc}）",
                    code="invalid_project_workspace",
                ) from exc
            label = target.name or str(target)
        else:
            target = self._path.parent / "projects" / project_id / "workspace"
            target.mkdir(parents=True, exist_ok=True)
            label = "项目工作区"
        return ProjectWorkspace(
            workspace_id=project_id,
            root_path=str(target),
            label=label,
            bound_at=_now(),
        )

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        root_path: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Project:
        with self._lock:
            projects = self._read()
            key = str(project_id or "").strip()
            project = projects.get(key)
            if project is None:
                raise NotFoundError(
                    f"项目不存在：{key}",
                    code="project_not_found",
                    details={"project_id": key},
                )
            updates: dict[str, object] = {"updated_at": _now()}
            if name is not None:
                title = name.strip()
                if not title:
                    raise AppError("项目名称不能为空。", code="invalid_request")
                updates["name"] = title
            if description is not None:
                updates["description"] = description.strip() or None
            if status is not None:
                try:
                    updates["status"] = ProjectStatus(status)
                except ValueError as exc:
                    raise AppError(
                        f"项目状态不合法：{status}", code="invalid_project_status"
                    ) from exc
            if root_path is not None:
                updates["workspace"] = self._bind_workspace(key, root_path)
            updated = project.model_copy(update=updates)
            projects[key] = updated
            self._write(projects)
            return updated

    def archive(self, project_id: str) -> Project:
        """归档而不是物理删除：运行记录与会话数据都还在，随时能恢复。"""

        return self.update(project_id, status=ProjectStatus.ARCHIVED.value)

    def touch(self, project_id: str) -> Project | None:
        """记录一次活动（新建运行 / 发消息时调用），用于项目列表排序。"""

        with self._lock:
            projects = self._read()
            key = str(project_id or "").strip()
            project = projects.get(key)
            if project is None:
                return None
            moment = _now()
            updated = project.model_copy(update={"last_activity_at": moment, "updated_at": moment})
            projects[key] = updated
            self._write(projects)
            return updated
