"""运行记录（会持久化到 ``data/runs/<id>/run.json``）。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.schemas.plan import ArchitecturePlan, CheckResult, StepCheck
from app.schemas.project import (
    DEFAULT_PROJECT_ID,
    ContextType,
    chat_context_id,
    project_context_id,
)


def _now() -> datetime:
    return datetime.now(UTC)


#: 阶段标识：架构段 / 执行段（指标账本按此区分）
PHASE_ARCHITECT = "architect"
PHASE_EXECUTOR = "executor"

#: 允许落盘的模型用量来源
USAGE_SOURCES = ("provider", "estimated", "unknown")

#: 路由白名单：除这些字段外的任何内容（尤其是 Key / 请求头）都不得进入指标
ROUTE_FIELDS: tuple[str, ...] = ("alias", "model", "protocol", "base_url")


def redact_base_url(value: str) -> str:
    """脱敏 base_url：丢掉 userinfo、查询串与锚点，只留 ``scheme://host[:port]/path``。"""
    text = str(value or "").strip()
    if "://" not in text:
        # 没有 scheme 时不猜，直接丢弃，避免把 ``user:pass@host`` 这类片段原样留下
        return ""
    parts = urlsplit(text)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    if not netloc:
        return ""
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def sanitize_route(raw: Mapping[str, Any] | None) -> dict[str, str]:
    """把任意路由字典收敛成可审计、可落盘的白名单字段。"""
    if not raw:
        return {}
    safe: dict[str, str] = {}
    for field in ROUTE_FIELDS:
        value = raw.get(field)
        if value is None or value == "":
            continue
        text = str(value)
        if field == "base_url":
            text = redact_base_url(text)
        if text:
            safe[field] = text
    return safe


class RunStatus(StrEnum):
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    #: 按批次跑到指定步骤后主动停下（可继续，区别于"取消/放弃"）
    PAUSED = "paused"
    #: 执行段明确表示"缺信息、无法继续"；用户补充后可从该步继续
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class ChangedFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    action: str = "update"
    additions: int = 0
    deletions: int = 0
    diff: str = ""
    size: int = 0
    error: str = ""


class CommandRun(BaseModel):
    """**系统实际执行过**的命令及其结果（只是"建议"的命令不会出现在这里）。"""

    model_config = ConfigDict(extra="ignore")

    cmd: str = ""
    ok: bool = False
    #: 被白名单/开关拦下：没执行，仍然只是建议
    skipped: bool = False
    exit_code: int | None = None
    duration_ms: int = 0
    output: str = ""
    truncated: bool = False
    error: str = ""


class PhaseMetrics(BaseModel):
    """按「阶段 + 步骤」聚合的可持久化指标。

    兼容性约定（旧 ``run.json`` 缺失时按下述默认值补齐）：
    - ``phase``：``architect`` / ``executor``；架构段不绑定步骤，``step_id`` 为 ``None``。
    - token 三项为 ``None`` 表示**未知**（提供方没返回 usage），不得写 0 冒充已知；
      0 只在提供方明确返回 0 时使用。
    - ``route`` 只保留白名单字段，base_url 已脱敏，任何凭据都不落盘。
    """

    model_config = ConfigDict(extra="ignore")

    phase: str = PHASE_EXECUTOR
    step_id: int | None = None
    #: 该阶段/步骤总共发起的模型调用次数（含重试）
    calls: int = 0
    #: 额外重试次数（首次尝试不计）；与 calls 分开存，便于看板直接展示
    retries: int = 0
    #: 本阶段/步骤实际发送的上下文字符数
    context_chars: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: provider（真实 usage）/ estimated（估算）/ unknown（未知）
    usage_source: str = "unknown"
    #: 未知或估算的原因，便于看板直接展示（例如 provider_no_usage）
    usage_reason: str = ""
    #: 累计耗时（毫秒）
    duration_ms: int = 0
    #: 本步上下文的构成（各段字符数）：task/brief/plan/tree/completed/current/files
    context_stats: dict[str, int] = Field(default_factory=dict)
    #: 本步的调用轮次：initial / fetch（按需索取文件）/ repair（按报错修正）
    rounds: dict[str, int] = Field(default_factory=dict)
    route: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("route", mode="before")
    @classmethod
    def _coerce_route(cls, value: Any) -> Any:
        """无论传入什么，落盘前都收敛成白名单字段，顺带挡住 Key 泄漏。"""
        if isinstance(value, Mapping):
            return sanitize_route(value)
        return {}


class RunStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str = ""
    goal: str = ""
    deliverables: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    #: 纲领声明的客观验收项（执行前由架构段给出，执行后逐条自动检查）
    checks: list[StepCheck] = Field(default_factory=list)
    #: 本次执行的客观验收结果（空 = 没有可自动判定的检查项）
    verification: list[CheckResult] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    summary: str = ""
    #: 交给下一步的接力说明（保持精炼，避免上下文随步骤线性膨胀）
    handoff: str = ""
    notes: list[str] = Field(default_factory=list)
    commands: list[dict[str, str]] = Field(default_factory=list)
    #: 系统实际执行过的验证命令（白名单内 + 用户开启命令执行时才会有内容）
    command_results: list[CommandRun] = Field(default_factory=list)
    files: list[ChangedFile] = Field(default_factory=list)
    #: 本步实际发送给执行段的上下文字符数（用于观察 token 消耗）
    context_chars: int = 0
    #: 本步执行段的重试次数（首次尝试不计；明细见 run.metrics 中同 step_id 的条目）
    retries: int = 0
    #: 本步按需索取过的文件
    fetched_files: list[str] = Field(default_factory=list)
    #: 本步注入过哪些 skill（按触发词挑中的；界面据此显示"这一步用了什么技能"）
    skills_used: list[str] = Field(default_factory=list)
    #: 本步实际执行过的 MCP 工具调用（含结果，供界面与审计查看）
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    #: 本步开始前的 git 锚点（用于"回滚这一步"；非 git 仓库时为空）
    git_snapshot: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def reset_for_rerun(self) -> None:
        """把这一步退回「从未执行」：清掉上一轮留下的**所有派生状态**。

        为什么集中成一个方法：重做这一步 / 补充信息继续 / 恢复被中断的运行 /
        回滚这一步 都要求"这一步从零再来"。以前四处的清理字段各写了一份，
        于是出现了自相矛盾的记录——``files`` 清空了、``verification`` 还留着
        （界面上"0 个文件却有 8 条验收"），``started_at``/``finished_at`` 也
        残留上一轮的时间（出现 finished_at < started_at）。
        """

        self.status = StepStatus.PENDING
        self.error = ""
        self.summary = ""
        self.handoff = ""
        self.notes = []
        self.commands = []
        self.command_results = []
        self.files = []
        self.verification = []
        self.fetched_files = []
        self.skills_used = []
        self.tool_results = []
        self.context_chars = 0
        self.retries = 0
        #: 锚点会在这一步真正开始执行时重新拍一张，旧的锚点留着只会误导回滚
        self.git_snapshot = {}
        self.started_at = None
        self.finished_at = None


class RunMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    phase: str = "architect"
    model: str = ""
    content: str = ""
    created_at: datetime = Field(default_factory=_now)


