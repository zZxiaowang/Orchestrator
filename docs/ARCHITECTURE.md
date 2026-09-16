# 架构基线（As-Is + 演进）

> 用途：**后续开发的唯一架构基线**。描述"现在真实是什么样"，以及"往哪长"。
> 与其它文档的关系：README 讲怎么用，HANDOFF 讲交接要事，本文讲结构、边界与扩展点；
> 具体契约细节仍见 `docs/*-contract*.md`。
> 更新规则：**动了结构（分层、模块、接口、存储布局）就必须同时改本文**。

基线日期：2026-09-16　分支：`main`　质量门：pytest 364 项 / ruff / 界面自检 88 项 / 打包版 `--selftest`

---

## 1. 产品与核心链路

**一句话**：用 GPT 把需求变成可验收的纲领，人工确认后由 DeepSeek V4 逐步落地成真实文件改动，
每一步都有客观验收、可回滚、可继续对话。

```
需求 ──▶ 架构段(GPT) ──▶ 纲领(目标/原则/组件/步骤/验收) ──▶ 人工确认门 ──▶ 执行段(DeepSeek) ──▶ 文件改动+报告
                                                          │
                                    步骤级：客观验收 · 可回滚 · 可续聊 · 指标账本
```

两条上下文边界（不可混淆）：

* **项目**：承载架构 / 计划 / 执行 / 验证 / 日志 / 设置，与工作区一一绑定，运行按 `project_id` 隔离；
* **普通对话**：只收发消息，不产生纲领与步骤、不碰工作区；有自己的长上下文管理
  （滚动窗口 + 承接摘要 + 超阈值开新对话，见 `docs/chat-context-window.md`）。

---

## 2. 技术栈与选型理由

| 层 | 选择 | 为什么 |
| --- | --- | --- |
| 语言 | Python 3.12 | 单进程本地工具 + 一个 OpenAI 兼容 Key，标准库够用；PyInstaller 打包成熟 |
| HTTP | FastAPI + uvicorn | 路由/校验/SSE 都直接可用；测试用 TestClient 就够 |
| 界面 | 原生 HTML/CSS/JS（**无构建步骤**） | 打包只需一步（PyInstaller 打 web/）；代价是没有类型系统，靠"契约 + 注入 + 自检"兜底 |
| 桌面 | pywebview（WebView2） | 双击即原生窗口；同一份代码也能 `--server` 用浏览器跑 |
| 存储 | JSON 文件（`data/`） | 单机单用户；运行/项目/能力各自一个目录，原子写 |
| 模型调用 | 自研 `RelayClient`（chat_completions / responses 双协议 + 自动降级） | 中转网关能力不一，必须自己处理探测、降级、重试与 usage 记账 |

**明确不引入**：前端框架与打包链（会把"一步打包"变成三步）、数据库（收益低于迁移风险）。

---

## 3. 分层与目录

```
app/
  core/            配置、错误、日志、relay 客户端、插件/目录存储        ← 不依赖上层
  schemas/         领域契约：run / plan / step / project / navigation / capability
  capabilities/    ★统一能力层：skill / MCP / 插件（registry 是唯一读写入口）
  services/        领域服务：orchestrator / architect / executor / verify / commands /
                   context / chat_context / projects / project_view / gitguard / storage …
  api/             路由：routes.py（聚合 + 运行/项目/设置/Git/市场）+ capabilities.py（能力）
  main.py          装配 FastAPI、静态界面（注入版本号与模块清单）、错误映射
  desktop.py       桌面客户端入口（窗口 / 自检 / `--server` 透传）
web/
  index.html       页面骨架 + 后端注入的 `__ORCHESTRATOR_PROJECT_MODULES__`
  app.js           前端（状态 / 路由 / 渲染 / 面板）
  styles.css
data/              settings.json · projects.json · runs/<id>/ · capabilities/ · plugins/ · logs/
```

依赖方向严格单向：`core → schemas → services → api → main`；前端只通过 `/api/v1` 取数。

---

## 4. 模块地图（改哪里 + 测哪里）

