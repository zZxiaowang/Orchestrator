# 项目上下文数据契约（第 2 步）

> 状态：已落地（第 2 步：建立项目上下文数据契约）
> 上位契约：`docs/project-navigation-contract.md`（第 1 步）
> 实现：`app/schemas/project.py`、`app/schemas/navigation.py`
> 测试：`tests/test_project_context_contract.py`

## 1. 目的

让「项目 / 会话 / 计划 / 运行 / 步骤 / 验证结果」之间具备明确的从属关系：架构与执行数据
一律通过项目上下文读取，不再以全局状态暴露；普通对话（`chat`）不得进入项目执行框架。

## 2. context_id 规范

| 上下文 | context_type | 形态 | 示例 | 生成函数 |
| --- | --- | --- | --- | --- |
| 普通对话 | `chat` | `chat:<sessionId>` | `chat:session-1` | `chat_context_id()` |
| 项目 | `project` | `project:<projectId>` | `project:alpha` | `project_context_id()` |

规则：

1. `CONTEXT_TYPES = ("chat", "project")`，`DEFAULT_CONTEXT_TYPE = "project"`。
2. `context_id` 必须带类型前缀；`<key>` 需匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`。
3. `project_id` / `sessionId` 自身不含前缀，前缀只出现在 `context_id` 里。
4. 历史运行数据缺 `context_type` 时按 `project` 归属；缺 `context_id` 时回落到 `project:default`（**不迁移、不改写**历史数据）。
5. `DEFAULT_PROJECT_ID = "default"`，用于历史数据展示归位与默认工作区绑定。

## 3. 错误语义（可区分）

| code | HTTP | 触发条件 | 处理建议 |
| --- | --- | --- | --- |
| `missing_project_context` | 400 | 既没有 `project_id` 也没有项目 `context_id` | 前端跳 `#/projects` 并提示选择项目 |
| `invalid_project_context` | 400 | 无法解析（缺前缀、非法字符、把 `chat:` 当项目用） | 提示上下文非法，不进入项目模块 |
| `project_not_found` | 404 | 项目 ID 合法但未注册 | 提示项目不存在，回到项目列表 |
| `project_access_denied` | 403 | 项目存在但不在允许集合；`context_id` 与 `project_id` 冲突；跨项目读记录 | 提示无权限，不泄露项目数据 |

`ProjectContextError` 暴露 `code`、`status_code`、`project_id`、`context_id`，并提供 `to_dict()`。

## 4. 项目数据契约（`Project`）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_id` | `str` | 稳定 ID，不含 `project:` 前缀 |
| `name` | `str` | 展示名称，非空 |
| `status` | `ProjectStatus` | `active` / `archived` / `deleted` |
| `workspace` | `ProjectWorkspace \| None` | 工作区关联：`workspace_id`、`root_path`、`label`、`bound_at` |
| `description` | `str \| None` | 可选描述 |
| `created_at` / `updated_at` | `datetime` | 生命周期时间（UTC） |
| `last_activity_at` | `datetime` | 最近活动时间，供项目列表排序 |

派生属性：`context_id`（`project:<projectId>`）、`is_active`。

`ProjectContextResolver` 把外部输入解析为可信 `ProjectContext`（含 `context_id`、`project_id`、
`project_name`、`project_status`、`workspace_id`、`is_default`）；`required=False` 时允许“无上下文”。

## 5. 读取契约（计划 / 运行 / 步骤 / 验证结果）

四类记录都继承 `ProjectScopedRecord`，公共字段为 `project_id` 与 `context_id`：

| 读取契约 | 关键字段 |
| --- | --- |
| `PlanReadContract` | `plan_id`、`status`、`phases` |
| `RunReadContract` | `run_id`、`status`、`context_type`（缺省 `project`）、`started_at`、`updated_at` |
| `StepReadContract` | `step_id`、`run_id`、`status`、`order` |
| `VerificationResultReadContract` | `verification_id`、`run_id`、`step_id`、`passed`、`message` |

`project_id` 解析优先级（`project_id_from_record()`）：

1. 显式 `project_id`（或 `projectId`）；
2. `context_id` / `contextId` 解析（必须是 `project:` 前缀）；
3. 都缺 → 回落 `project:default`（历史数据）。

若 `context_id` 是 `chat:` 前缀，则抛 `invalid_project_context`：普通对话数据不得作为项目数据读取。
`ensure_same_project()` 用于读取前拦截跨项目访问，失败返回 `project_access_denied`。
`read_project_record(kind, payload)` 按 `plan` / `run` / `step` / `verification` 分发。

## 6. 导航数据契约（`app/schemas/navigation.py`）

一级入口（`PRIMARY_NAVIGATION`，顺序固定）：

| entry | label | context_type | 说明 |
| --- | --- | --- | --- |
| `chat` | 普通对话 | `chat` | 会话列表 / 最近会话；`modules` 为空 |
| `projects` | 项目 | `project` | 项目列表；承载全部 `ProjectModule` |

