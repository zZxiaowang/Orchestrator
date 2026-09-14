# 交接文档（HANDOFF）

> 用途：换新会话/新窗口继续开发时，先读这一份。记录**需求清单**、**数据与文件位置**、
> 当前状态与待办。最后附「新窗口开场提示词」，可直接粘贴。

---

## 一、这个项目是什么

**架构-执行双模型编排器**：架构段（GPT）先把需求转成可验收的纲领，人工确认后由执行段（DeepSeek V4）
逐步落地成真实文件改动。只依赖一个 OpenAI 兼容的中转 API Key。

- 形态：Windows 桌面客户端（WebView2 原生窗口）+ 浏览器形态（本地 HTTP）
- 技术栈：Python 3.12 + FastAPI + 原生 JS/CSS（无构建步骤），PyInstaller 打包
- 仓库：`D:\orchestrator` → `https://github.com/zZxiaowang/Orchestrator.git`（分支 `main`）

## 二、需求清单（按时间顺序，含落地情况）

| # | 你提的需求 | 落地情况 |
|---|---|---|
| 1 | 用中转 API Key；架构/纲领性问题用 GPT，执行用 DeepSeek V4 | ✅ 两段式内核：`app/services/architect.py`、`executor.py` |
| 2 | 先查是否已有类似工具 | ✅ 调研 Aider(48.9k)、claude-code-router(37.2k)、LiteLLM/one-api、Roo/Cline 等，结论见讨论 |
| 3 | 联网查 GitHub 是否有同类 | ✅ 同上（GitHub API 检索） |
| 4 | Aider 的设计可以，但 UI 要 Codex 风格 | ✅ 三栏浅色工作台：对话时间线 / 纲领 / 变更审查 |
| 5 | 我用的是 Windows | ✅ 一键脚本（`.cmd`，CRLF）、UTF-8 控制台、启动器、打包器 |
| 6 | 很多功能点击没反应 + 要白色背景 | ✅ 修复遮罩拦截点击、竞态覆盖、脚本异常；改为白色主题 |
| 7 | 架构段异常：expected str, bytes found | ✅ `resp.aread()` 解码；新增流式→非流式降级 |
| 8 | 401 Invalid token | ✅ 识别"Key 栏填成了地址"，保存即拦截；Key 自动清洗 |
| 9 | 测试连接后 Key 自动消失 | ✅ 保留输入框内容 + 掩码提示 |
| 10 | 滚轮无法操作 | ✅ 修复 flex 子项被压扁（`flex-shrink:0`）+ 容器 `min-height:0` |
| 11 | 中转与个人 Key 灵活切换 | ✅ 多套配置（Provider）+ 侧栏一键切换 |
| 12 | 架构与执行可分别用中转/个人 | ✅ 分段路由 `PUT /api/v1/routes`，面板可选每段配置与模型 |
| 13 | 502（Cloudflare 回源失败） | ✅ 流式请求重试 + 可读提示；建议切换配置 |
| 14 | 长上下文 token 消耗 / 压缩 | ✅ 分层上下文 + 预算裁剪 + 纲领摘要 + 交接日志折叠 + 执行段按需取文件（need_files），禁止模型自行压缩 |
| 15 | 第 1 步被阻塞（空工作区） | ✅ 新增 `blocked` 状态与「补充信息并继续」；架构段会被告知空工作区 |
| 16 | 把对话内容交给编排器执行下一阶段 | ✅ 生成 14 步纲领并执行 P0（步骤 1–6），产物见 `docs/next-phase-architecture.md` 等 |
| 17 | 参考 dsh-desktop 做插件市场 | ✅ `app/core/catalog.py`、`plugins.py` + 市场面板（声明式插件、能力白名单、HTTPS 目录源） |
| 18 | 左侧任务栏参照 dsh-desktop | ✅ 可折叠图标栏 + 底部槽位（market/plugins/git/updates/settings），带 `data-entry`/`data-seat` 锚点 |
| 19 | 做成 exe 可打包 | ✅ `scripts/package.ps1` → `dist\Orchestrator.exe`（约 20.7 MB，无控制台） |
| 20 | 全部数据与源码移到 D 盘 orchestrator | ✅ 已迁移，并改写运行记录里的绝对路径（36 文件 / 37 处） |
| 21 | 连接我的 git | ✅ `git-connect.ps1`；远端 `zZxiaowang/Orchestrator` |
| 22 | （给了账号密码） | ⚠️ **未使用、未落盘**；建议改密码，推送改用 Personal Access Token |
| 23 | 再试一次推送 | ✅ 成功；根因是 **Windows 系统代理 git 不读**，已按域名配置 `http.https://github.com/.proxy` |
| 24 | 加类似 IDEA 的 Git 功能（简化）+ 每天开机自动提交开关 | ✅ Git 面板 + 启动文件夹开关（`scripts\auto-commit.cmd/.ps1`） |
| 25 | 所有功能都要做成界面按钮 | ✅ 面板共 29 个按钮（含代理/分支/暂存/丢弃/选中提交/历史 diff/自动提交） |
| 26 | 重新打包（图形界面找不到 Git） | ✅ 修复打包版仓库路径（`project_root()`），重打并自检通过 |
| 27 | Git 每次点击要等很久 | ✅ 状态查询 7 次 git 调用压到 3 次 + 代理状态缓存（实测面板刷新 228ms） |