| 模块 | 关键文件 | 接口 | 测试 |
| --- | --- | --- | --- |
| 运行编排 | `services/orchestrator.py` | `/runs*` | `test_api_flow` `test_metrics_flow` `test_cancel_state` |
| 上下文裁剪 | `services/context.py` | （内部） | `test_context` |
| 普通对话 | `services/chat_context.py` + orchestrator 的 chat 段 | `/chats*` | `test_chat_context` `test_chat_sessions` |
| 客观验收 | `services/verify.py` | （内部） | `test_verify` |
| 受控命令 | `services/commands.py` | （内部） | `test_commands` |
| 项目容器 | `services/projects.py` + `services/project_view.py` | `/projects*` | `test_projects_api` |
| 项目边界契约 | `schemas/project.py` `schemas/navigation.py` | `/navigation/*` | `test_project_context_contract` `test_navigation_information_architecture` |
| **能力层** | `capabilities/registry.py` `api/capabilities.py` | `/capabilities*` | `test_capabilities` |
| 模型调用 | `core/relay.py` `core/fallback.py` | （内部） | `test_relay` `test_provider_fallback` `test_relay_stats` |
| 配置与多套 Provider | `core/config.py` `core/providers.py` | `/settings` `/providers` `/routes` | `test_settings` `test_data_dir` |
| Git 能力 | `services/git_service.py` `gitguard.py` | `/git/*` | `test_git_service` `test_gitguard` |
| 插件市场（legacy） | `core/plugins.py` `core/catalog.py` | `/plugins` `/market/*` | `test_marketplace` |
| 桌面客户端 | `desktop.py` | `--selftest` `--server` | `test_desktop` `test_server_mode` |
| 前端 | `web/app.js` | — | `scripts/ui_check.mjs`（88 项） |

最小化修改的口径：改一个模块 = 改上表对应行里的文件 + 跑对应测试；跨行的改动说明边界被打破了，
先在本文里更新边界，再动代码。

---

## 5. 能力层（skill / MCP / 插件）

### 5.1 为什么要有这一层

三种能力形态各写一套安装 / 启用 / 作用域 / 审计，会让导航、设置、前端每加一种就再改一遍
（历史教训：项目模块清单曾经在 4 处重复）。所以：**同一份存储、同一套状态、同一套接口**，
形态差异只放在 `meta` 里。

```
Capability { id, kind(skill|mcp|plugin), name, description, version,
             source{kind,location}, enabled, scope(global|project), project_id,
             permissions[], meta{}, installed_at, updated_at, last_used_at }
```

存储：`data/capabilities/registry.json`（原子写）+ `audit.jsonl`（安装 / 启用 / 卸载 / 同步各一行，
超 2MB 轮转）。接口：`GET /api/v1/capabilities`、`POST /capabilities/{id}/enable|disable`、
`DELETE /capabilities/{id}`、`GET /capabilities/audit`。

### 5.2 形态与边界

| 形态 | 是什么 | 默认是否执行代码 | 边界 |
| --- | --- | --- | --- |
| `skill` | 指令包（`SKILL.md` + 可选 scripts / references / assets） | 否（只按需注入上下文） | 自带脚本执行走命令白名单 + 每次确认 |
| `mcp` | 工具服务器（stdio / Streamable HTTP） | 是（server 本体是进程） | 默认关闭、按项目启用、首次运行确认、全部审计 |
| `plugin` | 旧声明式插件（只登记 UI 入口与能力声明） | 否 | 只做镜像；安装 / 卸载仍在插件市场 |

### 5.3 工具调用协议（应用层）

模型通过 JSON 请求工具调用，**不依赖网关的 `tools` 字段**（中转不一定支持）：

```json
{"tool_calls": [{"capability_id": "mcp.filesystem", "tool": "read_file",
                 "arguments": {"path": "README.md"}, "reason": "需要看现有说明"}]}
```

执行段输出它 → 应用校验（启用状态 / 作用域 / 白名单 / 首次确认）→ 执行 → 把结果回灌进**同一步**继续，
与 `need_files`、`commands` 共用同一个循环。原生 function-calling 以后作为可选加速项，主链路不变。

---

## 6. 关键流程

| 流程 | 落点 | 要点 |
| --- | --- | --- |
| 规划 | `architect.py` + `orchestrator._plan` | 先意图分流（问答不走编排）；纲领解析失败会强制 JSON 重试 |
| 确认门 | `RunStatus.AWAITING_APPROVAL` | 空 feedback = 执行；有 feedback = 重做纲领 |
| 执行一步 | `orchestrator._execute_step` | 分层上下文 → 执行段 → 落地文件 → 受控命令 → 客观验收（失败先自动补一轮） |
| 客观验收 | `verify.py` | 文件存在 / 包含、glob、JSON、`py_compile`（写过的 .py 自动补）、`py_import`（需开命令执行） |
| 回滚一步 | `gitguard.py` | 只还原这一步碰过的文件；非 git 仓库用 `backup/` 兜底 |
| 续聊 | `POST /runs/{id}/continue` | 只追加新步骤，旧步骤与事件序号不动 |
| 长上下文 | `chat_context.py` | 窗口 + 增量摘要；摘要超限自动开新对话并留承接链 |
| 能力调用 | `capabilities/registry.py` + 工具协议 | 见 §5.3 |
| 指标账本 | `services/metrics.py` + `Run.metrics` | 每阶段 / 每步一条：调用数、耗时、token、上下文构成、重试 |

