"""纲领（架构段产物）的数据结构。

字段刻意保持"可验收"：每一步都要能独立判断是否完成。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 允许的客观检查类型（全部不执行任意命令，见 app/services/verify.py）
CHECK_TYPES: tuple[str, ...] = (
    "file_exists",
    "dir_exists",
    "glob",
    "file_contains",
    "py_compile",
    "json_valid",
)

_CHECK_ALIASES = {
    "exists": "file_exists",
    "file": "file_exists",
    "file_exist": "file_exists",
    "exists_file": "file_exists",
    "dir": "dir_exists",
    "directory_exists": "dir_exists",
    "contains": "file_contains",
    "file_include": "file_contains",
    "python": "py_compile",
    "py": "py_compile",
    "compile": "py_compile",
    "json": "json_valid",
    "valid_json": "json_valid",
    "glob_match": "glob",
}


class StepCheck(BaseModel):
    """纲领为某一步声明的**客观验收项**。

    只允许 ``CHECK_TYPES`` 里的类型：它们都能在没有副作用、不执行代码的前提下判定，
    所以可以在每步执行完立刻自动跑。命令类验收（pytest 等）需要用户审批，不在本轮范围。
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal[
        "file_exists",
        "dir_exists",
        "glob",
        "file_contains",
        "py_compile",
        "json_valid",
    ] = "file_exists"
    path: str = ""
    #: ``file_contains`` 需要的原文片段
    text: str = ""
    #: 人类可读说明；为空时由检查类型自动生成
    label: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"type": "file_exists", "path": value}
        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_type = str(data.get("type") or data.get("kind") or "file_exists").strip().lower()
        raw_type = _CHECK_ALIASES.get(raw_type, raw_type)
        data["type"] = raw_type if raw_type in CHECK_TYPES else "file_exists"
        data["path"] = str(
            data.get("path") or data.get("file") or data.get("pattern") or data.get("target") or ""
        ).strip()
        data["text"] = str(
            data.get("text")
            or data.get("value")
            or data.get("contains")
            or data.get("needle")
            or ""
        )
        data["label"] = str(data.get("label") or data.get("description") or data.get("why") or "")
        return data


class CheckResult(BaseModel):
    """一次客观检查的执行结果（会随运行记录落盘，供界面与报告展示）。"""

    model_config = ConfigDict(extra="ignore")

    type: str = ""
    path: str = ""
    text: str = ""
    label: str = ""
    ok: bool = False
    detail: str = ""


class PlanComponent(BaseModel):
    """架构组件：职责 + 对外接口。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    responsibility: str = ""
    interfaces: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    """可独立执行与验收的一步。"""

    model_config = ConfigDict(extra="ignore")

    id: int = 0
    title: str = ""
    goal: str = ""
    deliverables: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    #: 客观验收项：执行完会逐条自动检查，不通过就不算完成
    checks: list[StepCheck] = Field(default_factory=list)
    depends_on: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"title": value, "goal": value}
        if isinstance(value, dict):
            data = dict(value)
            data["title"] = str(data.get("title") or data.get("name") or "")
            data["goal"] = str(data.get("goal") or data.get("description") or data["title"])
            data["deliverables"] = _as_str_list(data.get("deliverables"))
            data["acceptance"] = _as_str_list(
                data.get("acceptance") or data.get("acceptance_criteria")
            )
            raw_checks = data.get("checks") or data.get("verifications") or []
            if isinstance(raw_checks, dict):
                raw_checks = [raw_checks]
            data["checks"] = [
                item if isinstance(item, dict) else item for item in raw_checks if item
            ]
            data["depends_on"] = _as_int_list(data.get("depends_on"))
            return data
        return value


class ArchitecturePlan(BaseModel):
    """纲领性架构。"""

    model_config = ConfigDict(extra="ignore")

    goal: str = ""
    summary: str = ""
    principles: list[str] = Field(default_factory=list)
    components: list[PlanComponent] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data["principles"] = _as_str_list(data.get("principles"))
        data["risks"] = _as_str_list(data.get("risks"))
        data["open_questions"] = _as_str_list(data.get("open_questions"))
        data["components"] = [
            item if isinstance(item, dict) else {"name": str(item)}
            for item in (data.get("components") or [])
        ]
        raw_steps = data.get("steps") or data.get("plan") or []
        if isinstance(raw_steps, dict):
            raw_steps = [raw_steps]
        steps: list[dict[str, Any]] = []
        for index, item in enumerate(raw_steps, start=1):
            if isinstance(item, str):
                step = {"title": item, "goal": item}
            elif isinstance(item, dict):
                step = dict(item)
            else:
                continue
            step["id"] = _coerce_int(step.get("id")) or index
            steps.append(step)
        data["steps"] = steps
        return data

    def normalize(self) -> ArchitecturePlan:
        """重排步骤 id，保证 1..n 连续且依赖指向存在。"""
        remap: dict[int, int] = {}
        for new_id, step in enumerate(self.steps, start=1):
            remap[step.id] = new_id
        for new_id, step in enumerate(self.steps, start=1):
            step.id = new_id
            step.depends_on = sorted(
                {remap[d] for d in step.depends_on if d in remap and remap[d] < new_id}
            )
        return self


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _as_int_list(value: Any) -> list[int]:
    out: list[int] = []
    if value is None:
        return out
    items = value if isinstance(value, (list, tuple)) else [value]
    for item in items:
        parsed = _coerce_int(item)
        if parsed is not None:
            out.append(parsed)
    return out


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        return int(digits) if digits else None
    return None
