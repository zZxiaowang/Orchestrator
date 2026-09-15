# 导航迁移说明（第 5 步交付物）

上位文档：`docs/project-context-contract.md`、`docs/project-navigation-contract.md`。
实现：`app/services/navigation_migration.py`；测试：`tests/test_navigation_migration.py`、`tests/test_project_workspace.py`。

## 1. 迁移结论

一级导航只保留两个任务入口：`普通对话`（chat）与`项目`（projects）。
架构、计划、执行、步骤、验证、日志、设置全部下沉为项目内模块，不再作为全局入口存在。

旧的全局工程链接由 `resolve_legacy_route(route, project_id=..., context_id=...)` 统一翻译，
翻译结果只有四种：

| kind | 含义 | HTTP |
| --- | --- | --- |
| `keep` | 保留入口，原样生效 | 200 |
| `redirect` | 可定位到项目，重定向进项目内模块 | 302 |
| `needs_project` | 无法定位项目，返回「需要选择项目」引导 | 302 |
| `removed` | 跨项目聚合入口，不再支持 | 410 |

## 2. 已迁移入口（redirect）

旧路由到项目内模块的映射表（`LEGACY_PROJECT_MODULE_ROUTES`）：

| 旧路由 | 项目内模块 |
| --- | --- |
| `#/overview` | `overview` |
| `#/architecture`、`#/arch`、`#/design` | `architecture` |
| `#/plan`、`#/plans` | `plan` |
| `#/execution`、`#/execute`、`#/run`、`#/runs` | `execution` |
| `#/verification`、`#/verify` | `verification` |
| `#/logs` | `logs` |
| `#/settings` | `settings` |

新落点由 `project_module_context(module, project_id)` 生成，同时带上
`project_id`、`context_id = project:<projectId>` 与项目内路由，页面无需再自行拼装。

## 3. 需要选择项目的入口（needs_project）

以下两种情况不重定向进项目，而是返回引导：

1. 已迁移路由但拿不到项目定位信息（既无 `project_id`，也无 `project:<projectId>` 形式的 `context_id`）；
2. 当前是普通对话上下文（`chat:<sessionId>`）却试图打开项目内模块。

行为约定：

* `target_route = #/projects`，`status_code = 302`，`requires_project_selection = True`；
* `project_id` 保持 `None`，**绝不静默回落到 `default` 项目**；
* 普通对话分支同样落到 `needs_project`，保证普通对话不出现项目执行框架。

## 4. 保留入口（keep）

`#/chat`、`#/chat/<sessionId>`、`#/projects`、`#/projects/<projectId>/...`。

空路由、`#` 与 `#/` 视为保留入口，落到 `DEFAULT_ROUTE`。未注册的路由原样透传（`keep`），
避免迁移层误伤后续新增页面。

## 5. 不再支持的入口（removed，410）

`#/architecture/all`、`#/plans/all`、`#/execution/all`、`#/runs/all`、`#/verification/all`、`#/logs/all`。

这些是旧的多项目聚合视图，与新信息架构的项目边界隔离直接冲突，因此明确返回 410 并引导到 `#/projects`，
而不是静默挑选某个项目渲染。

## 6. 历史数据映射规则

历史计划 / 运行 / 步骤 / 验证结果按以下顺序归属：

1. 记录自带 `project_id` → 直接使用该值；
2. 只有 `context_id` 且形如 `project:<projectId>` → 解析出项目；
3. 只有 `chat:<sessionId>` → 属于普通对话，不归入任何项目；
4. 两类信息都拿不到 → 视为未归属，走「需要选择项目」引导，**不写入也不显示在 default 项目下**；
5. 归属校验在同一项目边界内完成，跨项目读取 / 控制一律拒绝（`ProjectContextError`）。

## 7. 页面状态覆盖

`derive_project_view_state(project=..., run=..., load_error=..., restarting=...)` 统一推导展示态：

| 状态 | 触发条件 |
| --- | --- |
| `empty_project` | 项目存在但没有任何运行 / 计划 |
| `load_failed` | 项目加载失败（传入 `load_error`） |
| `running` | 运行状态为 `executing` / `running` / `pending` / `queued` / `in_progress` / `planning` |
| `run_failed` | 运行状态为 `failed` / `error` / `errored` |
| `run_cancelled` | 运行状态为 `cancelled` / `canceled` / `aborted` |
| `restarting` | 运行状态为 `restarting` / `resuming`，或显式 `restarting=True` |
| `completed` | 运行状态为 `completed` / `succeeded` / `done` / `finished` |

`load_error` 优先级最高，其次 `restarting`。

## 8. 兼容期与回滚

迁移层是纯翻译层，不写数据库、不改运行状态。回滚只需停止调用 `resolve_legacy_route`：
旧链接会按「未知路由透传」处理，不影响既有编排、执行、验证与重启逻辑。

## 9. 验收映射

| 验收点 | 覆盖位置 |
| --- | --- |
| 旧架构 / 执行入口重定向进项目模块 | `test_legacy_project_entry_redirects_into_project_module` |
| 无项目时返回选择项目引导 | `test_legacy_project_entry_without_project_asks_for_selection` |
| 无法映射的数据不静默归入错误项目 | `test_unmappable_legacy_link_never_falls_back_to_default_project` |
| 普通对话隔离 | `test_chat_context_cannot_open_project_module`、`test_chat_context_cannot_read_project_run` |
| 项目上下文恢复 | `test_legacy_project_entry_accepts_scoped_context_id`、`test_project_context_is_restored_from_scoped_context` |
| 跨项目拒绝 | `test_cross_project_read_is_rejected`、`tests/test_project_workspace.py::test_cross_project_records_are_rejected` |
| 空 / 失败 / 运行中 / 取消 / 重启中状态 | `test_project_view_state_covers_every_required_state` 等 |
| 已迁移 / 保留 / 不再支持入口记录 | 本文档第 2、4、5 节 |
