# 左侧栏信息架构：普通对话与项目

> 状态：已定稿（第 1 步：固化信息架构与路由契约）
> 上位契约：`docs/project-and-chat-information-architecture.md`（上下文类型、旧数据默认归属以其为准）
> 配套契约：`docs/project-navigation-contract.md`（路由表、数据上下文、空状态、兼容重定向）
> 适用范围：`web/app.js`（左侧栏与工作区）、后续 `app/api/routes.py` 与 `app/services/*` 的上下文校验

## 1. 结论（唯一基准）

左侧栏一级导航**有且仅有两个**入口：

1. **普通对话**
2. **项目**

一级导航**不得出现**任何工程/运行概念，包括但不限于：架构、执行、计划、步骤、验证、事件、日志、设置、插件、统计看板、运行列表、模型与供应商配置。

判定原则：一个工作区属于普通对话还是项目，只由上下文字段 `context_type`（`chat` / `project`）决定，**不得**由 UI 位置、历史遗留字段或数据来源推断。

## 2. 一级导航定义

| 顺序 | 入口 | 稳定标识 | 默认路由 | 数据上下文 | 左侧栏呈现 |
| --- | --- | --- | --- | --- | --- |
| 1 | 普通对话 | 会话列表（`chat:*`） | `#/chat` | `context_type=chat` | 扁平会话列表，选中项直接进入消息区 |
| 2 | 项目 | 项目列表（`project:*`） | `#/projects` | `context_type=project` | 项目列表，选中项展开该项目二级导航 |

两个一级入口互相独立：切换入口不改变对方的数据上下文；普通对话不创建、不绑定项目。

## 3. 普通对话的边界

- 只有消息收发（可含附件），没有运行（run）、计划（plan）、步骤（step）、验证（verify）、架构产物和项目事件。
- 不显示任何项目执行框架（进度条、步骤时间线、日志面板、统计看板）。
- 路由层命中 `PROJECT_ONLY_ACTIONS`（架构/执行/计划/步骤/验证/日志/设置等动作）时一律不派发。
- 会话可长期存在；删除会话只删除消息，不影响任何项目数据。

## 4. 项目二级模块（全部且仅在项目内）

| 顺序 | 模块 | 模块 id | 路由 | 承载能力 | 允许上下文 |
| --- | --- | --- | --- | --- | --- |
| 0 | 概览 | `overview` | `#/projects/:projectId` | 项目摘要、最近运行、快捷入口 | 仅 `project` |
| 1 | 架构 | `architecture` | `#/projects/:projectId/architecture` | 架构设计与产物 | 仅 `project` |
| 2 | 计划 | `plan` | `#/projects/:projectId/plan` | 计划与阶段 | 仅 `project` |
| 3 | 执行 | `execution` | `#/projects/:projectId/execution` | 运行、编排、重启、指标 | 仅 `project` |
| 4 | 步骤 | `steps` | `#/projects/:projectId/steps` | 步骤状态与产物 | 仅 `project` |
| 5 | 验证 | `verify` | `#/projects/:projectId/verify` | 验证结果与报告 | 仅 `project` |
| 6 | 日志 | `logs` | `#/projects/:projectId/logs` | 项目事件与运行日志 | 仅 `project` |
| 7 | 设置 | `settings` | `#/projects/:projectId/settings` | 项目工作区、模型、扩展配置 | 仅 `project` |

二级导航顺序固定为上表顺序，且只在选中项目后渲染；未选中项目时左侧栏只显示一级入口与项目列表。

## 5. 能力归属矩阵

| 能力 | 普通对话 | 项目 |
| --- | --- | --- |
| 消息收发 | 允许 | 允许（对话区） |
| 架构（architecture） | 禁止 | 允许 |
| 执行 / 运行（execution, run） | 禁止 | 允许 |
| 计划（plan） | 禁止 | 允许 |
| 步骤（steps） | 禁止 | 允许 |
| 验证（verify） | 禁止 | 允许 |
| 事件与日志（events, logs） | 禁止 | 允许 |
| 设置（settings） | 禁止 | 允许 |

「禁止」的判定必须在执行层完成，不能只靠隐藏按钮：普通对话上下文下不得派发对应动作、不得发起对应查询。

## 6. 上下文标识约定

- `CONTEXT_CHAT = "chat"`，`context_id` 形如 `chat:<sessionId>`
- `CONTEXT_PROJECT = "project"`，`context_id` 形如 `project:<projectId>`
- `CONTEXT_TYPES = ["chat", "project"]`，`DEFAULT_CONTEXT_TYPE = "project"`
- `normalizeContextType(value)`：非法值一律回落 `DEFAULT_CONTEXT_TYPE`
- 缺省项目：`project:default`，仅在旧数据无法定位所属项目时使用
- 旧数据默认归属规则见上位契约第 6 节（缺 `context_type` → 归属 `project`）

## 7. 空状态总则

所有空状态由三部分构成：**标题**（说明缺什么）、**解释**（为什么为空、影响是什么）、**主行动**（唯一的下一步按钮）。禁止只显示「暂无数据」。各路由的具体空状态见 `docs/project-navigation-contract.md` 第 1、4 节。

## 8. 与既有资产的关系

| 既有资产 | 新归属 |
| --- | --- |
| 左侧栏运行列表 / 统计看板 | 项目 → 执行 |
| 全局设置、插件管理 | 项目 → 设置（按项目生效） |
| 运行、计划、步骤、验证相关 API（`/api/v1` 前缀） | 项目上下文，需校验 `context_type` |
| 现有会话 / 消息 | 普通对话 |

## 9. 本步验收清单

- [x] 一级导航仅包含普通对话与项目（第 1、2 节）
- [x] 架构、执行、步骤、验证、日志、设置仅位于项目上下文内（第 4、5 节）
- [x] 路由、数据上下文、空状态与兼容策略见 `docs/project-navigation-contract.md`
