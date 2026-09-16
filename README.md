# 编排器（Orchestrator）

> 用 GPT 产出**纲领性架构**，用 DeepSeek V4 按纲领**落地执行**；界面是 Codex 风格的三栏工作台。
> 只依赖一个 OpenAI 兼容的**中转 API Key**。

界面为**浅色主题**（白底）。

## 这个工具解决什么

单一模型同时负责"想清楚"和"做出来"时，容易出现两种偏差：要么纲领空泛无法验收，要么直接埋头改代码而丢失整体结构。
本工具把两件事拆成两段、交给两个模型，并在中间加一道**人工确认门**：

```
需求 ──▶ 架构段（GPT）──▶ 纲领（目标/原则/组件/步骤/验收标准）──▶ 你确认 ──▶ 执行段（DeepSeek V4）──▶ 文件改动 + 报告
```

设计沿用 [Aider](https://github.com/Aider-AI/aider) 的 architect / editor 两段式思路，差别在于：

- 产物不止是代码编辑，还有可归档的 `plan.md`（纲领）与 `report.md`（执行报告）；
- 面向"纲领性问题"而非仅编码任务；
- 自带图形界面与实时事件流，而不是纯终端交互。

## 快速开始

### Windows 一键启动（推荐）

在资源管理器里**双击**即可，无需改 PowerShell 执行策略：

| 文件 | 用途 |
|---|---|
| `start-demo.cmd` | 离线演示：同时启动假中转与编排器，自动打开浏览器，**不需要任何 Key** |
| `start.cmd` | 接真实中转：用 `.env` 或界面里保存的配置启动（已配置过就不会再让你填 `.env`） |
| `stop.cmd` | 停止（只结束本工具的进程：命令行里匹配 `app.main:app` 或 `scripts.fake_relay`） |

脚本要点：

- 自动切到 UTF-8（`chcp 65001` + `PYTHONUTF8=1`），**中文日志不会乱码**；
- 运行时**保持那个黑窗口开着**，它就是日志窗口；关掉窗口即停止服务；
- 服务地址固定为 <http://127.0.0.1:8787>，浏览器会在服务就绪后自动打开。

> 这些 `.cmd` 必须是 **CRLF** 换行，否则 cmd.exe 会出现"逐行吞掉前两个字符"的诡异报错。
> 仓库根目录的 `.gitattributes` 已声明 `*.cmd text eol=crlf`，git 检出时会自动保证。

> 演示模式会**忽略** `data/settings.json` 里保存过的真实中转配置（只读忽略，不删除），
> 保证离线一定跑得起来；要用自己的中转请走 `start.cmd`。
> 演示模式下即使点了"保存/测试连接"也**只在本进程生效，不会覆盖**你的真实配置。

### 界面交互自检

用 Chrome DevTools 协议发**真实鼠标点击**（能发现"被遮罩挡住点不到"这类问题）：

```powershell
node scripts/ui_check.mjs          # 64 项检查：可点击性、弹窗、配置切换、滚轮、快捷键、提交任务
node scripts/ui_check.mjs --only chat,caps   # 只跑对话与能力中心（约 70 秒，全量约 106 秒）
```

自检按模块分组（`--only run,chat,caps,settings`，不写就是全量）：改哪个模块就跑哪几组，
**发布前仍跑全量**；被跳过的组会打印 `SKIP` 并在结论里计数，不会假装跑过。
需要本机装有 Chrome 或 Edge；截图默认落在 `.logs/ui-light.png`。

### 命令行方式（Windows / PowerShell）

```powershell
cd orchestrator
.\scripts\run.ps1 -Demo -OpenBrowser   # 离线演示：自动起假中转并打开浏览器
.\scripts\run.ps1                      # 接真实中转（读取 .env 或界面设置）

# 若提示"禁止运行脚本"：
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Demo
```

也可以手动分两个终端启动，并用脚本自检整条链路：

```powershell
python -m scripts.fake_relay     # 终端 A：本地假中转（127.0.0.1:8799）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787   # 终端 B
python -m scripts.smoke_check
```

### 接真实中转

```powershell
Copy-Item .env.example .env      # 或用 start.cmd，它会自动帮你创建并打开
```

在 `.env` 里填写：

```ini
RELAY_BASE_URL=https://your-relay.example.com/v1
RELAY_API_KEY=sk-xxxxxxxx
ARCHITECT_MODEL=gpt-5
EDITOR_MODEL=deepseek-v4
```

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

也可以在界面右上角 ⚙ 中填写（界面保存的值优先于环境变量）。模型名以你中转侧的命名为准：
`deepseek-v4`、`deepseek-v4-flash`、`deepseek-chat` 我都见过，配置项就是为此而留。

填完后点设置里的**「测试连接」**：它会先保存当前填写内容，再用 `GET /models` 探一次（不消耗额度），
直接告诉你地址通不通、Key 有没有被接受、目标模型在不在网关的模型列表里。
设置写错时页面顶部不会显示"已就绪"，底部输入框也会直接提示是哪一处有问题。

Key 输入框的行为：**不会回显明文**（打开设置时是空的）；已保存的 Key 会在占位提示里显示掩码，
例如"已保存：sk-a\*\*\*\*\*\*3def（留空表示不修改）"。保存或测试连接**都不会清空你填的内容**，
结果会显示在下方状态行。想换 Key 就直接粘贴新的覆盖。

## 多套配置：中转 / 个人 Key 直连，随时切换

每套配置（Provider）包含：名称、类型、地址、Key、协议、架构段模型、执行段模型。
典型用法是存两套——一套公司/自建**中转**，一套**个人 Key 直连**官方——随时切换、互不覆盖。

两种切换方式：

- **侧栏底部下拉框**：选中即生效（当前项带 `●` 标记），下一次运行立刻用新配置；
- **设置弹窗**：`＋ 新建` / `复制` / `删除` / `设为当前` / `测试当前配置`。

类型只影响界面提示与地址预设：选"个人 Key 直连"会出现 OpenAI、DeepSeek、Moonshot、智谱等
官方地址快捷填充；选"中转网关"就填你自己的网关地址。运行逻辑完全一致（都是 OpenAI 兼容端点）。

### 两段分别用不同配置（架构走中转、执行走个人 Key）

**最快的方式：直接点顶栏右上角的「架构 … → 执行 …」标签**（或它右边的 ✎），
在弹窗里分别选两段用哪套配置、哪个模型，保存即生效——不用进设置翻。
设置里勾选**「分段使用不同配置」**是同一个开关的详细入口，效果一致：

```
架构段  公司中转   gpt-5.6-sol     →  https://your-relay.example.com/v1
执行段  个人直连   deepseek-chat   →  https://api.deepseek.com/v1
```

反过来（架构段走个人直连、执行段走中转）同样支持；模型留空表示"用该配置的默认模型"。

- 顶栏徽章显示两段各自的模型与地址；分段生效时侧栏显示 `分段：架构 <host> → 执行 <host>`。
- 弹窗里的「两段都跟随当前配置」= 一键清掉分段，回到"两段都用侧栏当前配置"。
- **侧栏下拉框切换配置 = 两段都用这套**（会清掉分段设置）；只想改一段，请用设置里的分段模式。
- 存储为 `routes: {architect: {provider_id, model}, editor: {provider_id, model}}`，
  空 `provider_id` 表示该段"跟随当前配置"。

存储：`data/settings.json` 保存为 `{version, active_provider_id, providers[], globals}`。
旧的扁平格式（直接写 `relay_base_url` 等）在读取时自动迁移成一条名为"默认配置"的配置，**不会丢配置**。
如果你只用 `.env` 配置，打开设置会看到一条"环境变量配置"，可直接编辑或另存为新配置。
演示模式（`start-demo.cmd`）下的新建/修改只在本进程生效，不会写进这个文件。

## 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_BASE_URL` | 空 | 中转地址。带不带 `/v1` 都会自动探测并记住可用路径 |
| `RELAY_API_KEY` | 空 | 中转密钥，**只保存在本机**（`.env` 或 `data/settings.json`，均已 gitignore） |
| `RELAY_WIRE_API` | `chat_completions` | 协议；若你的中转/上游走 Responses，用 `responses` |
| `ARCHITECT_MODEL` | `gpt-5` | 架构段模型 |
| `EDITOR_MODEL` | `deepseek-v4` | 执行段模型 |
| `ARCHITECT_*` / `EDITOR_*` | 空 | 两段走**不同**中转时分别填写，留空回落到 `RELAY_*` |
| `REQUEST_TIMEOUT_SECONDS` | `300` | 单次请求超时 |
| `MAX_PLAN_STEPS` | `8` | 纲领步骤上限（1~20） |
| `ALLOW_COMMAND_EXECUTION` | `false` | 是否允许自动执行模型给出的命令，**默认只展示** |
| `CONTEXT_TREE_LIMIT` | `120` | 喂给执行段的工作区文件条目上限 |

## 使用流程

1. **描述目标**（可选填写"落地目录"：留空则在运行目录内新建工作区，填写则直接改真实项目）。
2. 架构段流式产出纲领；界面右侧"纲领"页同步显示步骤与验收标准。
3. **确认门**：点「确认并开始执行」；或写下反馈点「按反馈重做纲领」让 GPT 修订。
4. 执行段逐步落地：每步显示摘要、文件改动（可展开 unified diff）、建议命令、
   **客观验收结果**（见下）与说明。
5. 结束后可查看 `plan.md` / `report.md`，并在"文件"页浏览工作区产物。

状态机：`planning → awaiting_approval → executing → done / failed / cancelled`。

### 问答不会走编排

先判断你这句话是"要把东西做出来"还是"只是问一句"：

- **问答 / 闲聊**（例如"你是哪个模型"）：架构段直接回答，不生成纲领、不建步骤、不碰工作区，
  界面上显示为「直接回答 · 未进入编排」；
- **需求**：照常走「纲领 → 确认 → 执行」。

判不准时它宁可**多跑一次编排**，也不会把你的需求当闲聊丢掉（分类失败按需求处理）。

### 每步都有客观验收，不靠模型自称"完成"

执行段说"已完成"不算数。每步执行完会**自动逐条检查**纲领声明的客观条件：

| 检查 | 判定 |
| --- | --- |
| `file_exists` / `dir_exists` | 交付物文件/目录真的存在 |
| `glob` | 通配至少匹配到一个文件 |
| `file_contains` | 文件里真的有指定内容 |
| `py_compile` | Python 文件能编译通过（只编译，不执行） |
| `py_import` | Python 文件能被导入（**会真的导入一次**；需要开启「允许执行验证命令」，未开启时降级为语法编译并写明"没有真正导入"；打包版找不到可用 Python 解释器时同样降级） |
| `json_valid` | 文件是合法 JSON |

**写代码类步骤会自动补一条「能编译」**：这一步实际写出来/改过的 `.py` 文件，系统自己加
`py_compile` 检查，并为它**预留名额**（不会被纲领声明的一堆检查挤掉）。真实事故就是这么发生的——
某个模块少写两个引号，8 个测试文件全部因 SyntaxError 收集失败，而当时的验收只查"文件里含指定文字"，
这一步照样被标成了完成。

纲领说这一步要产出 `docs/plan.md`，系统就会真的去看这个文件在不在——写错路径、
只写空壳、忘了写，都会被拦下来：该步变成 `blocked` 并列出**哪一条没过、为什么**，
你可以补充说明后**只重跑这一步**。界面上每步卡片会显示「客观验收 3/3 全部通过」或具体失败项；
没有可自动判定的检查项时会明确标注「未验证」，不会假装验过。

验收没过不会立刻判死：系统会先把失败项（连同文件里的真实片段）回灌给执行段，让它在**同一步**里
补齐，轮数由设置里的「失败后自动修正轮数」控制；补不上才变成 `blocked`。

### 验证命令：让它自己跑测试、自己按报错改

执行段可以把"能证明这一步做对了"的命令写进 `commands`。**默认只展示不执行**；
在设置里打开「允许执行验证命令」并填好**命令白名单**（每行一条前缀，例如
`python -m pytest`、`python -m ruff`、`node scripts/ui_check.mjs`）之后：

1. 命中的命令会在**工作区内**真的执行（不经过 shell，`| & ; > %` 等一律拒绝）；
2. 失败的命令会把**退出码 + 输出（头尾各 2000 字符）回灌给执行段**，让它在**同一步**里继续修；
3. 自动修正的轮数由设置里的「失败后自动修正轮数」控制（默认 2 轮，0 = 不自动修）；
4. 轮数用尽仍未通过 → 该步 `blocked` 并列出是哪条命令、退出码多少，**不会**标成完成。

步骤卡片会显示每一条实际执行过的命令（✓/✗、退出码、耗时），点一下可以展开输出。
不在白名单里的命令仍然只是建议——这一点和以前一样。

### 改错了可以一键回滚（git 安全网）

每一步开始前会记录 git 锚点（HEAD/分支）。步骤卡片上有「回滚这一步」：
它只还原**这一步碰过的文件**——原本存在的用 git 还原，这一步新建的删掉，
**其他未提交改动一律不碰**；回滚后该步退回"待执行"，可以从这一步重跑。
非 git 仓库时仍可回滚状态，文件另有 `backup/` 目录兜底。

### 跑完之后可以继续对话（多轮续聊）

运行结束后（完成 / 阻塞 / 失败 / 暂停），时间线底部会出现「继续说下一步」输入框：

```
把这个改动跑一遍测试，没过就修到过
再补一份验收清单
```

提交后架构段会**只规划新增步骤**，仍然要你确认一次；确认后**只执行新增的部分**，
已完成的步骤、它们改过的文件和事件序号都不受影响。这样就能形成"跑一轮 → 看结果 →
接着说 → 再跑一轮"的迭代，而不必每次重开任务。

### 重新打包并重启（改自己的源码后让它生效）

命令面板（`Ctrl+K`）→「重新打包并重启」：先重新打包（含 pytest / ruff），
再结束当前程序并启动新版本。实现上由外部辅助脚本执行（见
`scripts/restart.ps1`）——因为 Windows 会锁住正在运行的 exe，而且"杀掉自己"之后的
代码不会继续执行。重启日志在 `data/logs/restart.log`；
正在跑的运行会被新实例标记为「已暂停」，可在界面点「继续执行」接着跑。

### 被阻塞时可以续跑（不用从头再来）

执行段发现信息不足（例如需求说"盘点**现有**模块"，但运行时用的是全新空工作区）会**主动停下**，
状态变成 `blocked` 而不是"失败"，并在时间线上给出恢复卡片：

- **补充信息并继续**：填上缺的内容（入口文件、约定、约束…），可选填**要改动的现有目录**；
  系统只重跑被阻塞的那一步，已完成的步骤不会重做；
- **按这些信息重做纲领**：如果补充的信息会改变拆分方式，直接带着它重新规划。

另外两处防呆：

- 架构段现在明确知道"本次是全新空工作区"，不会再设计"盘点现有实现"这种执行段根本做不到的步骤
  （会改成"从零新建"，或把问题放进 `open_questions` 先问你）；
- 提交任务时，如果文案里出现"现有 / 仓库 / 重构 / 改造 / 盘点"等字样而**落地目录为空**，
  会先弹一次确认，避免白跑一轮。

键盘操作：`Enter` 发送、`Shift+Enter` 换行；`Esc` 关闭设置弹窗；运行中任务输入框仍可选中/复制/粘贴，
只有"提交新任务"会被拦下并提示先结束当前任务。

侧栏底部显示版本号与构建号（例如 `v0.1.0 · 构建 72cc85cf`）：前端资源带版本查询串，
改动后普通刷新即可生效；构建号变了就说明浏览器已经加载到新代码。

## 安全边界

执行段是"会写盘"的一段，因此做了这些约束：

- **路径防护**：只接受工作区内的相对路径，拒绝绝对路径、盘符、`..` 穿越与 `.git` 等版本控制目录；
- **改动前备份**：覆盖/删除前把原件复制到 `data/runs/<run>/backup/`，可人工回溯；
- **增量替换优先**：`search/replace` 片段必须全部命中才写入，避免半成品文件；
- **命令不自动执行**：模型给出的命令默认只展示；确需执行再打开 `ALLOW_COMMAND_EXECUTION`；
- **密钥不出本机**：界面只显示掩码，任何接口都不回传明文 Key。

## API

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/v1/health` | 健康检查（含端点是否配置完整） |
| `GET`/`PUT` | `/api/v1/settings` | 读取/更新配置（读取时密钥掩码） |
| `GET`/`POST` | `/api/v1/runs` | 列出运行 / 创建运行（创建后自动进入架构段） |
| `GET` | `/api/v1/runs/{id}` | 运行详情（含 `event_seq`，供增量订阅） |
| `POST` | `/api/v1/runs/{id}/approve` | 空 feedback = 开始执行；有 feedback = 重做纲领 |
| `POST` | `/api/v1/runs/{id}/cancel` | 取消 |
| `GET` | `/api/v1/runs/{id}/events` | SSE 事件流（`?since=<seq>` 增量续传） |
| `GET` | `/api/v1/runs/{id}/tree`、`/file?path=` | 浏览工作区 |
| `GET` | `/api/v1/runs/{id}/docs` | 读取 `plan.md` / `report.md` |
| `GET` | `/api/v1/runs/{id}/metrics` | 运行指标：架构段一条 + 执行段每步一条 + 运行级汇总（token 未知时为 `null`） |
| `GET` | `/api/v1/providers` | 列出全部配置 + 当前配置 + 内置地址预设 |

项目与普通对话（真实数据，不再是界面壳子）：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/v1/projects` | 项目列表（含每次运行数 / 步数 / 文件改动 / 最近一次运行） |
| `POST` | `/api/v1/projects` | 新建项目（`name`、可选 `project_id`、`root_path`、`description`）；`root_path` 留空则分配 `data/projects/<id>/workspace` |
| `GET` / `PUT` / `DELETE` | `/api/v1/projects/{id}` | 读取 / 改名换目录 / 归档（归档不删数据） |
| `GET` | `/api/v1/projects/{id}/modules/{module}` | 项目内模块真实数据（`overview`/`architecture`/`plan`/`execution`/`verification`/`logs`/`settings`；旧名 `steps`/`verify` 自动别名） |
| `GET` | `/api/v1/runs?project_id=&kind=&context_type=` | 运行列表按项目与上下文过滤（默认只列项目运行） |
| `GET` | `/api/v1/chats` | 普通对话会话列表（标题 / 消息数 / 摘要） |
| `POST` | `/api/v1/chats` | 新建会话（可带第一句话） |
| `GET` / `DELETE` | `/api/v1/chats/{id}` | 读取会话（含全部消息）/ 删除会话 |
| `POST` | `/api/v1/chats/{id}/messages` | 发一条消息（多轮带上下文，回答流式返回） |
| `GET` | `/api/v1/chats/{id}/events` | 普通对话的 SSE 事件流 |
| `POST` | `/api/v1/providers` | 新建配置（`activate:false` = 先不切换） |
| `PUT` / `DELETE` | `/api/v1/providers/{id}` | 修改 / 删除配置 |
| `POST` | `/api/v1/providers/{id}/activate` | 切换为当前配置 |
| `POST` | `/api/v1/providers/{id}/test` | 测试指定配置（不切换） |
| `GET`/`PUT` | `/api/v1/routes` | 读取 / 设置分段路由（两段各用哪套配置、哪个模型） |

`PUT /api/v1/settings` 会做**完整校验**：`RELAY_BASE_URL` 必须是 `http://` 或 `https://` 开头的完整地址，
非法值直接返回 400，且**不会写坏已有配置**（先校验后落盘）。密钥留空表示"不修改"。
Key 会自动去掉粘贴时带上的引号、空白与 `Bearer ` 前缀；若检测到 Key 栏里填的是地址（`http://…`），
保存会被拒绝并提示该填在哪一栏。`POST /api/v1/settings/test` 提供上述连通性自检。

## 排障

| 现象 | 原因与处理 |
|---|---|
| `HTTP 401 … Invalid token` | Key 无效。最常见的是把**中转地址**粘进了 Key 栏（Key 通常形如 `sk-…`）。点「测试连接」立刻能确认，改好后重新保存 |
| `中转地址格式不正确` / 未就绪 | base_url 必须是完整的 `http(s)://` 地址，可带或不带 `/v1`（工具会自动探测） |
| `404 路径不存在` | base_url 少了 `/v1`，或该网关只提供 `chat/completions`；测试连接会给出具体路径 |
| `HTTP 502 bad_response_status_code` | 网关回源失败（Cloudflare 常见），属上游临时故障：流式与非流式都会**自动重试 3 次**；仍失败可稍后再试或切换配置 |
| 运行很慢或提示不支持 `stream` | 部分网关不支持流式，工具会自动退回一次性请求，功能不受影响 |
| 页面点了没反应 / 快捷键失效 | 先 `Ctrl+F5` 强刷一次；侧栏底部构建号应与最新一致 |
| 输入框/发送按钮跑到屏幕外 | 已修复：三栏容器补了 `min-height: 0` 约束，运行记录再多也不会把整页撑高 |
| 滚轮滚不动 / 内容被裁掉 | 已修复：滚动面板里的卡片曾被 flex 压扁（`flex-shrink` 默认为 1，卡片又是 `overflow:hidden`），已加 `flex-shrink: 0`；自检脚本含滚轮用例 |

界面深链：`http://127.0.0.1:8787/?run=<run_id>`；加 `&stream=0` 只渲染快照、不订阅事件流（静态截图/受限环境用）。

## 目录结构

```text
orchestrator/
├── app/
│   ├── core/          # 配置、错误、中转客户端（含协议/URL/参数自动降级）、JSON 容错解析
│   ├── schemas/       # 纲领、步骤产出、运行记录
│   ├── services/      # 工作区落地、事件总线、持久化、架构段、执行段、编排器
│   ├── api/routes.py  # HTTP 接口
│   └── main.py        # 入口（静态界面 + 全局错误处理）
├── web/               # 界面：index.html / styles.css / js/（12 个普通脚本，无构建步骤）
├── scripts/           # fake_relay（离线假中转）、smoke_check（端到端自检）、run.ps1
├── tests/             # pytest：中转协议、解析容错、路径防护、客观验收、端到端流程
└── data/              # 运行记录与工作区（已 gitignore）
```

## 测试与质量

```powershell
cd orchestrator
python -m pytest -q             # 387 passed
python -m ruff check .          # All checks passed
python -m ruff format --check . # 95 files already formatted
```

测试全部走本地假中转，不消耗真实额度，因此**不需要 Key 就能跑**。

## 长任务的上下文策略（省 token）

模型自己压缩上下文既贵又不可控，所以**压缩由编排器做**：确定性裁剪 + 按需取件。

| 层 | 内容 | 处理方式 |
|---|---|---|
| 稳定段 | 系统提示 → 总需求 → 纲领摘要 → 工作区文件树 | 放在最前，便于命中网关**前缀缓存** |
| 当前段 | 当前步骤（标题 / 目标 / 交付物 / 验收） | **永不裁剪** |
| 交接段 | 已完成步骤的接力说明 | 每步只留 ≤200 字 `handoff`；超长时把最早几步折成一行 |
| 文件段 | 相关文件内容 | 单文件超限取头尾；总预算不够时**优先丢它**，并提示可用 `need_files` 索取 |

- **纲领不再整份下发**：只发"目标 + 思路 + 原则 + 组件名 + 其他步骤标题"，比完整 JSON 小一半以上；
- **执行段可按需索取文件**：上下文里没有（或被省略）的文件，模型只输出
  `{"need_files": ["src/app.py"], "need_reason": "..."}`，系统补齐后**同一轮继续**，最多 2 轮、每轮 ≤6 个；
- **系统提示明确禁止复述/总结上下文**，避免"压缩"变成额外开销；
- 每步实际发送的字符数记录在 `step.context_chars` 并显示在步骤卡片上（如 `上下文 5.2k 字符`），
  按需读取过的文件也会列出来。

| 变量 | 默认 | 说明 |
|---|---|---|
| `CONTEXT_BUDGET_CHARS` | 24000 | 单步上下文字符预算 |
| `FILE_CONTEXT_MAX_CHARS` | 6000 | 单文件注入上限（超出取头尾） |
| `TREE_CONTEXT_MAX_CHARS` | 2000 | 文件树字符上限 |
| `COMPLETED_LOG_MAX_CHARS` | 1200 | 已完成步骤交接日志上限 |
| `STEP_FETCH_ROUNDS` | 2 | "按需索取文件"最大轮次 |
| `STEP_FILE_FETCH_LIMIT` | 6 | 每轮最多取回文件数 |

普通对话的长上下文管理（设置 → 行为与上下文；机制见 `docs/chat-context-window.md`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CHAT_CONTEXT_ENABLED` | `true` | 总开关；关闭 = 历史全部原样发送（旧行为，最贵） |
| `CHAT_WINDOW_TURNS` | `12` | 保留原文的最近轮数 |
| `CHAT_WINDOW_CHARS` | `6000` | 最近原文的字符上限 |
| `CHAT_FOLD_BATCH` | `4` | 每次折叠几轮（批量折叠更省调用） |
| `CHAT_SUMMARY_MAX_CHARS` | `2000` | 承接摘要超过该字数就自动开新对话（下限 200） |
| `CHAT_AUTO_SPLIT` | `true` | 关掉则只折叠并在界面提示，不切会话 |

经验值：把 `CONTEXT_BUDGET_CHARS` 压到 12000 左右通常仍能保持质量且明显省 token；
任务涉及大量既有代码时，优先靠 `need_files` 取件，而不是把预算调大。

## 插件市场（参照 anywhere-labs/dsh-desktop 的目录契约）

> **演进中**：插件市场是历史形态，新的方向是**能力中心**——skill / MCP / 插件统一管理。
> P0 已落地统一能力层（`data/capabilities/`、`GET /api/v1/capabilities`、侧栏「能力中心」入口与审计），
> 已装插件会以 `plugin` 形态镜像进来；skill 安装（P1）与 MCP 工具（P2）随后接上。
> 架构与路线见 `docs/ARCHITECTURE.md`。

## 能力中心（skill / MCP / 插件）

侧栏底部「✦ 能力中心」统一管理三类能力：

**Skills（已可用）**：兼容市面常见格式——目录里有 `SKILL.md` 即可，frontmatter 写 `name` 与
`description`（可选 `version`、`triggers`），可带 `scripts/`（**只登记，默认不执行**）、
`references/`、`assets/`。三种来源：

```
本地目录        D:\skills\my-skill
GitHub          owner/repo#main/skills/my-skill     （不带 #ref 用默认分支）
zip 地址        https://example.com/my-skill.zip
```

装进来的是一份**副本**（`data/capabilities/skills/<id>/`），源目录之后可以随便动。
执行段每一步会按 `triggers` 匹配当前步骤文本，**命中才注入**（最多 2 个、总量 ≤2400 字符），
步骤卡片上会写「本步注入的技能：xxx」；匹配不到就不带——不白烧上下文。
技能可以设为**全局启用**或**只在某个项目启用**（项目之间互不影响）；所有安装 / 启用 / 卸载都有审计。

仓库自带一个示例技能 `skills/code-review/`（含一个示例脚本，用来验证"脚本只登记不执行"）。

**MCP（已可用）**：把外部工具服务器接进来——本地进程（stdio）或服务地址（Streamable HTTP）。

```
能力中心 → MCP → 选预设（filesystem / git / fetch / sqlite / memory / time /
playwright / sequential-thinking / 示例 MCP）→ 添加 → 列出工具 → 确认信任
```

三条边界，缺一不可：

1. **默认不启用**：添加后是关闭状态，要去能力中心打开；
2. **首次确认信任**：MCP 服务器是**会以本机权限跑代码**的进程，确认前模型调用会被明确拒绝
   （拒绝原因如实回灌给执行段，不假装执行过）；
3. **作用域**：可以全局启用，也可以只在某个项目里生效。

模型侧走**应用层协议**（不依赖网关是否支持 `tools` 字段）：执行段在需要时输出
`{"tool_calls": [{"capability_id": "mcp.xxx", "tool": "…", "arguments": {…}}]}`，
应用校验并执行后把结果回灌，让它在**同一步**继续。可用工具清单（名字 + 一句说明）会随
上下文下发，命中"已启用 + 已确认信任"的服务器才会出现；设置里可整体关掉
（`MCP_ENABLED`），也能限定"一步最多几个调用、最多回灌几轮"。

仓库自带 `scripts/demo_mcp_server.py`（零依赖、离线可跑），「示例 MCP」预设就指向它，
用来验证整条链路。

左侧任务栏底部的 **插件市场** 入口（`market`）打开市场面板，分三页：

| 页 | 作用 |
|---|---|
| 市场 | 搜索插件、看能力声明与占用的槽位、一键安装 |
| 已装插件 | 启用 / 禁用 / 卸载；禁用后它在侧栏的入口立即消失 |
| 来源 | 添加/删除目录来源。只接受 **HTTPS 清单地址**，本地为每条来源生成独立 id |

目录契约对齐 `dsh-community-market`：

* 清单（`manifestVersion: 1.0.0`）必须声明 `providerId`、`attribution`、`transport{kind:"https-json",endpoint}`、`query`；
* 条目归一化后带 `provenance{source_record_id, provider_id, item_id}`，界面能显示"这条来自哪个源"；
* 文本统一清洗：去掉控制字符与双向控制符（`\u202A-\u202E`、`\u2066-\u2069`），图标只保留不透明引用（`mktimg_…`）；
* 条目的图标/仓库地址等字段原样保留，但**不会**被宿主当成本地路径使用。

**安全边界**：本工具当前只安装**声明式插件**——登记能力声明与界面入口，不下载、不执行任何第三方代码。
声明的能力必须在宿主白名单内（`ui.slot` / `provider.register` / `step.hook` / `command.run` / `storage.local`），
占用的槽位也必须存在（`sidebar.footer.action` / `sidebar.settings` / `inspector.panel`），否则安装直接被拒绝。
`command.run` 这类能力目前**只记录声明**，永远不会被自动执行。

接口：`GET /api/v1/market/capabilities`、`GET|POST /api/v1/market/sources`、
`DELETE /api/v1/market/sources/{id}`、`GET /api/v1/market/items`、
`GET|POST /api/v1/plugins`、`POST /api/v1/plugins/{id}/enable|disable`、`DELETE /api/v1/plugins/{id}`。

## 左侧任务栏

布局与锚点参照 dsh-desktop 的 sidebar 契约（便于测试与插件挂载）：

```
品牌 / 折叠按钮
＋ 新任务
运行记录（可滚动）
footArea
├── sidebar-route（当前配置 · 地址）
├── quick-switch（切换中转 / 个人 Key 直连）
├── footer-actions   ← 槽位 data-slot="sidebar.footer.action"
│     capabilities（能力中心） / git（Git） / updates（版本） + 插件贡献的入口
└── settings-area    ← 槽位 data-slot="sidebar.settings" → settings
```

- 每个入口带稳定锚点：`data-entry="capabilities|git|updates"`、`data-seat="settings"`，插件入口用 `data-entry="<plugin-id>"`。
- **插件市场与已装插件已并进「能力中心」**（插件分页里有「打开插件市场（legacy）」入口）：
  侧栏底部不再为历史形态单独占两个按钮。
- **可折叠成图标栏**：点折叠按钮后 `#sidebar[data-wide="false"]`，宽度 60px，只留图标与运行状态点；
  折叠态**保留展开按钮**（否则就成了单向门）；状态记忆在 localStorage。
- 位置调整：设置入口从顶栏移到底部（与 dsh 一致），顶栏只保留标题、路由徽章。

## 导航与项目边界（一级入口只有两个）

一级导航只表达"用户要做什么"：**普通对话** 与 **项目**。
架构 / 计划 / 执行 / 验证 / 日志 / 设置 不再是一级入口，而是**项目内部的二级模块**
（普通对话上下文里访问不到它们，接口会明确拒绝，而不是静默落到默认项目）。

### 这两块现在是真功能，不是壳子

| 入口 | 真实行为 | 数据 |
| --- | --- | --- |
| 普通对话 | 新建会话 → 发消息 → 模型流式回答；**多轮带上下文**（接着说不用重复前文）；可搜索、可切换、可删除 | 会话存在 `data/runs/<id>/run.json`（`context_type=chat`），消息落库，刷新后还在 |
| 项目 | 新建项目（名称 + 工作区目录）→ 项目列表 → 打开项目 → 七个二级模块 | 项目存 `data/projects.json`（名称 / 状态 / 工作区绑定 / 最近活动） |
| 项目内模块 | **概览**（计数 + 最近运行）、**架构**（纲领目标 / 原则 / 组件 / 风险 + 原始输出）、**计划**（步骤与验收）、**执行**（运行时间线，可确认 / 停止 / 重试 / 回滚）、**验证**（逐步客观验收结果）、**日志**（消息与命令）、**设置**（改名 / 换工作区 / 归档） | `GET /api/v1/projects/{id}/modules/{module}` 现场装配，只含本项目数据 |

关键约束：**运行属于项目**。项目里的运行按 `project_id` 过滤，项目和项目之间不串数据；
普通对话**不产生纲领与步骤、不碰工作区、不能进执行流程**（接口返回 `not_a_project_run`），
反向也成立——项目里的运行不能当对话用（`not_a_chat_session`）。
项目与工作区一一绑定：没填"落地目录"时，项目内的运行直接落在项目的工作区里。

**普通对话越聊越贵的问题已经处理**：默认只把最近 12 轮 / 6000 字符的原文发出去，
更早的历史增量折进一段 ≤300 字的**承接摘要**；摘要本身超过 2000 字时**自动开新对话承接**
（旧对话保留、可互相跳转，事件流里会提示）。线程顶部有一张「长上下文管理」卡片，
显示「已折叠 n 轮 · 摘要 n 字 · 上一轮发送 n 字符」，摘要可展开查看。
开关与全部阈值都在 **设置 → 行为与上下文**，关掉即回到"全量历史"的旧行为；
项目里的运行不受影响（执行段始终是确定性裁剪）。细节见 `docs/chat-context-window.md`。

**项目模块里的内容都按 Codex 那套折叠**：已经输出完的默认折起来、只留一行摘要
（状态 · 耗时 · token · 验收 n/n），正在跑的一步保持展开，点标题行**原地**展开/收起，
不会把你甩到别的模块去。

- **概览**：运行记录每条可原地展开（任务描述 / 运行 ID / 错误 + 「去执行看时间线 →」按钮），
  想看细节不必先跳走；
- **架构**：纲领目标 / 设计原则 / 组件与职责 / 风险 / 待澄清 / 架构段原始输出，全部默认折叠，
  折叠时标题行就是那一段的一句话摘要；
- **计划**：每个步骤默认折叠，标题行显示「状态 · 交付物 n 个 · 验收 n 条」；
- **执行**：架构段原始输出在流式生成时展开（看得见输出），出完纲领自动折起来写「已解析为纲领」；
  步骤卡片沿用「已完成折叠、运行中展开」；
- 展开状态按内容记在内存里，数据刷新不会把你的展开收起。

地址栏也可以直接进：`#/chat`、`#/chat/<会话ID>`、`#/projects`、`#/projects/<项目ID>/<模块>`；
旧的 `#/runs/<运行ID>`、`#/settings`、`#/plugins` 会自动重定向。

约定与迁移：

* 项目用稳定 ID `project_id` 标识；前缀只出现在 `context_id`（`project:<projectId>` / `chat:<sessionId>`）。
* 项目与工作区一一绑定，**项目之间不共享根目录与运行数据**。
* 缺项目定位时不静默落到默认项目，而是返回「需要选择项目」引导；
  旧链接按「保留 / 重定向 / 需要选项目 / 已下线（410）」四种结论处理。

细节见 `docs\project-context-contract.md`、`docs\project-navigation-contract.md`、
`docs\navigation-migration-notes.md`；接口在 `GET /api/v1/navigation/sidebar`、
`GET /api/v1/navigation/project-modules/{module}`、`GET /api/v1/projects/{id}/modules/{module}`。

## 打包成 EXE（Windows）

### 桌面客户端（默认形态，不是浏览器页面）

双击 `dist\Orchestrator.exe`（或 `start-client.cmd`）= 打开**原生应用窗口**：

* 用 Windows 自带的 **WebView2** 渲染界面，没有地址栏、没有标签页，就是一个独立窗口；
* 后端（FastAPI/uvicorn）跑在窗口进程内的后台线程，只监听 `127.0.0.1`，端口自动挑空闲的；
* 关掉窗口即退出程序；日志写到 `data\logs\orchestrator.log`（无控制台窗口）；
* 需要排查启动问题时用 `-Console` 参数打包，会保留控制台窗口。

```powershell
.\dist\Orchestrator.exe                 # 桌面客户端（默认）
.\dist\Orchestrator.exe --selftest 6    # 自检：开窗 → 校验后端与界面渲染 → 自动关窗
.\dist\Orchestrator.exe --debug         # 带开发者工具
.\dist\Orchestrator.exe --server        # 退回"服务器 + 浏览器"形态
.\dist\Orchestrator.exe --port 9000     # 固定端口（默认自动选空闲端口）
```

自检会依次验证三件事并写进日志：后端健康检查、窗口对象、**窗口内界面是否真的渲染出来**（通过窗口内 DOM 查询，不是只看窗口存在）。

数据目录与源码版**共用一份**（`D:\orchestrator\data`）：`start-client.cmd` 已经帮你设好
`ORCHESTRATOR_DATA_DIR`，所以配置、运行记录、已装插件在两种形态下是同一套。
若直接双击 `dist\Orchestrator.exe`，它会用 EXE 同级的 `dist\data\`（两份数据互不影响）。

```powershell
cd orchestrator
.\scripts\package.ps1              # 单文件桌面客户端（约 21 MB，无控制台）
.\scripts\package.ps1 -OneDir      # 目录版：dist\Orchestrator\（启动更快）
.\scripts\package.ps1 -Console     # 保留控制台窗口，便于排查启动问题
.\scripts\package.ps1 -SkipTests   # 跳过打包前的 pytest + ruff（默认会先跑一遍）
```

打包前脚本会先跑 **测试 + ruff**，不通过就中止；并且会**先结束正在运行的 Orchestrator 进程**
（Windows 上被占用的 exe 无法覆盖，会报 WinError 5）。

**用起来**：双击 `dist\Orchestrator.exe`（自动打开浏览器），或命令行带配置启动：

```powershell
$env:RELAY_BASE_URL='https://your-relay.example.com/v1'
$env:RELAY_API_KEY='sk-xxxxxxxx'
$env:ARCHITECT_MODEL='gpt-5.6-sol'; $env:EDITOR_MODEL='deepseek-v4-flash'
.\dist\Orchestrator.exe
```

**数据放在哪**（打包版与源码版共用一套逻辑）：

| 情况 | 位置 |
|---|---|
| 默认 | EXE 同级的 `data\`（便携：拷走整个文件夹就带走配置与记录） |
| EXE 所在目录不可写（如装在 `Program Files`） | `%LOCALAPPDATA%\Orchestrator\data` |
| 想自己指定 | 环境变量 `ORCHESTRATOR_DATA_DIR` |

`data\settings.json`（多套配置与路由）、`data\runs\`（运行记录与工作区）、`data\plugins\`（已装插件与市场来源）都在这里面；
**绝不会**写到 PyInstaller 的临时解压目录（`_MEIxxxx`）——那种目录退出即删，记录会丢。

其它打包细节：

- `packaging/orchestrator.spec` 负责把 `web/` 与 `.env.example` 一起打进去，并补齐 uvicorn / httpx / anyio 的动态导入；
- 启动时直接把 app 对象交给 uvicorn（不用 `"app.main:app"` 字符串），避免冻结环境下按模块名重新导入；
- 打包版会自动把控制台代码页切到 UTF-8，中文日志不乱码；
- `packaging/version_info.txt` 提供 exe 的版本信息（右键属性可见）；想加图标就放一个 `.ico` 并在 spec 里加 `icon=`。

## 界面交互与快捷操作

### 为什么以前会"点了没反应"

定位到三类真实原因，都已修复（并且现在**任何前端异常都会上报**，见下）：

1. **遮罩吃掉点击**：弹窗打开时，半透明遮罩覆盖全屏；如果没意识到弹窗还开着，后面所有点击都落在遮罩上。
   现在**点遮罩即可关闭**（设置、市场、命令面板、确认框都支持），Esc 也能关掉最上层。
2. **过期响应覆盖新状态**：打开面板时发出的加载请求若晚于"安装/切换"等操作返回，会把界面刷回旧快照
   （表现就是"我刚点的操作没生效"）。现在所有列表加载都带请求序号，旧响应直接丢弃。
3. **一个脚本异常拖垮整页**：早期 `boot()` 之前若有异常，后面所有事件绑定都不会执行。
   现在入口有 try/catch，事件绑定走统一的 `on()` 帮助函数，缺元素或处理函数抛错都会**记录并提示**，而不是静默失效。

另外，确认类弹窗（删除配置、卸载插件、"要改现有代码吗"）全部改成**页内对话框**，
不再依赖 WebView 原生的 `confirm`（部分 WebView 里它可能不返回，导致整条点击链路卡住）。

### 为什么长任务会把界面卡死（已修）

流式输出曾经是**每个 token 写一次 DOM**：`textContent = 完整缓冲区` 之后紧跟
`scrollTop = scrollHeight`，而这两个属性会强制浏览器同步重排——文档越大越贵（实测平均
14.5 毫秒/次），单个 token 就吃掉一整帧。断线重连按 `since` 重放、上游积压后一次性送达时，
这些事件会挤在同一瞬间，界面被整段占死。

实测（注入 3000 个 token）：修复前主线程被占住 **43.6 秒**，修复后 **2 毫秒**；
4 秒快速流里运行列表的 DOM 变更从 **177822 个节点** 降到 **0**。

现在的做法：流式文本**按帧合并写入**（同一帧多个 token 只写一次）、`scheduleRender`
**120 毫秒合并**、运行列表与时间线用**数据签名缓存**（数据没变就不碰 DOM）。

### 执行中的时间线：已完成的步骤默认折叠

长任务跑起来后，时间线不再越堆越长：**已完成的步骤只留标题那一行**
（状态 · 耗时 · token · 验收 n/n），明细（文件改动、验证命令、客观验收、说明）默认收起；
点标题行即可展开 / 再收起，你的选择会按「运行 + 步骤」记住，重跑这一步（done → running）
会自动重新展开。运行中、待执行、被阻塞、失败的步骤**一律保持展开**——需要你看的东西不该被藏起来。

### 前端错误可追溯

界面/桌面窗口里的异常与 Promise 拒绝会自动上报到 `POST /api/v1/client-log`，
后端保留最近 200 条并写入日志；排查时直接：

```powershell
curl http://127.0.0.1:8787/api/v1/client-log
```

### 快捷键与 Codex 式操作

| 操作 | 说明 |
|---|---|
| `Ctrl+K` | 命令面板：新建任务、打开插件市场、切换配置、跳转任务、跳转面板、确认执行…（支持搜索与 ↑↓ 选择） |
| `Ctrl+/` | 快捷键说明 |
| `Enter` | 发送任务（`Shift+Enter` 换行；`Ctrl+Enter` 同样可用） |
| `Esc` | 关闭最上层弹窗 |

任务列表（左侧）也按 Codex 的方式补齐了管理能力：**搜索框**、**显示/隐藏已归档**（▣），
以及任务项悬停时的 **★ 置顶 / ✎ 重命名 / ▣ 归档**。卡片右侧新增**一键复制**（纲领、步骤摘要与文件清单）。

## 已知边界

- 执行段一次只做一步：步骤过大时建议在确认门处让 GPT 拆分；
- 命令执行默认关闭，且目前**没有沙箱**：只按白名单前缀放行、拒绝 shell 字符、
  限定在工作区内跑、有超时与输出截断；`rm`、`git push` 这类命令永远不在默认白名单里；
- "让它改自己"还差两块：多轮续聊（运行结束后继续迭代）与打包重启（见
  `docs/self-dev-design.md` 的 P3/P4）；
- 纲领质量门（步骤粒度、依赖关系、验收是否可判定）尚未实现——纲领本身仍可能拆得不好；
- 主备配置自动切换（`app/core/fallback.py`）已接到主链路：主用网关 502/503 时自动切到备用配置，
  并在指标里留下实际使用的配置；单次调用内另有流式重试、参数降级、强制 JSON 重试；
- 工作区文件树只用于给模型提供上下文，超大仓库建议填写更聚焦的落地目录；
- 运行记录保存在 `data/runs/`，是本地单机模型，没有多用户与并发隔离设计。
- 想清空历史记录：停掉服务后直接删除 `data/runs/` 目录即可（界面里的列表随之清空）。
