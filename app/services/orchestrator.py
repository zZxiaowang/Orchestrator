"""编排器：把架构段与执行段串成一条可控的状态机。

状态流转：

``planning`` → ``awaiting_approval`` →（人工确认）→ ``executing`` → ``done``

任何一段失败都会落到 ``failed`` 并保留上下文，便于在界面上直接查看与重试。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Endpoint, Settings, get_settings
from app.core.errors import AppError, ConfigurationError, NotFoundError, WorkspaceError
from app.core.relay import CallStats, RelayClient
from app.schemas.run import (
    PHASE_ARCHITECT,
    PHASE_EXECUTOR,
    ChangedFile,
    CommandRun,
    Run,
    RunMessage,
    RunStatus,
    RunStep,
    StepStatus,
)
from app.schemas.step import StepOutput
from app.services.architect import (
    build_architect_messages,
    build_continue_messages,
    run_architect,
    run_chat,
)
from app.services.commands import failure_block, run_allowed
from app.services.context import StepContextBuilder, clip, clip_head_tail
from app.services.events import EventBus
from app.services.executor import EXECUTOR_SYSTEM, build_step_messages, run_step
from app.services.gitguard import revert_paths, snapshot
from app.services.intent import detect_intent
from app.services.metrics import apply_call, route_of
from app.services.storage import RunStore, new_run_id
from app.services.verify import effective_checks, failure_text, run_checks, summarize
from app.services.workspace import Workspace


def message_chars(messages: Sequence[dict[str, Any]]) -> int:
    """本次调用实际发送的上下文字符数（指标账本用）。"""

    total = 0
    for item in messages:
        content = item.get("content")
        total += len(content) if isinstance(content, str) else len(str(content or ""))
    return total


class Orchestrator:
    def __init__(
        self,
        store: RunStore,
        bus: EventBus,
        *,
        settings_provider: Callable[[], Settings] = get_settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self._settings_provider = settings_provider
        self._transport = transport
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()

    # ── 生命周期 ──

    def create_run(
        self,
        task: str,
        *,
        title: str = "",
        target_dir: str = "",
        context: str = "",
        brief: str = "",
    ) -> Run:
        task = (task or "").strip()
        if not task:
            raise AppError("任务描述不能为空。", code="invalid_request")

        settings = self._settings_provider()
        run_id = new_run_id()
        workspace_path, target = self._resolve_workspace(run_id, target_dir)

        run = Run(
            id=run_id,
            title=title.strip() or task.splitlines()[0][:60],
            task=task,
            brief=brief.strip(),
            status=RunStatus.PLANNING,
            workspace_dir=str(workspace_path),
            target_dir=str(target) if target else "",
            route=self.describe_route(settings),
        )
        run.messages.append(RunMessage(role="user", phase="task", content=task))
        if context.strip():
            run.messages.append(RunMessage(role="user", phase="context", content=context.strip()))
            run.user_notes.append(context.strip())
        self.store.save(run)
        return run

    def _resolve_workspace(self, run_id: str, target_dir: str) -> tuple[Path, Path | None]:
        run_workspace = self.store.workspace_dir(run_id)
        raw = (target_dir or "").strip()
        if not raw:
            return run_workspace, None
        target = Path(raw).expanduser().resolve()
        if not target.exists() or not target.is_dir():
            raise WorkspaceError(
                f"目标目录不存在或不是目录：{target}",
                details={"target_dir": str(target)},
            )
        if target.parent == target:
            raise WorkspaceError(
                "拒绝把磁盘根目录作为工作区。", details={"target_dir": str(target)}
            )
        return target, target

    # ── 架构段 ──

    def start_planning(self, run_id: str, *, feedback: str | None = None) -> None:
        self._cancelled.discard(run_id)
        self._tasks[run_id] = asyncio.create_task(self._plan(run_id, feedback=feedback))

    async def _plan(self, run_id: str, *, feedback: str | None) -> None:
        run = self.store.load(run_id)
        settings = self._settings_provider()
        endpoint = settings.resolve_architect()
        try:
            self._require_endpoint(endpoint)
            run.status = RunStatus.PLANNING
            run.error = None
            self.store.save(run)
            self.bus.publish(
                run_id,
                "status",
                status=run.status.value,
                message="正在判断这是需求还是问答…",
            )

            client = self._client(endpoint, settings)
            stats = CallStats()

            # 先分流：问答（"你是哪个模型"）不该走"纲领 → 确认 → 执行"这条重流程。
            # 判错方向的代价不对称：把真需求当问答会丢掉工作，把问答当需求只是多跑一次。
            intent = await detect_intent(
                client,
                run.task,
                model=endpoint.model,
                brief=run.brief,
                stats=stats,
            )
            if intent.is_chat:
                await self._answer_chat(run, client, endpoint, intent, stats)
                return

            self.bus.publish(
                run_id,
                "status",
                status=run.status.value,
                message="架构段（GPT）正在产出纲领…",
            )
            context = self._build_context(run, settings)
            messages = build_architect_messages(
                run.task,
                max_steps=settings.max_plan_steps,
                feedback=feedback,
                context=context,
                previous_plan=run.plan_raw if feedback else None,
            )
            if feedback and run.plan_revision:
                run.plan_revision += 1
                self.bus.publish(
                    run_id, "status", status=run.status.value, message="按反馈修订纲领…"
                )

            try:
                plan, raw = await run_architect(
                    client,
                    messages,
                    model=endpoint.model,
                    stats=stats,
                    on_token=lambda text: self.bus.publish(
                        run_id, "token", phase="architect", model=endpoint.model, text=text
                    ),
                )
            finally:
                # 成功与失败都要记账：否则失败运行在统计里是"零成本"，用户无从判断值不值得重试
                self._record_metrics(
                    run,
                    stats,
                    endpoint,
                    phase=PHASE_ARCHITECT,
                    context_chars=message_chars(messages),
                )

            plan = plan.model_copy(
                update={
                    "steps": plan.steps[: settings.max_plan_steps],
                }
            )
            plan.normalize()

            if run.plan_revision == 0:
                run.plan_revision = 1
            run.plan = plan
            run.plan_raw = raw
            run.steps = [
                RunStep(
                    id=step.id,
                    title=step.title,
                    goal=step.goal,
                    deliverables=list(step.deliverables),
                    acceptance=list(step.acceptance),
                    checks=list(step.checks),
                )
                for step in plan.steps
            ]
            run.status = RunStatus.AWAITING_APPROVAL
            run.messages.append(
                RunMessage(
                    role="assistant",
                    phase="architect",
                    model=endpoint.model,
                    content=raw,
                )
            )
            self.store.save(run)
            self._write_plan_doc(run)
            self.bus.publish(
                run_id,
                "plan",
                plan=run.plan.model_dump(mode="json") if run.plan else {},
                steps=[step.model_dump(mode="json") for step in run.steps],
                raw=raw,
                message="纲领已生成，等待确认后执行。",
            )
            self.bus.publish(run_id, "status", status=run.status.value)
        except AppError as exc:
            self._fail(run, exc)
        except asyncio.CancelledError:  # pragma: no cover - 主动取消
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底，避免后台任务静默失败
            self._fail(run, AppError(f"架构段异常：{exc}", code="architect_error"))

    # ── 执行段 ──

    def continue_run(self, run_id: str, instruction: str) -> Run:
        """多轮续聊：在已结束的运行上追加新要求，**不重写**已完成的步骤。

        典型用法就是"自开发"的对话迭代：跑完一轮 → 看结果 → 说"这里再改一下"。
        新步骤由架构段规划，仍然要过人工确认门，然后只执行新增的步骤。
        """

        text = (instruction or "").strip()
        if not text:
            raise AppError("请先写下要继续做什么。", code="invalid_request")
        run = self.store.load(run_id)
        if run.status in (RunStatus.PLANNING, RunStatus.EXECUTING):
            raise AppError(
                "运行还在进行中，等它跑完或先点「停止」再继续。",
                code="run_busy",
            )
        if not run.steps:
            raise AppError("这次运行还没有纲领，直接新开一个任务吧。", code="plan_missing")

        run.messages.append(RunMessage(role="user", phase="context", content=text))
        run.user_notes.append(text)
        run.error = None
        run.status = RunStatus.PLANNING
        # 续聊默认一路跑完新步骤，除非用户又在界面上指定了停靠点
        run.stop_after_step = None
        self.store.save(run)
        self.bus.publish(
            run_id,
            "status",
            status=run.status.value,
            message="根据你的补充规划新增步骤…",
        )
        self._cancelled.discard(run_id)
        self._tasks[run_id] = asyncio.create_task(self._plan_continuation(run_id, text))
        return self.store.load(run_id)

    async def _plan_continuation(self, run_id: str, instruction: str) -> None:
        run = self.store.load(run_id)
        settings = self._settings_provider()
        endpoint = settings.resolve_architect()
        try:
            self._require_endpoint(endpoint)
            client = self._client(endpoint, settings)
            stats = CallStats()
            context = self._build_context(run, settings)
            messages = build_continue_messages(
                run.task,
                max_steps=settings.max_plan_steps,
                done_log=self._completed_digest(run),
                instruction=instruction,
                context=context,
            )
            try:
                plan, raw = await run_architect(
                    client,
                    messages,
                    model=endpoint.model,
                    stats=stats,
                    on_token=lambda text: self.bus.publish(
                        run_id, "token", phase="architect", model=endpoint.model, text=text
                    ),
                )
            finally:
                self._record_metrics(
                    run,
                    stats,
                    endpoint,
                    phase=PHASE_ARCHITECT,
                    context_chars=message_chars(messages),
                )

            additions = plan.steps[: settings.max_plan_steps]
            offset = len(run.steps)
            for index, step in enumerate(additions, start=1):
                new_id = offset + index
                # 追加到纲领：plan.md 里能看到"原计划 + 本轮追加"的完整路线图
                if run.plan is not None:
                    run.plan.steps.append(
                        step.model_copy(
                            update={
                                "id": new_id,
                                "depends_on": [d + offset for d in step.depends_on],
                            }
                        )
                    )
                run.steps.append(
                    RunStep(
                        id=new_id,
                        title=step.title,
                        goal=step.goal,
                        deliverables=list(step.deliverables),
                        acceptance=list(step.acceptance),
                        checks=list(step.checks),
                    )
                )
            run.messages.append(
                RunMessage(role="assistant", phase="architect", model=endpoint.model, content=raw)
            )
            run.status = RunStatus.AWAITING_APPROVAL
            self.store.save(run)
            self._write_plan_doc(run)
            self.bus.publish(
                run_id,
                "plan",
                plan=run.plan.model_dump(mode="json") if run.plan else {},
                steps=[item.model_dump(mode="json") for item in run.steps],
                raw=raw,
                message=f"已追加 {len(additions)} 个新步骤，确认后执行。",
            )
            self.bus.publish(run_id, "status", status=run.status.value)
        except AppError as exc:
            self._fail(run, exc)
        except asyncio.CancelledError:  # pragma: no cover - 主动取消
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底，避免后台任务静默失败
            self._fail(run, AppError(f"续聊规划异常：{exc}", code="architect_error"))

    @staticmethod
    def _completed_digest(run: Run) -> str:
        """已完成步骤的摘要，交给架构段避免重复规划。"""

        lines: list[str] = []
        for step in run.steps:
            if step.status == StepStatus.DONE:
                summary = step.handoff or step.summary or "已完成"
                lines.append(
                    f"- 第 {step.id} 步「{step.title}」：{' '.join(summary.split())[:200]}"
                )
            elif step.status in (StepStatus.BLOCKED, StepStatus.FAILED):
                lines.append(
                    f"- 第 {step.id} 步「{step.title}」：{step.status.value}（{step.error[:120]}）"
                )
        return "\n".join(lines)

    async def _answer_chat(
        self,
        run: Run,
        client: RelayClient,
        endpoint: Endpoint,
        intent,
        stats: CallStats,
    ) -> None:
        """问答分支：直接回答，不建纲领、不建步骤、不碰工作区。"""

        self.bus.publish(
            run.id,
            "status",
            status=run.status.value,
            message=f"判断为问答（{intent.reason or '无需产出文件'}），直接回答，不进入编排。",
        )
        try:
            answer = await run_chat(
                client,
                run.task,
                model=endpoint.model,
                brief=run.brief,
                stats=stats,
                on_token=lambda text: self.bus.publish(
                    run.id, "token", phase="chat", model=endpoint.model, text=text
                ),
            )
        finally:
            self._record_metrics(
                run,
                stats,
                endpoint,
                phase=PHASE_ARCHITECT,
                context_chars=len(run.task) + len(run.brief),
            )

        run.kind = "chat"
        run.plan = None
        run.plan_raw = ""
        run.steps = []
        run.status = RunStatus.DONE
        run.error = None
        run.messages.append(
            RunMessage(role="assistant", phase="chat", model=endpoint.model, content=answer)
        )
        self.store.save(run)
        self.bus.publish(
            run.id,
            "done",
            status=run.status.value,
            summary={**self._run_summary(run), "kind": "chat", "reason": intent.reason},
        )

    def start_execution(self, run_id: str) -> None:
        self._cancelled.discard(run_id)
        self._tasks[run_id] = asyncio.create_task(self._execute(run_id))

    async def _execute(self, run_id: str) -> None:
        run = self.store.load(run_id)
        settings = self._settings_provider()
        endpoint = settings.resolve_editor()
        try:
            self._require_endpoint(endpoint)
            if not run.plan or not run.steps:
                raise AppError("还没有可执行的纲领。", code="plan_missing")

            run.status = RunStatus.EXECUTING
            run.error = None
            self.store.save(run)
            self.bus.publish(
                run_id,
                "status",
                status=run.status.value,
                message="执行段（DeepSeek V4）开始按纲领落地…",
            )

            workspace = Workspace(Path(run.workspace_dir), backup_dir=self.store.backup_dir(run.id))
            client = self._client(endpoint, settings)

            for step in run.steps:
                if run_id in self._cancelled:
                    run.status = RunStatus.CANCELLED
                    break
                if step.status == StepStatus.DONE:
                    continue
                await self._execute_step(run, step, workspace, client, endpoint, settings)
                if step.status in (StepStatus.FAILED, StepStatus.BLOCKED):
                    # 被阻塞 ≠ 失败：这是"缺信息"，用户可以补充后从这一步继续
                    run.status = (
                        RunStatus.BLOCKED if step.status == StepStatus.BLOCKED else RunStatus.FAILED
                    )
                    run.error = {
                        "code": "step_failed",
                        "message": f"第 {step.id} 步{('被阻塞' if step.status == StepStatus.BLOCKED else '执行失败')}：{step.error or step.summary}",
                    }
                    break
                if run.stop_after_step and step.id >= run.stop_after_step:
                    # 分批交付：本批跑完就停，状态是"已暂停"而不是"完成"
                    run.status = RunStatus.PAUSED
                    run.error = None
                    break
            else:
                run.status = RunStatus.DONE

            if run_id in self._cancelled and run.status == RunStatus.EXECUTING:
                run.status = RunStatus.CANCELLED

            self.store.save(run)
            self._write_report_doc(run)
            self.bus.publish(
                run_id,
                "done",
                status=run.status.value,
                summary=self._run_summary(run),
            )
        except AppError as exc:
            self._fail(run, exc)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail(run, AppError(f"执行段异常：{exc}", code="executor_error"))

    async def _execute_step(
        self,
        run: Run,
        step: RunStep,
        workspace: Workspace,
        client: RelayClient,
        endpoint: Endpoint,
        settings: Settings,
    ) -> None:
        step.status = StepStatus.RUNNING
        step.started_at = datetime.now(UTC)
        # 步骤级锚点：出错时可以"回滚这一步"（只还原这一步碰过的文件）
        step.git_snapshot = snapshot(workspace.root).as_dict()
        self.store.save(run)
        self.bus.publish(
            run.id,
            "step_start",
            step_id=step.id,
            title=step.title,
            goal=step.goal,
            acceptance=step.acceptance,
        )

        # 分层装配上下文：按预算裁剪，而不是把整份纲领和所有文件都塞给模型，
        # 让模型自己去压缩（那既贵又不可控）。
        builder = StepContextBuilder(
            budget_chars=settings.context_budget_chars,
            file_max_chars=settings.file_context_max_chars,
            files_max_chars=settings.files_context_max_chars,
            tree_max_chars=settings.tree_context_max_chars,
            log_max_chars=settings.completed_log_max_chars,
            brief_max_chars=settings.brief_max_chars,
            tree_limit=settings.context_tree_limit,
        )
        packet = builder.build(
            system=EXECUTOR_SYSTEM,
            task=run.task,
            plan=run.plan,
            step=step,
            steps=run.steps,
            tree=workspace.tree(limit=settings.context_tree_limit),
            read_file=workspace.read,
            user_notes=run.user_notes,
            brief_text=run.brief,
            # 文件树只在第一步给全量：后面每步都给全量，既贵又破坏前缀缓存
            tree_full=step.id == run.steps[0].id,
        )
        messages = build_step_messages(packet)

        # 本步的所有模型调用共用一个账本：索取文件的每一轮、JSON 强制重试、
        # 传输层重试都会累加到同一条 PhaseMetrics 上，而不是各记一条。
        stats = CallStats()
        output, raw = await self._call_executor(run, step, client, endpoint, messages, stats=stats)

        # 执行段发现上下文不足时，可以按需索取文件：补给它后再跑同一轮，
        # 这样它不必为了"看一眼"而把整个仓库读进上下文。
        fetched: list[str] = []
        rounds = 0
        while (
            output.need_files
            and not output.files
            and not output.blocked
            and rounds < settings.step_fetch_rounds
        ):
            requested = self._resolve_requested_files(
                workspace, output.need_files, limit=settings.step_file_fetch_limit
            )
            if not requested:
                break
            fetched.extend(requested)
            self.bus.publish(
                run.id, "fetch", step_id=step.id, files=requested, reason=output.need_reason
            )
            messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": self._requested_files_block(workspace, requested, settings)
                    + f"\n\n请继续完成第 {step.id} 步，仍然只输出那一个 JSON 对象。"
                    "若已足够，请直接给出 files/commands。",
                },
            ]
            output, raw = await self._call_executor(
                run, step, client, endpoint, messages, stats=stats
            )
            rounds += 1

        step.context_chars = packet.chars
        step.fetched_files = list(dict.fromkeys(fetched))
        fetch_rounds = rounds

        step.summary = output.summary
        step.handoff = output.handoff or output.summary
        step.notes = list(output.notes)
        step.commands = [
            {"cmd": item.cmd, "why": item.why} for item in output.commands if item.cmd.strip()
        ]

        self._apply_edits(run, step, workspace, output)

        # 受控命令执行 + 步骤内迭代（自开发的关键闭环）：
        # 模型写完文件并不知道对不对，跑一次白名单内的验证命令才知道；
        # 失败了就把报错回灌给它，让它在**同一步**里继续修，而不是丢给用户。
        raw, last_command_results, command_rounds_used = await self._run_step_commands(
            run, step, workspace, client, endpoint, settings, messages, raw, stats
        )
        # 指标在**所有轮次跑完之后**再记：这样 calls / 耗时 / 上下文构成
        # 覆盖到索取文件与按报错修正的每一轮，轮次也单独留档。
        entry = self._record_metrics(
            run,
            stats,
            endpoint,
            phase=PHASE_EXECUTOR,
            step_id=step.id,
            context_chars=packet.chars,
            context_stats=packet.stats,
            rounds={
                "initial": 1,
                "fetch": int(fetch_rounds),
                "repair": int(command_rounds_used),
            },
        )
        step.retries = int(entry.retries if entry is not None else 0)

        failures = [f for f in step.files if f.error]
        # 只看**最后一轮**的结果：第一轮失败、修好后第二轮通过，就应该算通过
        command_failures = [
            item for item in last_command_results if not item.ok and not item.skipped
        ]
        still_asking = bool(output.need_files) and not output.files
        no_output = not output.files and not (output.summary.strip() or output.notes)

        # 客观验收：纲领声明的检查 + 从交付物派生的「文件存在」检查。
        # 这是"模型说完成"与"确实完成"之间的唯一分界线。
        checks = effective_checks(step.checks, step.deliverables)
        results = run_checks(workspace, checks) if checks else []
        step.verification = results
        verdict = summarize(results)
        if results:
            self.bus.publish(
                run.id,
                "verify",
                step_id=step.id,
                results=[item.model_dump(mode="json") for item in results],
                summary=verdict,
            )

        if output.blocked:
            step.status = StepStatus.BLOCKED
            step.error = output.block_reason or "执行段报告被阻塞。"
        elif failures:
            step.status = StepStatus.FAILED
            step.error = "；".join(f"{f.path}: {f.error}" for f in failures)
        elif command_failures:
            # 验证命令没过：不许标完成，但这是"可以补信息/改命令再来一次"的情形
            step.status = StepStatus.BLOCKED
            step.error = f"验证命令未通过（已自动修正 {command_rounds_used} 轮）：" + "；".join(
                f"{item.cmd}（{item.error or f'退出码 {item.exit_code}'}）"
                for item in command_failures[:3]
            )
        elif still_asking:
            # 连续索取文件却始终不产出改动：不能算完成，标成"被阻塞"更诚实
            step.status = StepStatus.BLOCKED
            step.error = (
                f"执行段连续 {max(rounds, 1)} 轮只请求文件、未产出任何改动"
                f"（已补齐：{'、'.join(dict.fromkeys(fetched)) or '无'}）。"
                "可补充说明后继续，或直接告诉它「按现有内容修改，不要再索取文件」。"
            )
        elif no_output:
            step.status = StepStatus.BLOCKED
            step.error = "执行段没有返回任何文件改动或说明，无法确认这一步已完成。"
        elif verdict["failed"]:
            # 产出了东西、但没通过客观验收：不能算完成，但这是"补信息重跑一次"的情形
            step.status = StepStatus.BLOCKED
            step.error = f"客观验收未通过：{failure_text(results)}"
        else:
            step.status = StepStatus.DONE

        step.finished_at = datetime.now(UTC)
        run.messages.append(
            RunMessage(
                role="assistant",
                phase="executor",
                model=endpoint.model,
                content=raw,
            )
        )
        self.store.save(run)
        self.bus.publish(
            run.id,
            "step_done",
            step=step.model_dump(mode="json"),
            message=f"第 {step.id} 步{('已完成' if step.status == StepStatus.DONE else '未完成')}",
        )

    # ── 取消 ──

    def recover_interrupted(self) -> int:
        """启动时收敛"上次被中断"的运行。

        进程被杀/重启时，运行记录会停在 ``executing``、步骤停在 ``running``，
        界面看起来像还在跑但永远不会动。这里统一改成 ``paused`` 并把未完成的步骤
        退回 ``pending``，用户点"继续执行"即可接着跑。
        """
        recovered = 0
        for summary in self.store.list_runs():
            run = self.store.load(str(summary["id"]))
            if run.status not in (RunStatus.PLANNING, RunStatus.EXECUTING):
                continue
            for step in run.steps:
                if step.status == StepStatus.RUNNING:
                    step.status = StepStatus.PENDING
                    step.error = ""
                    step.summary = ""
                    step.files = []
                    step.commands = []
                    step.fetched_files = []
                    step.context_chars = 0
            run.status = RunStatus.PAUSED
            run.error = {
                "code": "interrupted",
                "message": "服务重启导致本次执行中断，已自动暂停；点「继续执行」会从当前步骤接着跑。",
            }
            self.store.save(run)
            self.bus.publish(
                run.id,
                "status",
                status=run.status.value,
                message=run.error["message"],
            )
            recovered += 1
        return recovered

    def resume(
        self,
        run_id: str,
        *,
        note: str = "",
        target_dir: str = "",
        stop_after_step: int | None = None,
    ) -> Run:
        """补充信息后从被阻塞/失败的那一步继续（不必从头重跑）。

        可以顺带指定落地目录——"任务提到现有代码但没给目录"是最常见的阻塞原因。
        """
        run = self.store.load(run_id)
        note = (note or "").strip()
        raw_dir = (target_dir or "").strip()
        if raw_dir:
            path = Path(raw_dir).expanduser().resolve()
            if not path.is_dir():
                raise WorkspaceError(
                    f"目标目录不存在或不是目录：{path}", details={"target_dir": str(path)}
                )
            run.target_dir = str(path)
            run.workspace_dir = str(path)
        if note:
            run.user_notes.append(note)
            run.messages.append(RunMessage(role="user", phase="context", content=note))

        for step in run.steps:
            if step.status in (StepStatus.BLOCKED, StepStatus.FAILED):
                step.status = StepStatus.PENDING
                step.error = ""
                step.summary = ""
                step.notes = []
                step.commands = []
                step.files = []
                step.fetched_files = []
        run.error = None
        run.status = RunStatus.EXECUTING
        # 续跑默认一路跑完；需要继续分批就显式再给一个停靠点
        run.stop_after_step = stop_after_step
        self.store.save(run)
        self.bus.publish(
            run_id,
            "status",
            status=run.status.value,
            message="已补充信息，从被阻塞的步骤继续。",
        )
        self.start_execution(run_id)
        return self.store.load(run_id)

    def update_run_meta(
        self,
        run_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> Run:
        """运行记录管理：重命名 / 置顶 / 归档（Codex 式任务列表）。"""
        run = self.store.load(run_id)
        if title is not None:
            cleaned = " ".join(title.split())[:120]
            if cleaned:
                run.title = cleaned
        if pinned is not None:
            run.pinned = bool(pinned)
        if archived is not None:
            run.archived = bool(archived)
        self.store.save(run)
        return run

    def retry_step(
        self,
        run_id: str,
        step_id: int,
        *,
        note: str = "",
        stop_after: bool = True,
    ) -> Run:
        """重做某一步（用于"这一步标了完成但其实没产出"或想换个说法重试）。

        默认只跑这一步就停下，避免顺手把后面的批次也带跑。
        """
        run = self.store.load(run_id)
        target = next((step for step in run.steps if step.id == step_id), None)
        if target is None:
            raise NotFoundError(f"未找到第 {step_id} 步", details={"step_id": step_id})

        note = (note or "").strip()
        if note:
            run.user_notes.append(note)
            run.messages.append(RunMessage(role="user", phase="context", content=note))

        target.status = StepStatus.PENDING
        target.error = ""
        target.summary = ""
        target.handoff = ""
        target.notes = []
        target.commands = []
        target.files = []
        target.fetched_files = []
        target.context_chars = 0
        run.error = None
        run.stop_after_step = step_id if stop_after else None
        run.status = RunStatus.EXECUTING
        self.store.save(run)
        self.bus.publish(
            run_id,
            "status",
            status=run.status.value,
            message=f"重新执行第 {step_id} 步…",
        )
        self.start_execution(run_id)
        return self.store.load(run_id)

    def revert_step(self, run_id: str, step_id: int) -> tuple[Run, dict[str, object]]:
        """回滚某一步的文件改动，并把该步退回 ``pending``。

        只还原**这一步碰过的路径**（来自 ``step.files``），不动用户其他未提交的改动；
        原本就存在的文件用 git 还原，这一步新建的文件删掉。
        非 git 仓库时仍会把步骤状态退回 pending，只是文件需要人工处理（backup/ 里还有原件）。
        """

        run = self.store.load(run_id)
        step = next((item for item in run.steps if item.id == step_id), None)
        if step is None:
            raise NotFoundError(f"未找到第 {step_id} 步", details={"step_id": step_id})
        if run.status in (RunStatus.PLANNING, RunStatus.EXECUTING):
            raise AppError(
                "运行正在执行中，等它跑完或先点「停止」再回滚。",
                code="run_busy",
            )

        workspace = Workspace(Path(run.workspace_dir))
        paths = [item.path for item in step.files if item.path and not item.error]
        summary: dict[str, object] = {
            "restored": [],
            "removed": [],
            "skipped": [],
            "errors": [],
        }
        if paths:
            summary = revert_paths(workspace.root, paths).as_dict()

        step.status = StepStatus.PENDING
        step.error = ""
        step.summary = ""
        step.handoff = ""
        step.notes = []
        step.commands = []
        step.command_results = []
        step.files = []
        step.verification = []
        step.fetched_files = []
        step.context_chars = 0
        step.retries = 0
        step.started_at = None
        step.finished_at = None
        run.status = RunStatus.PAUSED
        run.error = None
        self.store.save(run)
        self._write_report_doc(run)
        self.bus.publish(
            run.id,
            "status",
            status=run.status.value,
            message=(
                f"已回滚第 {step_id} 步：还原 {len(summary['restored'])} 个、"
                f"删除 {len(summary['removed'])} 个文件。"
            ),
        )
        return self.store.load(run_id), summary

    def cancel(self, run_id: str) -> Run:
        self._cancelled.add(run_id)
        run = self.store.load(run_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        if run.status in (
            RunStatus.PLANNING,
            RunStatus.EXECUTING,
            RunStatus.AWAITING_APPROVAL,
        ):
            run.status = RunStatus.CANCELLED
            # 正在执行的步骤退回 pending：否则会出现"运行已取消、步骤仍显示执行中"的悬挂状态
            for step in run.steps:
                if step.status == StepStatus.RUNNING:
                    step.status = StepStatus.PENDING
                    step.error = ""
                    step.summary = ""
            self.store.save(run)
        self.bus.publish(run_id, "status", status=run.status.value, message="已取消。")
        return run

    # ── 辅助 ──

    def describe_route(self, settings: Settings | None = None) -> dict[str, object]:
        settings = settings or self._settings_provider()
        return {
            "architect": settings.resolve_architect().describe(),
            "editor": settings.resolve_editor().describe(),
        }

    def _client(self, endpoint: Endpoint, settings: Settings) -> RelayClient:
        return RelayClient(
            endpoint.base_url,
            endpoint.api_key,
            wire_api=endpoint.wire_api,
            timeout=settings.request_timeout_seconds,
            transport=self._transport,
            model_hint=endpoint.model,
        )

    def _require_endpoint(self, endpoint: Endpoint) -> None:
        if not endpoint.configured:
            raise ConfigurationError(
                f"{endpoint.label}未配置完整（需要 base_url / api_key / model）。",
                details={
                    "role": endpoint.role,
                    "model": endpoint.model,
                    "base_url": endpoint.base_url,
                },
            )

    def _build_context(self, run: Run, settings: Settings) -> str:
        lines: list[str] = []
        if run.brief.strip():
            lines.append("## 前期沟通简报（本次运行的背景，务必据此判断）")
            lines.append(clip(run.brief, settings.brief_max_chars))
            lines.append("")
        workspace = Workspace(Path(run.workspace_dir))
        tree = workspace.tree(limit=settings.context_tree_limit)
        if run.target_dir:
            lines.append(f"本次运行会直接改动这个目录：{run.target_dir}")
        else:
            lines.append("本次运行使用**全新空工作区**（用户未指定落地目录，也没有提供现有代码）。")
        if tree:
            lines.append("工作区中已存在的文件（部分）：")
            lines.extend(f"- {item}" for item in tree)
        else:
            lines.append("工作区当前没有任何文件。")
            lines.append(
                "因此：如果需求提到「现有 / 已有 / 当前」的实现，请不要把步骤设计成"
                "「盘点现有代码」——执行段看不到那些代码。应当二选一："
                "把需要澄清的入口/仓库位置写进 open_questions，或把步骤改成「从零新建」。"
            )
        for note in run.user_notes:
            lines.append(f"用户补充说明：{note}")
        for message in run.messages:
            if message.phase == "context":
                lines.append(f"用户补充说明：{message.content}")
        return "\n".join(lines)

    async def _call_executor(
        self,
        run: Run,
        step: RunStep,
        client: RelayClient,
        endpoint: Endpoint,
        messages: list[dict[str, str]],
        *,
        stats: CallStats | None = None,
    ) -> tuple[StepOutput, str]:
        return await run_step(
            client,
            messages,
            model=endpoint.model,
            stats=stats,
            on_token=lambda text: self.bus.publish(
                run.id,
                "token",
                phase="executor",
                step_id=step.id,
                model=endpoint.model,
                text=text,
            ),
        )

    def _record_metrics(
        self,
        run: Run,
        stats: CallStats,
        endpoint: Endpoint,
        *,
        phase: str,
        step_id: int | None = None,
        context_chars: int | None = None,
        context_stats: dict[str, int] | None = None,
        rounds: dict[str, int] | None = None,
    ):
        """把一次调用的账本写进 ``run.metrics``（同一阶段/步骤累加，不重复建条目）。"""

        try:
            entry = run.metrics_for(phase, step_id)
            apply_call(entry, stats, context_chars=context_chars, route=route_of(endpoint))
            if context_stats:
                # 上下文构成：让"这一步的钱花在哪"可查（不是只给一个总字符数）
                entry.context_stats = {
                    key: int(value) for key, value in context_stats.items() if key != "budget"
                }
            if rounds:
                entry.rounds = {key: int(value) for key, value in rounds.items()}
        except Exception:  # noqa: BLE001 - 指标绝不能让主流程失败
            return None
        self.bus.publish(
            run.id,
            "metrics_updated",
            metrics=entry.model_dump(mode="json"),
            summary=run.metrics_summary(),
        )
        return entry

    def _apply_edits(
        self, run: Run, step: RunStep, workspace: Workspace, output: StepOutput
    ) -> None:
        """把一轮输出里的文件改动落到工作区，并推送 file 事件。"""

        for edit in output.files:
            change = workspace.apply_edit(edit)
            changed = ChangedFile(
                path=change.path,
                action=change.action,
                additions=change.additions,
                deletions=change.deletions,
                diff=change.diff,
                size=change.size,
                error=change.error,
            )
            step.files.append(changed)
            self.bus.publish(
                run.id,
                "file",
                step_id=step.id,
                file=changed.model_dump(mode="json"),
            )

    async def _run_step_commands(
        self,
        run: Run,
        step: RunStep,
        workspace: Workspace,
        client: RelayClient,
        endpoint: Endpoint,
        settings: Settings,
        messages: list[dict[str, str]],
        raw: str,
        stats: CallStats,
    ) -> tuple[str, list[CommandRun], int]:
        """跑白名单内的验证命令；失败就把报错回灌给执行段，让它在同一步里继续修。

        返回 ``(最新原始输出, 最后一轮的命令结果, 实际用掉的修正轮数)``。
        只把**最后一轮**的结果交给状态判断——早先失败但已修好的，不该继续算失败。
        """

        rounds = 0
        last_results: list[CommandRun] = []
        while True:
            requested = [str(item.get("cmd", "")) for item in step.commands if item.get("cmd")]
            if not requested:
                return raw, last_results, rounds

            results = run_allowed(
                requested,
                cwd=workspace.root,
                allowlist=settings.command_allowlist,
                enabled=settings.allow_command_execution,
                timeout=settings.command_timeout_seconds,
            )
            last_results = [CommandRun(**item.as_dict()) for item in results]
            for item in results:
                step.command_results.append(CommandRun(**item.as_dict()))
                self.bus.publish(
                    run.id,
                    "command",
                    step_id=step.id,
                    result=item.as_dict(),
                )
            self.store.save(run)

            failed = [item for item in results if not item.ok and not item.skipped]
            if not failed or rounds >= max(0, settings.step_command_rounds):
                return raw, last_results, rounds

            rounds += 1
            self.bus.publish(
                run.id,
                "status",
                status=run.status.value,
                message=f"第 {step.id} 步：验证命令未通过，正在按报错自动修正（第 {rounds} 轮）…",
            )
            retry_messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"{failure_block(failed)}"
                        f"\n\n请针对上面的报错继续修正第 {step.id} 步，"
                        "只输出那一个 JSON 对象：需要改的文件放 files，"
                        "改完想重新验证的命令放 commands。"
                        "如果确认报错与你的改动无关（例如环境问题），"
                        "请在 notes 里说明并把 blocked 设为 false。"
                    ),
                },
            ]
            output, raw = await self._call_executor(
                run, step, client, endpoint, retry_messages, stats=stats
            )
            step.commands = [
                {"cmd": item.cmd, "why": item.why} for item in output.commands if item.cmd.strip()
            ]
            if output.summary.strip():
                step.summary = output.summary
            if output.handoff.strip():
                step.handoff = output.handoff
            if output.notes:
                step.notes = list(output.notes)
            self._apply_edits(run, step, workspace, output)
            self.store.save(run)

    @staticmethod
    def _resolve_requested_files(
        workspace: Workspace, patterns: list[str], *, limit: int
    ) -> list[str]:
        """解析执行段索要的文件：支持精确路径与 ``*`` 通配，全部限定在工作区内。"""
        tree = workspace.tree(limit=400, max_depth=8)
        matched: list[str] = []
        for raw in patterns:
            pattern = str(raw).strip().replace("\\", "/").lstrip("./")
            if not pattern:
                continue
            if any(ch in pattern for ch in "*?["):
                for path in tree:
                    if fnmatch(path, pattern) or fnmatch(path.rsplit("/", 1)[-1], pattern):
                        matched.append(path)
            elif pattern in tree or workspace.exists(pattern):
                matched.append(pattern)
        return list(dict.fromkeys(matched))[:limit]

    @staticmethod
    def _requested_files_block(workspace: Workspace, paths: list[str], settings: Settings) -> str:
        chunks: list[str] = []
        for path in paths:
            try:
                content = workspace.read(path)
            except AppError:
                continue
            piece = clip_head_tail(content, settings.file_context_max_chars, path=path)
            chunks.append(f"### {path}\n```\n{piece}\n```")
        return "## 你索要的文件\n" + ("\n\n".join(chunks) or "（这些文件当前不存在）")

    def _write_plan_doc(self, run: Run) -> None:
        if not run.plan:
            return
        plan = run.plan
        lines = [
            f"# 纲领：{run.title}",
            "",
            f"> 运行 ID：`{run.id}`　架构段模型：`{run.route.get('architect', {}).get('model', '')}`",
            "",
            f"## 目标\n{plan.goal}",
            "",
            f"## 纲领性说明\n{plan.summary}",
            "",
        ]
        if plan.principles:
            lines.append("## 设计原则")
            lines.extend(f"- {item}" for item in plan.principles)
            lines.append("")
        if plan.components:
            lines.append("## 组件与职责")
            for component in plan.components:
                interfaces = "、".join(component.interfaces) or "—"
                lines.append(
                    f"- **{component.name}**：{component.responsibility}（接口：{interfaces}）"
                )
            lines.append("")
        lines.append("## 执行步骤")
        for step in plan.steps:
            lines.append(f"### {step.id}. {step.title}")
            lines.append(f"- 目标：{step.goal}")
            if step.deliverables:
                lines.append(f"- 交付物：{'、'.join(step.deliverables)}")
            if step.acceptance:
                lines.append("- 验收标准：")
                lines.extend(f"  - {item}" for item in step.acceptance)
            lines.append("")
        if plan.risks:
            lines.append("## 风险")
            lines.extend(f"- {item}" for item in plan.risks)
            lines.append("")
        if plan.open_questions:
            lines.append("## 待澄清")
            lines.extend(f"- {item}" for item in plan.open_questions)
            lines.append("")
        self._write_run_doc(run, "plan.md", "\n".join(lines))

    def _write_report_doc(self, run: Run) -> None:
        lines = [
            f"# 执行报告：{run.title}",
            "",
            f"> 运行 ID：`{run.id}`　状态：`{run.status.value}`",
            "",
            "## 步骤结果",
        ]
        for step in run.steps:
            lines.append(f"### {step.id}. {step.title} — {step.status.value}")
            if step.summary:
                lines.append(step.summary)
            for change in step.files:
                marker = f" (+{change.additions}/-{change.deletions})" if change.diff else ""
                lines.append(
                    f"- `{change.path}`{marker}{' — ' + change.error if change.error else ''}"
                )
            if step.commands:
                lines.append("- 建议命令：")
                lines.extend(f"  - `{item.get('cmd', '')}`" for item in step.commands)
            if step.verification:
                verdict = summarize(step.verification)
                lines.append(
                    f"- 客观验收：{verdict['passed']}/{verdict['total']} 通过"
                    f"{'（未通过）' if verdict['failed'] else ''}"
                )
                lines.extend(
                    f"  - {'✓' if item.ok else '✗'} {item.label or item.path}"
                    f"{'：' + item.detail if item.detail else ''}"
                    for item in step.verification
                )
            elif step.status == StepStatus.DONE:
                lines.append("- 客观验收：本步没有可自动判定的检查项（未验证）")
            lines.append("")
        if run.error:
            lines.append(f"## 失败原因\n{run.error.get('message', '')}")
        self._write_run_doc(run, "report.md", "\n".join(lines))

    def _write_run_doc(self, run: Run, name: str, content: str) -> None:
        try:
            directory = self.store.run_dir(run.id)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(content, encoding="utf-8")
            self.bus.publish(run.id, "artifact", name=name, content=content)
        except OSError:
            pass

    def _run_summary(self, run: Run) -> dict[str, object]:
        return {
            "steps_total": len(run.steps),
            "steps_done": sum(1 for s in run.steps if s.status == StepStatus.DONE),
            "files_changed": sum(len(s.files) for s in run.steps),
            "status": run.status.value,
        }

    def _fail(self, run: Run, exc: AppError) -> None:
        run.status = RunStatus.FAILED
        run.error = exc.as_dict()
        self.store.save(run)
        self.bus.publish(run.id, "error", error=exc.as_dict(), status=run.status.value)
        self.bus.publish(run.id, "status", status=run.status.value)
