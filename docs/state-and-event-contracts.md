# 编排器 · 状态与事件契约（冻结版）

本文件是 `app/schemas/run.py`（`RunStatus` / `StepStatus`）与 `app/services/events.py`（`EventBus`）
的对外契约。后续步骤只能**新增**状态、事件与字段，不得改变这里已冻结的含义。

## 1. 运行状态（RunStatus）

已实现取值：`planning`、`awaiting_approval`、`executing`、`blocked`、`done`、`failed`、`cancelled`。
本阶段预留：`queued`（P2，步骤 11，等待执行资源）。

| 状态 | 语义 | 允许转入 | 触发者 |
| --- | --- | --- | --- |
| `planning` | 架构段生成 / 修订纲领中 | `awaiting_approval`、`failed`、`cancelled` | 架构段完成 / 出错 / 用户取消 |
| `awaiting_approval` | 纲领待人工确认 | `executing`、`planning`、`failed`、`cancelled` | 用户确认 / 用户要求重做纲领 / 出错 / 取消 |
| `executing` | 执行段按步骤落地 | `done`、`blocked`、`failed`、`cancelled` | 全部步骤完成 / 某步阻塞 / 出错 / 取消 |
| `blocked` | 执行段明确声明缺信息，暂停在阻塞步骤 | `executing`、`failed`、`cancelled` | 用户补充信息并继续 / 出错 / 取消 |
| `done` | 终态：全部步骤成功或跳过 | —（续聊见第 7 节） | — |
| `failed` | 终态：出错并保留上下文 | `executing`（显式重试） | 用户重试 |
| `cancelled` | 终态：用户主动取消 | — | — |
| `queued`（预留） | P2：已创建但未获得执行资源 | `planning`、`cancelled` | 调度器放行 / 取消 |

禁止的转换（必须由测试守住）：
- `planning` → `executing`：必须先经过 `awaiting_approval`。
- `done` → 任何状态（P1 续聊引入前）。
- `cancelled` → 任何状态。
- 任何状态 → `queued`（P2 中仅允许从 `planning` 之前/创建时进入）。

## 2. 步骤状态（StepStatus）

主链路：`pending` → `running` → `done` / `failed` / `blocked`；`skipped` 为终态。

| 当前 | 允许转入 |
| --- | --- |
| `pending` | `running`、`skipped` |
| `running` | `done`、`failed`、`blocked` |
| `blocked` | `running`（补充信息后只重跑该步） |
| `failed` | `running`（显式重试） |
| `done` / `skipped` | 不允许回退 |

## 3. 运行级不变量

1. `status == done` ⟹ 所有步骤为 `done` 或 `skipped`。
2. `status == blocked` ⟹ 至少一个步骤为 `blocked`，且它之后不再有非终态步骤。
3. `status == executing` ⟹ 存在 `running` 步骤，或仍有 `pending` 步骤。
4. `status == failed` ⟹ `run.error` 非空。
5. 单次运行内步骤 `id` 唯一且单调递增。

## 4. blocked 恢复语义

- 恢复时只重跑**被阻塞的那一步及其之后**；已 `done` 的步骤不得重新执行、不产生新的文件改动。
- 用户补充的说明追加到 `run.user_notes`，并以消息形式进入执行段上下文（保持既有行为）。
- 恢复不得重置 `run.plan` 与 `plan_revision`，不得重新生成事件序号（`seq` 继续递增）。

## 5. 事件契约（EventBus）

所有事件必须满足同一信封：

```json
{"type": "status", "run_id": "r-xxx", "seq": 12, "ts": "2026-09-14T00:48:29Z", "data": {}}
```

- `seq`：**每个运行内从 1 开始单调递增**，是断线续传的唯一依据。
  **禁止用 `ts` 过滤**——Windows 时钟精度约 15ms，同批事件时间戳可能相同，会丢事件。
- `ts`：UTC ISO8601；精度不保证，仅用于展示，不参与任何过滤逻辑。
- `data`：随 `type` 变化的对象。既有事件类型的既有字段不得删除或改类型，只允许追加字段；消费端必须容忍字段缺失。
- 历史：每个运行最多保留 `HISTORY_LIMIT = 1500` 条，超出丢弃最旧。
- 心跳：无事件时每 `HEARTBEAT_SECONDS = 15.0` 秒发一次 `ping`；`ping` 不写历史、不占 `seq`，仅当前订阅者可见。
- 已实现事件类型：`status`、`error`、`artifact`、`ping`。
  后续新增（指标、命令生命周期、队列）必须沿用同一信封，并使用新的 `type` 名，不复用旧名。

订阅起点：客户端用已收到的最大 `seq` 作为 `since` 做增量订阅；服务端的当前序号可由 `EventBus.current_seq(run_id)` 得到。

## 5.1 运行指标字段（第 2 步新增，向后兼容）

指标落在 `run.json` 的 `metrics` 数组里，每条对应「一个阶段 + 一个步骤」：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `phase` | `"executor"` | `architect` / `executor`，区分架构段与执行段 |
| `step_id` | `null` | 执行段为该步骤 `id`；架构段固定 `null` |
| `calls` | `0` | 该阶段/步骤发起的模型调用次数（含重试） |
| `retries` | `0` | 额外重试次数（首次尝试不计） |
| `context_chars` | `0` | 实际发送的上下文字符数 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | `null` | **`null` 表示未知**（提供方未返回 usage），不得写 0 冒充；0 仅表示提供方明确返回 0 |
| `usage_source` | `"unknown"` | `provider` / `estimated` / `unknown` |
| `usage_reason` | `""` | 未知或估算的原因（如 `provider_no_usage`） |
| `duration_ms` | `0` | 累计耗时（毫秒） |
| `route` | `{}` | 仅白名单字段 `alias` / `model` / `protocol` / `base_url`；base_url 已脱敏 |

兼容性：旧 `run.json` 没有上述字段时按默认值补齐，行为与旧版本一致。
`RunStep` 另新增 `retries: int = 0`（本步执行段的重试次数）。
`Run.metrics_summary()` 提供运行级汇总，token 全部未知时返回 `null` 而非 0，
并在 `unknown_usage` 里列出缺 usage 的阶段/步骤。

## 6. 敏感字段与审计约束

**不得**出现在事件 `data`、日志、`run.json`、错误信封中的内容：
- 任何 API Key / token / `Authorization` 值（含用户误填到地址栏的值）；
- 完整请求头与完整请求体；
- 网关鉴权错误里可能回显的凭据片段。

**必须**做到：
- 错误信息写入 `run.error` 之前完成类型归一：`bytes` 先解码为 `str`，避免 `expected str instance, bytes found` 掩盖真实网关错误。
- 路由信息（`run.route`）只记录模型名、协议、脱敏后的 base_url 与配置别名，不得含 Key 明文。
- 命令执行的审计信息只记录：命令文本、工作区相对路径、退出码、截断后的输出摘要。
- 新增任何持久化字段时，必须自查该字段是否可能携带凭据；可能则先脱敏再落盘。

## 7. 演进规则

1. 新增 `RunStatus` / `StepStatus` 成员属兼容变更，可以加；改变已有成员含义属破坏性变更，禁止。
2. 新增运行字段必须有默认值，且旧 `run.json` 读入后行为与旧版本一致。
3. 续聊（P1 步骤 10）引入的 `done → executing` 回迁，必须作为**显式例外**记入本文件，并保证原步骤记录与事件序号不被重写。
4. 队列（P2 步骤 11）引入 `queued` 时，必须先保证不含 `queued` 的旧记录仍能正常加载。
