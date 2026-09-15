# 项目与普通对话：信息架构与上下文边界

> 状态：已定稿（第 1 步：定义导航与上下文模型）
> 适用范围：`web/app.js`（左侧栏与工作区）、`app/schemas/run.py`（上下文类型字段），后续 `app/api/routes.py` 与 `app/services/*` 的上下文校验

## 1. 目标与边界

左侧栏只暴露两个一级入口：**普通对话** 与 **项目**。

- **普通对话（context_type = `chat`）**：轻量、无项目生命周期约束的独立工作区。只做消息收发，
  不产生运行（run）、不产生计划与步骤、不触发架构与执行。
- **项目（context_type = `project`）**：承载完整工作流的容器。架构、执行、计划、步骤、验证、
  项目事件与项目上下文，全部且仅存在于项目之下。

判定原则：**一个工作区属于普通对话还是项目，由上下文类型字段决定，不能由 UI 位置或历史遗留字段推断。**

## 2. 一级入口

| 一级入口 | context_type | 稳定标识 | 左侧栏分组 | 承载能力 |
| --- | --- | --- | --- | --- |
| 普通对话 | `chat` | `context_id` = 会话 id（前缀 `chat:`） | 会话列表 | 消息（可选附件） |
| 项目 | `project` | `context_id` = 项目 id（前缀 `project:`） | 项目列表，每项下挂二级导航 | 架构、执行、计划、步骤、验证、事件、项目上下文 |

前端常量（定义于 `web/app.js`）：

- `CONTEXT_CHAT = "chat"`
- `CONTEXT_PROJECT = "project"`
- `CONTEXT_TYPES = [CONTEXT_CHAT, CONTEXT_PROJECT]`
- `DEFAULT_CONTEXT_TYPE = CONTEXT_PROJECT`
- `normalizeContextType(value)`：非法值一律回落到 `DEFAULT_CONTEXT_TYPE`

## 3. 项目内二级能力

| 二级能力 | 中文名 | 现有实现 | 允许的上下文 |
| --- | --- | --- | --- |
| architecture | 架构 | `app/services/architect.py` | 仅 `project` |
| execution | 执行 | `app/services/executor.py`、`app/services/orchestrator.py` | 仅 `project` |
| plan | 计划 | `app/schemas/plan.py`、`app/services/storage.py` | 仅 `project` |
| steps | 步骤 | `app/schemas/step.py` | 仅 `project` |
| verify | 验证 | `app/services/verify.py` | 仅 `project` |
| events | 项目事件 | `app/services/events.py` | 仅 `project` |
| context | 项目上下文 / 工作区 | `app/services/context.py`、`app/services/workspace.py` | 仅 `project` |

普通对话之下**没有**二级导航，也不渲染上述任何面板。

## 4. 字段与数据模型

`app/schemas/run.py`：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `context_type` | `Literal["chat", "project"]` | `"project"` | 上下文类型，稳定标识 |
| `context_id` | `str` | `""` | 上下文实例标识；空串表示沿用默认归属 |

约定：

1. 创建运行时必须显式给出 `context_type`；缺省即按 `project` 归属。
2. 项目内的所有运行 `context_type` 必须为 `project`。
3. `chat` 上下文不产生运行；后端收到 `context_type == "chat"` 的运行创建请求应直接拒绝。

## 5. 禁止跨上下文的操作

| 操作 | 归属上下文 | 在普通对话中的行为 |
| --- | --- | --- |
| 启动 / 重写架构 | 项目 | 拒绝（按钮不渲染 + 后端返回 400） |
| 启动执行 / 运行步骤 | 项目 | 拒绝 |
| 验证步骤 | 项目 | 拒绝 |
| 重启 / 取消运行 | 项目 | 拒绝 |
| 读取运行、计划、步骤、事件列表 | 项目 | 拒绝（不请求、不展示） |
| 打开项目上下文 / 工作区文件 | 项目 | 拒绝 |
| 在项目里发送纯聊天消息 | 普通对话 | 允许（降级为普通消息，不进入编排） |

约束：**上下文类型不可由前端在请求中提升或篡改**，后端以会话 / 项目上下文为准做校验。

前端守卫集合（`web/app.js`）：

- `PROJECT_SECTIONS = ["architecture", "execution", "plan", "steps", "verify", "events"]`
- `PROJECT_ONLY_ACTIONS`：普通对话上下文中一律不派发的动作集合。

## 6. 旧数据默认归属规则（可执行）

1. 缺少 `context_type` 字段 → 一律归属为 `project`（历史数据全部是编排运行，天然属于项目）。
2. `context_type` 的值不在 `["chat", "project"]` 中 → 视为缺失，按第 1 条处理。
3. 缺少 `context_id` → 归属到该运行所属项目；无法确定项目时归属到默认项目 `project:default`。
4. 前端 `localStorage`（如 `orchestrator.lastRun`）中记录的运行，若解析后没有合法上下文类型 →
   按 `project` 处理，且不清空历史记录。
5. 普通对话会话不得因旧数据默认规则被提升为项目；反向不成立。

## 7. 现有资产映射

| 现有资产 | 新归属 |
| --- | --- |
| `web/app.js` 运行列表 / 打开运行 / SSE 事件流渲染 | 项目上下文，二级导航「执行」 |
| `web/app.js` 统计看板 | 项目上下文 |
| `app/api/routes.py` 运行、计划、步骤、验证相关路由 | 项目上下文，需校验 `context_type` |
| `app/services/orchestrator.py`、`executor.py`、`architect.py`、`verify.py`、`events.py` | 项目上下文 |
| `app/services/context.py`、`workspace.py` | 项目上下文 |
| 普通会话 / 消息 | 普通对话上下文（本步仅定义模型，落地见第 2、3 步） |

## 8. 后续步骤的落点（不在本步执行）

1. 第 2 步：按本文档重组左侧栏与工作区入口（普通对话 / 项目两个一级入口）。
2. 第 3 步：收敛项目能力边界，为路由与服务补上上下文校验。
3. 第 4 步：补齐项目内导航与兼容验证（含旧数据默认归属的回归用例）。
