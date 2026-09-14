"""纲领（架构段产物）的数据结构。

字段刻意保持"可验收"：每一步都要能独立判断是否完成。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