---

## 7. 数据布局

| 路径 | 内容 | 权威写入方 |
| --- | --- | --- |
| `data/settings.json` | 多套 Provider、分段路由、全局选项（含 Key，已 gitignore） | `core/config.py` |
| `data/projects.json` | 项目：ID / 名称 / 状态 / 工作区绑定 / 最近活动 | `services/projects.py` |
| `data/projects/<id>/workspace/` | 项目默认工作区（没填目录时） | `services/projects.py` |
| `data/runs/<id>/run.json` | 运行：状态机 / 步骤 / 消息 / 验收 / 指标 / 长上下文承接链 | `services/storage.py` |
| `data/runs/<id>/{workspace,backup,plan.md,report.md}` | 工作区、改动备份、纲领与报告 | 编排器 |
| `data/capabilities/{registry.json,audit.jsonl}` | 已装能力与审计 | `capabilities/registry.py` |
| `data/plugins/*` | 已装插件与市场来源（legacy，镜像进能力层） | `core/plugins.py` `core/catalog.py` |
| `data/logs/orchestrator.log` | 运行日志（轮转 3 份） | `core/logging.py` |

---

## 8. 质量门

| 门 | 命令 | 何时必须跑 |
| --- | --- | --- |
| 单元 / 集成 | `python -m pytest -q`（364 项） | 每次改动 |
| 静态检查 | `python -m ruff check .` + `ruff format --check .` | 每次改动 |
| 界面自检 | `node scripts/ui_check.mjs --url http://127.0.0.1:8788`（88 项） | 改前端 / 改接口契约 |
| 打包自检 | `.\dist\Orchestrator.exe --selftest 3` | 发版前 / 改前端或启动逻辑 |

界面自检只允许打**演示实例**（8788 + 假中转 8799，脚本里有安全闸）；
演示数据在 `.logs/ui-check-data`，不要写进 `data/`。

---

## 9. 演进路线（前瞻）

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| **P0 地基** | 能力层（契约 + 注册表 + 接口 + 审计）、模块清单单一来源、删除历史死代码、能力中心入口、架构基线文档 | ✅ 本次完成 |
| **P1 Skills** | `SKILL.md` 解析与资格校验、三种来源安装（本地 / HTTPS 清单 / GitHub）、启用与作用域、按需注入执行段上下文（带预算）、能力中心安装 UI | 待做 |
| **P2 MCP** | MCP 客户端（stdio + Streamable HTTP）、server 配置与启用、`tools/list` 缓存、应用层 `tool_calls` 闭环、手动调用面板、常用 server 预设、审计与超时 | 待做 |
| **P3 UI 收敛** | 能力中心合并「插件市场 / 已装插件」、设置四区收敛、折叠规则统一、四态（加载 / 空 / 错误 / 有数据）统一 | 待做 |

### 已下线的历史层（不要再照着写）

| 删掉的 | 原因 | 现在的落点 |
| --- | --- | --- |
| `services/project_workspace.py` | 契约期产物，`app/` 无人引用，只有测试在撑着 | `services/projects.py` + `schemas/project.py` |
| `services/navigation_migration.py` | 旧路由翻译层，前端已自己实现 `#/runs`、`#/settings` 重定向 | `web/app.js` 的 `parseRoute` / `applyRoute`（自检覆盖） |
| `services/metrics_sink.py` | 旧 `{architect,steps}` 字典形态，与 `Run.metrics` 并存会误导 | `services/metrics.py` + `Run.metrics` |

---

## 10. 约定与禁忌

1. **契约先行**：新增字段 / 模块先改 `schemas/`，再改服务与接口，最后改前端；前端不猜字段。
2. **唯一权威**：项目模块清单在 `schemas/navigation.py`，由后端注入页面；能力形态清单在 `api/capabilities.py`。
3. **别名而非静默改名**：接口改名要留别名（如 `steps→execution`），否则历史链接与旧记录会 404。
4. **单文件上限**：新增逻辑按模块落文件；`orchestrator.py`、`routes.py`、`web/app.js` 只做聚合，
   下次动到哪块就顺手拆哪块（绞杀者模式，不专门排期重写）。
5. **安全默认关闭**：会执行代码的能力（命令、MCP、skill 脚本）默认关闭、按项目启用、首次确认、全审计。
6. **失败要可操作**：错误信息必须说明"哪个字段 / 哪一步 + 怎么改"，不把底层异常直接丢给界面。
7. **不改历史数据**：旧记录缺失字段一律按契约回落，不迁移、不改写。