## 三、数据与文件位置（重要）

### 1. 源码与仓库

| 内容 | 位置 |
|---|---|
| 项目根（git 仓库，分支 main） | `D:\orchestrator` |
| 远端 | `https://github.com/zZxiaowang/Orchestrator.git` |
| 后端源码 | `D:\orchestrator\app\`（`core/` `schemas/` `services/` `api/`） |
| 前端界面（无构建） | `D:\orchestrator\web\`（`index.html` `styles.css` `app.js`） |
| 测试 | `D:\orchestrator\tests\`（166 项 pytest） |
| 架构/契约文档 | `D:\orchestrator\docs\`（含本文件、`next-phase-architecture.md` 等） |

### 2. 运行时数据（**全部在 `D:\orchestrator\data`**）

| 内容 | 位置 | 说明 |
|---|---|---|
| 主配置 | `D:\orchestrator\data\settings.json` | 多套 Provider（中转 / 个人 Key）、分段路由、全局选项；**含 API Key** |
| 配置备份 | `D:\orchestrator\data\settings.json.bak-*` | 每次修复前备份（如 `settings.json.bak-20260914-195307`） |
| 运行记录 | `D:\orchestrator\data\runs\<run-id>\run.json` | 状态机、步骤、消息、指标 |
| 纲领/报告 | `D:\orchestrator\data\runs\<run-id>\plan.md`、`report.md` | 可归档产物 |
| 运行工作区 | `D:\orchestrator\data\runs\<run-id>\workspace\` | 未指定落地目录时的产出位置 |
| 改动备份 | `D:\orchestrator\data\runs\<run-id>\backup\` | 覆盖/删除前的原件 |
| 已装插件 | `D:\orchestrator\data\plugins\installed.json` | 声明式插件清单 |
| 市场来源 | `D:\orchestrator\data\plugins\sources.json` | 目录源（HTTPS 清单） |
| 服务日志 | `D:\orchestrator\data\logs\orchestrator.log` | 桌面客户端/后端日志（轮转 3 份） |

### 3. 打包与启动

| 内容 | 位置 |
|---|---|
| 打包产物（单文件桌面客户端） | `D:\orchestrator\dist\Orchestrator.exe`（约 20.7 MB） |
| 快捷方式 | `D:\orchestrator\dist\Orchestrator.lnk` |
| 打包工作目录（可删，临时缓存） | `D:\orchestrator\build\` |
| 推荐启动（复用同一份 data） | 双击 `D:\orchestrator\start-client.cmd` |
| 源码启动（浏览器形态） | `D:\orchestrator\start.cmd`；离线演示 `start-demo.cmd`；停止 `stop.cmd` |
| 打包脚本 | `D:\orchestrator\scripts\package.ps1`（`-OneDir` 目录版 / `-Console` 保留控制台 / `-SkipTests`） |

### 4. 每日开机自动提交（Git 面板开关的落点）

| 内容 | 位置 |
|---|---|
| 启动项（开关开启时存在） | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Orchestrator-AutoCommit.cmd` |
| 自动提交脚本 | `D:\orchestrator\scripts\auto-commit.cmd` → `auto-commit.ps1` |
| 自动提交日志 | `D:\orchestrator\.logs\auto-commit.log` |
| 关闭方式 | 面板关开关，或直接删除上面那个启动项文件 |

