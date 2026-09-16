"""能力注册表：``data/capabilities/registry.json`` + 审计日志 ``audit.jsonl``。

三种形态（skill / mcp / plugin）共用这一份存储与一套状态；各形态自己的字段放在 ``meta``。
历史插件（``data/plugins/installed.json``）在启动时**镜像**进来（``kind=plugin``），
这样"能力中心"一开始就能看到已有插件，而不用等 P1/P2。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.core.errors import AppError, NotFoundError
from app.schemas.capability import Capability, CapabilityKind, CapabilityScope

STATE_VERSION = 1
MAX_AUDIT_BYTES = 2 * 1024 * 1024

#: 列表顺序按形态固定（与能力中心的分页顺序一致）：skill → mcp → plugin
KIND_ORDER: dict[str, int] = {"skill": 0, "mcp": 1, "plugin": 2}


def resolve_callable_mcp(
    registry: CapabilityRegistry, capability_id: str, *, project_id: str = ""
) -> Capability:
    """取一个**可调用**的 MCP 服务器；三道闸门缺一不可：启用 → 作用域匹配 → 已确认信任。

    手动调用（能力中心）与模型自主调用（执行段 tool_calls）共用这一个入口，
    不存在"面板能跑、模型不能跑"的偏差。
    """

    capability = registry.require(capability_id)
    if capability.kind is not CapabilityKind.MCP:
        raise AppError("这不是 MCP 服务器。", code="not_an_mcp_server")
    if not capability.enabled:
        raise AppError("这个 MCP 服务器还没启用：到「能力中心 → MCP」打开它。", code="mcp_disabled")
    if (
        capability.scope is CapabilityScope.PROJECT
        and project_id
        and capability.project_id != project_id
    ):
        raise AppError(
            f"这个 MCP 服务器只对项目 {capability.project_id} 生效。",
            code="mcp_scope_mismatch",
        )
    if not capability.meta.get("trusted"):
        raise AppError(
            "这个 MCP 服务器还没确认信任：它会以本机权限运行，"
            "请到「能力中心 → MCP」点一次「确认信任」再让它执行工具。",
            code="mcp_needs_trust",
        )
    return capability


def _now() -> datetime:
    return datetime.now(UTC)


class CapabilityRegistry:
    """已装能力的唯一读写入口（原子写，坏数据不静默丢）。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._path = self._root / "registry.json"
        self._audit_path = self._root / "audit.jsonl"
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    # ── 读写 ──

    def _read(self) -> dict[str, Capability]:
        if not self._path.is_file():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        raw = payload.get("capabilities") if isinstance(payload, dict) else payload
        out: dict[str, Capability] = {}
        for item in raw or []:
            try:
                capability = Capability.model_validate(item)
            except Exception:  # noqa: BLE001 - 单条坏数据不该让整表不可用
                continue
            out[capability.id] = capability
        return out

    def _write(self, capabilities: dict[str, Capability]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "capabilities": [
                item.model_dump(mode="json")
                for item in sorted(
                    capabilities.values(),
                    key=lambda c: (KIND_ORDER.get(c.kind.value, 9), c.id),
                )
            ],
        }
        tmp = self._path.with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # ── 查询 ──

    def list(
        self,
        *,
        kind: str | None = None,
        project_id: str | None = None,
        include_disabled: bool = True,
    ) -> list[Capability]:
        with self._lock:
            items = list(self._read().values())
        out: list[Capability] = []
        for item in items:
            if kind and item.kind.value != kind:
                continue
            if not include_disabled and not item.enabled:
                continue
            if (
                project_id
                and item.scope is CapabilityScope.PROJECT
                and item.project_id != project_id
            ):
                continue
            out.append(item)
        out.sort(key=lambda item: (KIND_ORDER.get(item.kind.value, 9), item.id))
        return out

    def get(self, capability_id: str) -> Capability | None:
        with self._lock:
            return self._read().get(str(capability_id or "").strip().lower())

    def require(self, capability_id: str) -> Capability:
        capability = self.get(capability_id)
        if capability is None:
            raise NotFoundError(
                f"能力不存在：{capability_id}",
                code="capability_not_found",
                details={"capability_id": capability_id},
            )
        return capability

    def active_for(self, project_id: str = "", *, kind: str | None = None) -> list[Capability]:
        """某个项目当前**生效**的能力（全局启用 + 该项目启用的）。"""

        return [item for item in self.list(kind=kind) if item.is_active_for(project_id)]

    # ── 写入 ──

    def upsert(self, capability: Capability, *, record: str = "install") -> Capability:
        with self._lock:
            items = self._read()
            existing = items.get(capability.id)
            if existing is not None:
                capability = capability.model_copy(
                    update={
                        "installed_at": existing.installed_at,
                        "last_used_at": existing.last_used_at,
                    }
                )
            capability.updated_at = _now()
            items[capability.id] = capability
            self._write(items)
        self.audit({"event": record, "id": capability.id, "kind": capability.kind.value})
        return capability

    def set_enabled(self, capability_id: str, enabled: bool) -> Capability:
        with self._lock:
            items = self._read()
            key = str(capability_id or "").strip().lower()
            capability = items.get(key)
            if capability is None:
                raise NotFoundError(
                    f"能力不存在：{capability_id}",
                    code="capability_not_found",
                    details={"capability_id": capability_id},
                )
            capability = capability.model_copy(
                update={"enabled": bool(enabled), "updated_at": _now()}
            )
            items[key] = capability
            self._write(items)
        self.audit({"event": "enable" if enabled else "disable", "id": key})
        return capability

    def remove(self, capability_id: str) -> None:
        with self._lock:
            items = self._read()
            key = str(capability_id or "").strip().lower()
            if key not in items:
                raise NotFoundError(
                    f"能力不存在：{capability_id}",
                    code="capability_not_found",
                    details={"capability_id": capability_id},
                )
            del items[key]
            self._write(items)
        self.audit({"event": "uninstall", "id": key})

    def touch(self, capability_id: str) -> None:
        """记录一次使用（面板/工具调用），用于"最近用过"排序与审计。"""

        with self._lock:
            items = self._read()
            key = str(capability_id or "").strip().lower()
            capability = items.get(key)
            if capability is None:
                return
            items[key] = capability.model_copy(update={"last_used_at": _now()})
            self._write(items)

    # ── 审计 ──

    def audit(self, event: dict[str, object]) -> None:
        """追加一行审计（超过 2MB 轮转一次），失败不影响主流程。"""

        try:
            self._root.mkdir(parents=True, exist_ok=True)
            if self._audit_path.is_file() and self._audit_path.stat().st_size > MAX_AUDIT_BYTES:
                self._audit_path.replace(self._audit_path.with_suffix(".jsonl.1"))
            line = json.dumps({"at": _now().isoformat(), **event}, ensure_ascii=False)
            with self._audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError:
            return

    def read_audit(self, *, limit: int = 100) -> list[dict[str, object]]:
        if not self._audit_path.is_file():
            return []
        lines = self._audit_path.read_text(encoding="utf-8", errors="replace").splitlines()
        out: list[dict[str, object]] = []
        for line in lines[-max(1, limit) :]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # ── 历史插件镜像 ──

    def sync_plugins(self, plugins: Iterable[object]) -> int:
        """把已装插件**完整镜像**成 ``kind=plugin`` 的能力（插件仓库是它的权威存储）。

        幂等：补进来 / 更新展示与启用状态 / 把已经不在插件仓库里的镜像删掉。
        只碰 ``kind=plugin`` 的条目，skill 与 MCP 不受影响。
        """

        changed = 0
        with self._lock:
            items = self._read()
            seen: set[str] = set()
            for plugin in plugins:
                data = plugin.describe() if hasattr(plugin, "describe") else dict(plugin or {})
                plugin_id = str(data.get("id") or "").strip().lower()
                if not plugin_id:
                    continue
                capability_id = f"plugin.{plugin_id}"
                seen.add(capability_id)
                meta = {
                    "plugin_id": plugin_id,
                    "version": data.get("version", ""),
                    "contribution": data.get("contribution") or {},
                    "capabilities": data.get("capabilities") or [],
                }
                candidate = Capability(
                    id=capability_id,
                    kind=CapabilityKind.PLUGIN,
                    name=str(data.get("name") or plugin_id),
                    description=str(data.get("description") or ""),
                    version=str(data.get("version") or ""),
                    enabled=bool(data.get("enabled", True)),
                    scope=CapabilityScope.GLOBAL,
                    permissions=["ui.slot"],
                    meta=meta,
                )
                existing = items.get(capability_id)
                if existing is not None and existing.model_dump(
                    exclude={"updated_at", "installed_at", "last_used_at"}
                ) == candidate.model_dump(exclude={"updated_at", "installed_at", "last_used_at"}):
                    continue
                items[capability_id] = candidate
                changed += 1
            # 插件被卸载后，镜像也要跟着消失（否则能力中心会留着一条假记录）
            for capability_id, item in list(items.items()):
                if item.kind is CapabilityKind.PLUGIN and capability_id not in seen:
                    del items[capability_id]
                    changed += 1
            if changed:
                self._write(items)
        if changed:
            self.audit({"event": "sync_plugins", "count": changed})
        return changed