项目二级模块 `ProjectModule`：`overview` / `architecture` / `plan` / `execution` / `verification` /
`logs` / `settings`；`PROJECT_ONLY_ACTIONS` 即这 7 个模块名，`action_allowed("chat", <项目动作>)` 恒为 `False`。

路由：`PROJECT_MODULE_ROUTES` 提供 `#/projects/{project_id}[/<module>]` 模板；
`build_project_route()` / `default_project_route()` 生成具体地址，缺 `project_id` 报 `missing_project_context`；
`module_from_route()` 反解模块（`#/chat` 视为非法项目模块），`route_project_id()` 反解项目 ID。
`ProjectModuleContext.build(module, project_id)` 给出项目内模块的统一上下文（含 `context_id` 与 `route`）。

## 7. 隔离规则

1. 项目内模块（架构、计划、执行、步骤、验证、日志、设置）只接受 `ProjectContext`，不接受裸全局状态。
2. 左侧栏只持久化 `selectedProjectId`，项目数据按项目边界按需查询，不缓存跨项目结果。
3. 读取计划 / 运行 / 步骤 / 验证结果前，统一用 `ensure_same_project()` 校验归属。
4. 兼容映射不得把普通对话提升为项目，反向同样禁止（对应第 1 步第 5 节重定向规则）。
5. 错误语义不得合并：403 与 404 必须可区分，避免用 404 掩盖越权访问。

## 8. 验收对照

| 验收项 | 落点 |
| --- | --- |
| 项目契约含稳定 ID、名称、状态、工作区关联、最近活动 | `Project` / `ProjectWorkspace` / `ProjectStatus` |
| 导航契约区分普通对话、项目列表与项目内部模块 | `PRIMARY_NAVIGATION` / `ProjectModule` / `PROJECT_ONLY_ACTIONS` |
| 计划、运行、步骤、验证结果均能解析 `project_id` | `ProjectScopedRecord` 四个子契约 + `project_id_from_record()` |
| 缺失 / 无效 / 无权限错误语义可区分 | `ProjectContextErrorCode` + `status_code` |
| 测试覆盖无上下文、有效上下文、跨项目被拒三类 | `tests/test_project_context_contract.py` |

验证命令：

```bash
python -m py_compile app/schemas/project.py app/schemas/navigation.py
python -m pytest tests/test_project_context_contract.py -q
```

## 10. 落地状态（2026-09-16：从契约变成真实数据）

契约本身在第 2 步就写好了，但直到这一步之前，界面上的「项目 / 普通对话 / 项目内模块」只是壳子：
项目是从运行列表临时拼出来的候选、对话列表读的是没人写入的 `localStorage`、二级模块只是去点
页面上某个 `data-view` 元素。现在这些全部接到真实数据上：

| 能力 | 实现 | 接口 |
| --- | --- | --- |
| 项目容器（稳定 ID / 名称 / 状态 / 工作区绑定 / 最近活动） | `app/services/projects.py`（`data/projects.json`，原子写）+ `ProjectStore` | `GET/POST /api/v1/projects`、`GET/PUT/DELETE /api/v1/projects/{id}` |
| 项目与工作区一一绑定 | 建项目时创建/校验 `root_path`；运行未指定 `target_dir` 时落在项目工作区 | `POST /api/v1/projects {root_path}` |
| 运行归属项目 | `Run.project_id` + `context_type`，`GET /runs?project_id=` 过滤 | `POST /api/v1/runs {project_id}` |
| 普通对话会话（多轮、带历史、流式） | `Run(context_type="chat")` + `Orchestrator.create_chat/send_chat_message` | `GET/POST /api/v1/chats`、`GET /api/v1/chats/{id}`、`POST /api/v1/chats/{id}/messages`、`GET /api/v1/chats/{id}/events` |
| 项目内七个模块的真实数据 | `app/services/project_view.py` 按模块装配（概览 / 架构 / 计划 / 执行 / 验证 / 日志 / 设置） | `GET /api/v1/projects/{id}/modules/{module}` |
| 上下文互斥 | 普通对话不能进编排（`not_a_project_run`）；项目运行不能当对话（`not_a_chat_session`） | 见 `tests/test_chat_sessions.py` |

历史数据仍按本契约第 3 节处理：缺 `project_id` 的运行归到 `project:default`，`ProjectStore`
启动时**保证默认项目存在**，但默认项目**不绑定工作区**，以免改变"没指定项目时在运行目录内新建工作区"
的既有行为。

## 9. 非目标

1. 不修改执行编排、重启、指标与既有 API 行为（第 5 步验收后再评估下线）。
2. 不迁移、不清空历史运行数据与 `localStorage` 记录。
3. 本步只交付数据与导航契约；左侧栏渲染与前端路由改造属第 3 步。