class Run(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""
    task: str = ""
    #: 所属项目：项目内的运行一律带稳定 project_id；历史数据缺省归到 default
    project_id: str = DEFAULT_PROJECT_ID
    #: 上下文类型：``project`` = 项目内运行；``chat`` = 普通对话（不进入编排）
    context_type: ContextType = "project"
    #: 普通对话的**承接摘要**：被折叠掉的历史压成的一段背景（项目运行不用它）
    summary: str = ""
    #: 已经折叠掉的轮数（用于界面提示"已折叠 n 轮"）
    folded_turns: int = 0
    #: 上一次回答实际发送的上下文字符数（让它"省了多少"可见）
    last_context_chars: int = 0
    #: 承接链：自动开新会话时，新会话记 prev，旧会话记 next，两边都能点回去
    prev_session_id: str = ""
    next_session_id: str = ""
    #: 置顶（Codex 式任务列表管理）
    pinned: bool = False
    #: 归档：默认不出现在列表里，但记录保留
    archived: bool = False
    #: 前期沟通简报（比 task 长，架构段与执行段都会作为稳定背景读入）
    brief: str = ""
    #: 运行类型：``task`` = 走"纲领 → 确认 → 执行"；``chat`` = 判定为问答，直接回答
    kind: str = "task"
    status: RunStatus = RunStatus.PLANNING
    workspace_dir: str = ""
    target_dir: str = ""
    plan: ArchitecturePlan | None = None
    plan_raw: str = ""
    plan_revision: int = 0
    steps: list[RunStep] = Field(default_factory=list)
    messages: list[RunMessage] = Field(default_factory=list)
    #: 运行过程中用户补充的说明（resume 时追加，执行段会看到）
    user_notes: list[str] = Field(default_factory=list)
    route: dict[str, Any] = Field(default_factory=dict)
    #: 执行到该步骤后暂停（用于分批交付；None = 一路跑完）
    stop_after_step: int | None = None
    #: 指标账本：架构段一条（step_id=None），执行段每步一条；旧 run.json 缺省为空列表
    metrics: list[PhaseMetrics] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def metrics_for(self, phase: str, step_id: int | None = None) -> PhaseMetrics:
        """取出（必要时新建）指定阶段/步骤的指标条目。

        ``phase`` 与 ``step_id`` 同时相同的条目视为同一条；架构段固定 ``step_id=None``。
        """
        for item in self.metrics:
            if item.phase == phase and item.step_id == step_id:
                return item
        item = PhaseMetrics(phase=phase, step_id=step_id)
        self.metrics.append(item)
        return item

    @property
    def is_chat(self) -> bool:
        """普通对话：只收发消息，没有纲领、步骤与工作区。"""

        return self.context_type == "chat" or self.kind == "chat"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def context_id(self) -> str:
        """上下文标识：``project:<projectId>`` 或 ``chat:<sessionId>``。

        用 ``computed_field`` 而不是普通属性：接口返回的 run JSON 里必须带上它，
        界面才能据此判断"这条属于哪个项目 / 是不是普通对话"。
        """

        if self.context_type == "chat":
            return chat_context_id(self.id)
        return project_context_id(self.project_id)

    def metrics_summary(self) -> dict[str, Any]:
        """运行级指标汇总。

        token 在**完全未知**时返回 ``None``（而不是 0），并列出哪些阶段/步骤缺 usage。
        """

        def _known(items: list[PhaseMetrics], field: str) -> int | None:
            values: list[int | None] = [getattr(item, field) for item in items]
            known: list[int] = [value for value in values if value is not None]
            return sum(known) if known else None

        def _block(items: list[PhaseMetrics]) -> dict[str, Any]:
            return {
                "calls": sum(item.calls for item in items),
                "retries": sum(item.retries for item in items),
                "context_chars": sum(item.context_chars for item in items),
                "duration_ms": sum(item.duration_ms for item in items),
                "prompt_tokens": _known(items, "prompt_tokens"),
                "completion_tokens": _known(items, "completion_tokens"),
                "total_tokens": _known(items, "total_tokens"),
            }

        architect = [item for item in self.metrics if item.phase == PHASE_ARCHITECT]
        executor = [item for item in self.metrics if item.phase != PHASE_ARCHITECT]
        return {
            "architect": _block(architect),
            "executor": _block(executor),
            "unknown_usage": [
                {"phase": item.phase, "step_id": item.step_id, "reason": item.usage_reason}
                for item in self.metrics
                if item.total_tokens is None
            ],
        }

    def summarize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "task": self.task,
            "kind": self.kind,
            "project_id": self.project_id,
            "context_type": self.context_type,
            "context_id": self.context_id,
            "pinned": self.pinned,
            "archived": self.archived,
            "status": self.status.value,
            "steps_total": len(self.steps),
            "steps_done": sum(1 for s in self.steps if s.status == StepStatus.DONE),
            "files_changed": sum(len(s.files) for s in self.steps),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
