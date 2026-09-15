# 项目导航契约：路由、数据上下文、空状态与兼容策略

> 状态：已定稿（第 1 步：固化信息架构与路由契约）
> 上位契约：`docs/project-and-chat-information-architecture.md`
> 信息架构：`docs/sidebar-project-information-architecture.md`
> 适用范围：`web/app.js` 前端路由与左侧栏；后续 `app/api/routes.py`、`app/services/*` 的上下文校验

## 1. 路由层级与逐路由上下文

采用前端 hash 路由，两级结构：一级入口 → 项目二级模块。一级导航只有两个：

| 路由 | 一级入口 | 说明 |
| --- | --- | --- |
| `#/chat` | 普通对话 | 会话列表 / 最近会话 |
| `#/projects` | 项目 | 项目列表 |

项目二级路由 `#/projects/:projectId/...` 仅在选中项目后可访问；`:projectId` 为项目 id（不含 `project:` 前缀，渲染时统一拼为 `project:<projectId>`）。

| 路由 | 模块 | 数据上下文 | 空状态 | 主行动 |
| --- | --- | --- | --- | --- |
| `#/chat` | 普通对话（会话列表） | `context_type=chat` | 「还没有对话」+「普通对话不绑定项目，可直接开始」 | 「新建对话」→ `#/chat/:chatId` |
| `#/chat/:chatId` | 普通对话（消息区） | `context_type=chat`，`context_id=chat:<chatId>` | 「开始这段对话」+「消息只属于本会话，不产生运行与步骤」 | 输入框聚焦 |
| `#/projects` | 项目列表 | `context_type=project` | 「还没有项目」+「架构、执行、计划、验证都在项目内进行」 | 「新建项目」→ `#/projects/:projectId` |
| `#/projects/:projectId` | 概览 overview | `context_id=project:<projectId>` | 「项目还没有运行」+「先看架构或直接执行」 | 「去架构」/「开始执行」 |
| `#/projects/:projectId/architecture` | 架构 | 同上 | 「尚未生成架构」+「生成后会作为执行依据」 | 「生成架构」 |
| `#/projects/:projectId/plan` | 计划 | 同上 | 「尚无计划」+「计划用于拆分阶段」 | 「生成计划」 |
| `#/projects/:projectId/execution` | 执行 | 同上 + 运行列表查询携带 `context_id` | 「还没有运行」+「运行会在此显示进度与事件」 | 「新建运行」 |
| `#/projects/:projectId/execution/:runId` | 执行（运行详情） | 同上 + `runId` | 「运行不存在或已被清理」 | 「返回执行列表」 |
| `#/projects/:projectId/steps` | 步骤 | 同上 + 可选 `?runId=` | 「没有步骤」+「步骤依赖一次运行」 | 「去执行」 |
| `#/projects/:projectId/verify` | 验证 | 同上 + 可选 `?runId=` | 「暂无验证结果」 | 「运行验证」 |
| `#/projects/:projectId/logs` | 日志 | 同上 + 项目事件流 | 「暂无日志」 | 「刷新」 |
| `#/projects/:projectId/settings` | 设置 | 同上 | 「项目尚未配置」 | 「保存设置」 |

覆盖范围：普通对话、项目列表、项目概览，以及架构、计划、执行、步骤、验证、日志、设置七个项目二级模块（≥4 个）。

## 2. 数据上下文契约