### 5. 其它

| 内容 | 位置 |
|---|---|
| 临时脚本 / 截图 / 迁移前源码快照 | `D:\orchestrator\.logs\`（已 gitignore） |
| 环境变量模板 | `D:\orchestrator\.env.example`（真实 `.env` 不入库） |
| 打包版旧数据目录（**已不使用**） | `D:\orchestrator\dist\data\`（可删） |
| 界面自检脚本 | `D:\orchestrator\scripts\ui_check.mjs`、冒烟 `scripts\smoke_check.py` |
| 界面自检截图 | `D:\orchestrator\.logs\ui-light.png` |

### 6. 配置优先级

```
data\settings.json（界面保存）  >  环境变量 / 项目根 .env  >  代码默认值
```

常用环境变量：`RELAY_BASE_URL`、`RELAY_API_KEY`、`ARCHITECT_MODEL`、`EDITOR_MODEL`、
`ORCHESTRATOR_DATA_DIR`（换数据目录）、`ORCHESTRATOR_IGNORE_SAVED_SETTINGS=1`（演示/测试用，忽略已保存配置且不写盘）。

## 四、当前状态

| 项 | 值 |
|---|---|
| 分支 / 远端 | `main` / `https://github.com/zZxiaowang/Orchestrator.git`（已同步） |
| 你的配置 | 「默认配置」= 中转 `https://api.routescope.ai/v1`，`responses`，`gpt-5.6-sol` / `deepseek-v4-flash`；另有「deepseek」官方直连 |
| 服务 | 源码实例 `http://127.0.0.1:8787`；演示实例 8788 + 假中转 8799（按需启动） |
| 质量门 | pytest **166 项**通过；ruff check/format 全绿；界面自检 46 项（44 通过，2 项为统计看板待接线） |
| 桌面自检 | 渲染 PASS、点击链路 PASS、Git 面板 29 按钮 PASS |
| 推送 | 已配置 `http.https://github.com/.proxy = http://127.0.0.1:10809`（**只对 github.com 生效**） |

## 五、待办（下一步可做）

1. **统计看板接线**：`app/services/metrics_sink.py` 与 `Run.metrics` 契约不一致（`list[PhaseMetrics]` vs `{architect,steps}` 字典），未接线 → 面板显示不出耗时/token/重试。
2. 编排器「下一阶段迭代」的 **P1 / P2**（步骤 7–14）：受控命令执行、大仓库检索、多轮续聊、并发队列、分发常驻。
3. 账号密码安全问题：**改密码**，推送用 PAT。
4. 可选：exe 图标、Inno Setup/NSIS 安装包；Git 面板「暂存/丢弃」的批量选择体验优化。

## 六、新窗口开场提示词（可直接粘贴）

```text
继续开发 D:\orchestrator（架构-执行双模型编排器，git 仓库，远端 zZxiaowang/Orchestrator）。

先读这两份再动手：
- D:\orchestrator\docs\HANDOFF.md（需求清单 / 数据位置 / 待办）
- D:\orchestrator\README.md（使用与打包说明）

环境要点：
- 数据全在 D:\orchestrator\data（settings.json 含我的中转 Key，已被 .gitignore 排除，别提交）
- 源码实例跑在 127.0.0.1:8787；界面自检只允许打演示实例 8788（脚本有安全闸）
- git 走代理 http://127.0.0.1:10809，仅对 github.com 生效
- 质量门：pytest + ruff 必须全绿；改前端后跑 scripts/ui_check.mjs 与 dist 打包自检

本次要做的任务：<在这里写你这次要做什么>
```