- 每个路由必须能用 `(context_type, context_id)` 唯一确定查询范围；不得先取全局列表再在前端过滤。
- 普通对话路由：`context_type=chat`，只允许消息相关查询，禁止派发 `PROJECT_ONLY_ACTIONS`。
- 项目路由：`context_type=project`，`context_id=project:<projectId>`；运行、计划、步骤、验证、事件、日志、设置的查询都必须携带该上下文。
- API 侧：`/api/v1` 前缀下的运行 / 计划 / 步骤 / 验证等资源族在接收请求时校验 `context_type`；精确路径以 `app/api/routes.py` 为准，本契约只约束上下文参数与归属，不新增路径。
- 前端路由解析：`parseRoute(hash)` → `{ level, contextType, contextId, moduleId, params }`；`moduleId ∈ {overview, architecture, plan, execution, steps, verify, logs, settings}`。
- 缺省与兜底：缺 `projectId` → 跳 `#/projects`；缺 `chatId` → 跳 `#/chat`；非法 `moduleId` → 回落 `overview`。

## 3. 守卫规则

| 场景 | 处理 |
| --- | --- |
| 普通对话上下文请求项目模块 | 重定向到 `#/chat/:chatId`（无会话则 `#/chat`），提示「该功能属于项目」 |
| 项目上下文使用消息能力 | 允许在项目对话区，但不得伪造 `chat` 上下文 |
| `projectId` 不存在 | 渲染项目空状态并引导回 `#/projects` |
| 无 `context_type` 的旧数据 | 按上位契约第 6 节，一律归属 `project` |
| 打开运行详情但无法确定所属项目 | 归属 `project:default` |

## 4. 空状态与错误状态

- 三层结构：标题 / 解释 / 单一主行动（见信息架构文档第 7 节）。
- 错误状态与空状态分开：空状态用中性文案，加载失败用错误态并提供重试。
- 每个二级模块实现四态：加载中、空、错误、有数据；四态下二级导航都保持可见。

## 5. 旧入口 → 新路由 兼容与重定向

| 旧入口 / 旧路由 | 旧行为 | 新行为 | 策略 |
| --- | --- | --- | --- |
| 左侧栏「运行」列表 | 全局运行列表 | `#/projects` → 项目 → 执行 | 入口下线，数据保留，不做数据迁移 |
| 根路由 `#/` 或空 hash | 全局运行页 | `#/projects` | 前端 replace 重定向 |
| 统计看板 | 全局统计 | `#/projects/:projectId/execution` 内的统计区块 | 入口下线 |
| `#/runs/:runId` | 运行详情 | `#/projects/:projectId/execution/:runId` | 能解析出项目则 replace；不能则跳 `#/projects` 并提示 |
| `#/settings` | 全局设置 | `#/projects/project:default/settings` | replace，并提示「设置已归属项目」 |
| `#/plugins` | 全局插件 | `#/projects/:projectId/settings` 的扩展分区 | 入口下线，保留已安装数据 |
| 无 `context_type` 的旧运行数据 | 直接展示 | 归属 `project`，缺 `context_id` 时用 `project:default` | 不迁移、不改写历史数据 |
| `localStorage` 的 `orchestrator.lastRun` | 直接打开该运行 | 解析出项目上下文后重定向到运行详情 | 解析失败按 `project:default`；**不清空**该记录 |
| 直接访问项目模块但缺 `projectId` | — | `#/projects` | replace + 提示 |

兼容规则：

1. 重定向统一在前端路由解析层完成（hash 替换），不使用服务端 301。
2. 重定向必须幂等：同一旧地址多次访问结果一致。
3. 兼容映射不得把普通对话提升为项目；反向（项目被当作普通对话）同样禁止。
4. 旧入口对应的 API 行为保持不变，待第 5 步验收覆盖完成后再评估下线。

## 6. 验收样例（第 5 步回归用）

- 打开应用默认落在 `#/projects`，一级导航只有「普通对话」「项目」。
- `#/chat` 与 `#/chat/:chatId` 下不出现架构 / 执行 / 步骤 / 验证 / 日志 / 设置入口。
- 架构、执行、步骤、验证四个以上项目二级模块各自可达且各有独立空状态。
- `#/runs/:runId`、`#/settings`、`#/plugins` 均按第 5 节重定向。
- 缺 `context_type` 的历史运行仍能在项目执行模块打开。
