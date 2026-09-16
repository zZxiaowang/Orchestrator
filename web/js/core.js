/* 状态 / DOM 引用 / h() / request() / api / 事件绑定助手（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
/* 编排器前端：Codex 风格的三栏工作台。
   数据来源：REST 拉取运行状态 + SSE 实时事件流。
   渲染策略：结构性事件触发整体重排；流式 token 追加到缓冲区，
   因此在执行中重排也不会丢字。 */

const STORAGE_KEY = "orchestrator.lastRun";
const SIDEBAR_KEY = "orchestrator.sidebarWide";
const PANEL_KEY = "orchestrator.panelOpen";

/* ── 上下文模型：左侧栏只有两个一级入口 ──
   普通对话（chat）：轻量工作区，没有项目生命周期，不产生运行、计划与步骤；
   项目（project）：承载架构、执行、计划、步骤、验证与项目事件。
   旧数据（后端字段或 localStorage）缺少合法上下文类型时，一律按 project 归属。 */
const CONTEXT_CHAT = "chat";
const CONTEXT_PROJECT = "project";
const CONTEXT_TYPES = [CONTEXT_CHAT, CONTEXT_PROJECT];
const DEFAULT_CONTEXT_TYPE = CONTEXT_PROJECT;

//: 项目内的二级导航能力，普通对话下不渲染
//: 项目内二级模块的**唯一来源是后端**（index.html 里由 __PROJECT_MODULES_JSON__ 注入）。
//: 这里只在注入缺失时（例如直接打开静态文件）用一份兜底清单，保证页面不空白。
const PROJECT_MODULE_DEFS = (() => {
  const injected = window.__ORCHESTRATOR_PROJECT_MODULES__;
  if (Array.isArray(injected) && injected.length) return injected;
  return [
    { id: "overview", label: "概览" },
    { id: "architecture", label: "架构" },
    { id: "plan", label: "计划" },
    { id: "execution", label: "执行" },
    { id: "verification", label: "验证" },
    { id: "logs", label: "日志" },
    { id: "settings", label: "设置" },
  ];
})();

const PROJECT_SECTIONS = PROJECT_MODULE_DEFS.map((item) => item.id);

//: 上下文类型归一化：非法值 / 缺失值一律回落到默认归属
function normalizeContextType(value) {
  return CONTEXT_TYPES.includes(value) ? value : DEFAULT_CONTEXT_TYPE;
}

function contextIdPrefix(contextType) {
  return `${normalizeContextType(contextType)}:`;
}

//: 仅项目上下文可派发的动作；普通对话里点击一律不发出请求
const PROJECT_ONLY_ACTIONS = new Set([
  "architect",
  "execute",
  "run-step",
  "verify",
  "restart",
  "cancel",
]);

//: 统计看板的刷新入口：宿主元素在「统计」标签里按需创建，所以不能只在加载时抓一次
let dashboardRefresh = null;

//: 运行列表的签名：数据没变就不重建 DOM（否则一次运行会产生上千次 DOM 变更）
let lastRunListSignature = "";
//: 时间线的签名：结构没变就不重建卡片
let lastTimelineSignature = "";
//: 流式文本的合并写入（每个动画帧最多写一次，避免逐块触发重排）
let streamFrame = null;
const pendingStreams = new Set();

/* ── 前端错误留证：点不动的问题必须能在后端查到 ── */

function reportClientError(kind, error, context = {}) {
  const message = String((error && error.message) || error || kind);
  const stack = String((error && error.stack) || "");
  try {
    console.error("[client]", kind, message, context);
  } catch (error) {
    /* 忽略 */
  }
  try {
    showToast(`前端错误：${message.slice(0, 140)}`);
  } catch (error) {
    /* showToast 可能尚未就绪 */
  }
  try {
    const payload = { level: kind, message, stack, url: location.href, context };
    const body = JSON.stringify(payload);
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/v1/client-log", new Blob([body], { type: "application/json" }));
    } else {
      void fetch("/api/v1/client-log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      }).catch(() => {});
    }
  } catch (error) {
    /* 上报失败就算了，不能再抛 */
  }
}

window.addEventListener("error", (event) =>
  reportClientError("error", event.error || event.message)
);
window.addEventListener("unhandledrejection", (event) =>
  reportClientError("rejection", event.reason)
);

const state = {
  settings: null,
  runs: [],
  run: null,
  es: null,
  reconnectTimer: null,
  lastSeq: 0,
  buffers: {}, // key -> 流式文本缓冲区
  //: 普通对话列表的搜索词（服务端过滤）
  chatQuery: "",
  //: 步骤卡片的展开状态，key = `${runId}:${stepId}` → { status, open }。
  //: 已完成的步骤默认折叠：执行长任务时，视野留给"正在跑的那一步"。
  //: 记 status 是为了"某步被重跑（done → running）"时自动重新展开，而不是沿用旧选择。
  stepOpen: {},
  //: 正在执行的步骤：本地的"开始时间 + 索取文件/验收补轮次数"。
  //: 后端要等一步跑完才落账（指标是完成才写的），这里用事件流做**实时**进度，
  //: 否则长篇步骤跑起来界面只有一行"执行中"，用户不知道它是不是卡住了。
  stepLive: {},
  //: 普通折叠卡片（概览里的运行、架构/计划的卡片、架构段原始输出）的展开状态。
  //: 只记用户显式点过的；没点过就按"是否还在输出"决定的默认值走。
  cardOpen: {},
  statusHint: "", // 后端给当前状态的一句话说明（例如"正在判断这是需求还是问答…"）
  tab: "plan",
  docs: [],
  fileView: null,
  //: 右侧明细面板是否展开（默认收起：主区占满，Codex 式）
  panelOpen: false,
  //: 分段路由弹窗当前编辑哪一段（architect / editor / both）
  routeStage: "both",
  //: 设置弹窗当前分区
  settingsSection: "model",
  renderQueued: false,
  providers: [],
  activeProviderId: "",
  editingProviderId: "",
  presets: {},
  sidebarWide: true,
  marketTab: "market",
  runQuery: "",
  showArchived: false,
  palette: { items: [], active: 0, open: false },
  // 异步加载的请求序号：旧响应回来时直接丢弃，避免覆盖新状态（竞态会让界面"点了没反应"）
  tokens: { runs: 0, plugins: 0, market: 0, providers: 0, capabilities: 0 },
  //: 能力中心（skill / MCP / 插件）的列表缓存
  capabilities: [],
  market: { capabilities: null, sources: [], items: [], query: "", sourceId: "" },
  plugins: { plugins: [], footer_actions: [] },
};

const dom = {
  runList: document.getElementById("run-list"),
  runTitle: document.getElementById("run-title"),
  statusPill: document.getElementById("status-pill"),
  routeChips: document.getElementById("route-chips"),
  timeline: document.getElementById("timeline"),
  inspectorBody: document.getElementById("inspector-body"),
  sidebarRoute: document.getElementById("sidebar-route"),
  taskInput: document.getElementById("task-input"),
  targetDir: document.getElementById("target-dir"),
  sendBtn: document.getElementById("send-btn"),
  cancelBtn: document.getElementById("cancel-btn"),
  composerHint: document.getElementById("composer-hint"),
  settingsBtn: document.getElementById("settings-btn"),
};

/* ── 基础工具 ── */

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function request(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const error = payload.error || { message: `HTTP ${response.status}` };
    throw new Error(error.message + (error.details?.hint ? `（${error.details.hint}）` : ""));
  }
  return payload;
}

const api = {
  settings: () => request("GET", "/api/v1/settings"),
  saveSettings: (patch) => request("PUT", "/api/v1/settings", patch),
  runs: (params = {}) => request("GET", `/api/v1/runs?${new URLSearchParams(params)}`),
  patchRun: (id, body) => request("PATCH", `/api/v1/runs/${id}`, body),
  clientLog: (body) => request("POST", "/api/v1/client-log", body),
  run: (id) => request("GET", `/api/v1/runs/${id}`),
  createRun: (body) => request("POST", "/api/v1/runs", body),
  approve: (id, feedback) => request("POST", `/api/v1/runs/${id}/approve`, { feedback: feedback || "" }),
  resume: (id, body) => request("POST", `/api/v1/runs/${id}/resume`, body),
  continueRun: (id, instruction) =>
    request("POST", `/api/v1/runs/${id}/continue`, { instruction }),
  cancel: (id) => request("POST", `/api/v1/runs/${id}/cancel`, {}),
  retryRun: (id) => request("POST", `/api/v1/runs/${id}/retry`, {}),
  tree: (id) => request("GET", `/api/v1/runs/${id}/tree`),
  file: (id, path) => request("GET", `/api/v1/runs/${id}/file?path=${encodeURIComponent(path)}`),
  docs: (id) => request("GET", `/api/v1/runs/${id}/docs`),
  providers: () => request("GET", "/api/v1/providers"),
  createProvider: (body) => request("POST", "/api/v1/providers", body),
  updateProvider: (id, body) => request("PUT", `/api/v1/providers/${id}`, body),
  deleteProvider: (id) => request("DELETE", `/api/v1/providers/${id}`),
  activateProvider: (id) => request("POST", `/api/v1/providers/${id}/activate`, {}),
  testProvider: (id) => request("POST", `/api/v1/providers/${id}/test`, {}),
  routes: (body) => request("PUT", "/api/v1/routes", body),
  revertStep: (id, stepId) => request("POST", `/api/v1/runs/${id}/steps/${stepId}/revert`, {}),
  restart: (rebuild) =>
    request("POST", "/api/v1/system/restart", { rebuild: Boolean(rebuild), confirm: true }),
  systemInfo: () => request("GET", "/api/v1/system/info"),
  devPreset: () => request("POST", "/api/v1/system/dev-preset", {}),
  marketCapabilities: () => request("GET", "/api/v1/market/capabilities"),
  marketSources: () => request("GET", "/api/v1/market/sources"),
  addMarketSource: (manifestUrl) =>
    request("POST", "/api/v1/market/sources", { manifest_url: manifestUrl }),
  removeMarketSource: (id) => request("DELETE", `/api/v1/market/sources/${id}`),
  marketItems: (params) => request("GET", `/api/v1/market/items?${new URLSearchParams(params)}`),
  plugins: () => request("GET", "/api/v1/plugins"),
  installPlugin: (body) => request("POST", "/api/v1/plugins", body),
  enablePlugin: (id) => request("POST", `/api/v1/plugins/${id}/enable`, {}),
  disablePlugin: (id) => request("POST", `/api/v1/plugins/${id}/disable`, {}),
  uninstallPlugin: (id) => request("DELETE", `/api/v1/plugins/${id}`),
  gitStatus: () => request("GET", "/api/v1/git/status"),
  gitLog: () => request("GET", "/api/v1/git/log?limit=15"),
  gitDiff: (path) => request("GET", `/api/v1/git/diff?path=${encodeURIComponent(path)}`),
  gitCommit: (body) => request("POST", "/api/v1/git/commit", body),
  gitPush: () => request("POST", "/api/v1/git/push", {}),
  gitPull: () => request("POST", "/api/v1/git/pull", {}),
  gitAutoCommit: () => request("GET", "/api/v1/git/auto-commit"),
  gitAutoCommitEnable: (push) => request("POST", "/api/v1/git/auto-commit", { push }),
  gitAutoCommitDisable: () => request("DELETE", "/api/v1/git/auto-commit"),
  gitAutoCommitRun: () => request("POST", "/api/v1/git/auto-commit/run", {}),
  gitStage: (paths) => request("POST", "/api/v1/git/stage", { paths }),
  gitUnstage: (paths) => request("POST", "/api/v1/git/unstage", { paths }),
  gitDiscard: (paths) => request("POST", "/api/v1/git/discard", { paths }),
  gitBranches: () => request("GET", "/api/v1/git/branches"),
  gitCheckout: (name, create) => request("POST", "/api/v1/git/branches", { name, create }),
  gitShow: (commit) => request("GET", `/api/v1/git/show?commit=${encodeURIComponent(commit)}`),
  gitProxyStatus: () => request("GET", "/api/v1/git/proxy"),
  gitProxySet: (proxyUrl) => request("POST", "/api/v1/git/proxy", { proxy_url: proxyUrl }),
  gitProxyClear: () => request("DELETE", "/api/v1/git/proxy"),
  // ── 项目（真实容器）──
  projects: (params = {}) => request("GET", `/api/v1/projects?${new URLSearchParams(params)}`),
  project: (id) => request("GET", `/api/v1/projects/${encodeURIComponent(id)}`),
  createProject: (body) => request("POST", "/api/v1/projects", body),
  updateProject: (id, body) =>
    request("PUT", `/api/v1/projects/${encodeURIComponent(id)}`, body),
  archiveProject: (id) => request("DELETE", `/api/v1/projects/${encodeURIComponent(id)}`),
  projectModule: (id, module) =>
    request("GET", `/api/v1/projects/${encodeURIComponent(id)}/modules/${module}`),
  // ── 普通对话（chat 会话）──
  chats: (params = {}) => request("GET", `/api/v1/chats?${new URLSearchParams(params)}`),
  // ── 能力中心（skill / MCP / 插件）──
  capabilities: (params = {}) =>
    request("GET", `/api/v1/capabilities?${new URLSearchParams(params)}`),
  enableCapability: (id) =>
    request("POST", `/api/v1/capabilities/${encodeURIComponent(id)}/enable`, {}),
  disableCapability: (id) =>
    request("POST", `/api/v1/capabilities/${encodeURIComponent(id)}/disable`, {}),
  uninstallCapability: (id) =>
    request("DELETE", `/api/v1/capabilities/${encodeURIComponent(id)}`),
  capabilityAudit: (limit = 30) => request("GET", `/api/v1/capabilities/audit?limit=${limit}`),
  capabilityBody: (id) => request("GET", `/api/v1/capabilities/${encodeURIComponent(id)}/body`),
  installSkill: (body) => request("POST", "/api/v1/capabilities/skills/install", body),
  setCapabilityScope: (id, body) =>
    request("POST", `/api/v1/capabilities/${encodeURIComponent(id)}/scope`, body),
  mcpPresets: () => request("GET", "/api/v1/capabilities/mcp/presets"),
  addMcpServer: (body) => request("POST", "/api/v1/capabilities/mcp/servers", body),
  trustCapability: (id) =>
    request("POST", `/api/v1/capabilities/${encodeURIComponent(id)}/trust`, {}),
  mcpTools: (id, refresh = false) =>
    request(
      "GET",
      `/api/v1/capabilities/${encodeURIComponent(id)}/mcp/tools?refresh=${refresh ? "true" : "false"}`
    ),
  mcpCall: (id, tool, args) =>
    request("POST", `/api/v1/capabilities/${encodeURIComponent(id)}/mcp/call`, {
      tool,
      arguments: args,
    }),
  chat: (id) => request("GET", `/api/v1/chats/${encodeURIComponent(id)}`),
  createChat: (body) => request("POST", "/api/v1/chats", body),
  sendChat: (id, text) =>
    request("POST", `/api/v1/chats/${encodeURIComponent(id)}/messages`, { text }),
  deleteChat: (id) => request("DELETE", `/api/v1/chats/${encodeURIComponent(id)}`),
};

const STATUS_TEXT = {
  idle: "待开始",
  planning: "架构段产出纲领中",
  awaiting_approval: "等待确认",
  executing: "执行中",
  blocked: "需要补充信息",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const STEP_STATUS_TEXT = {
  pending: "待执行",
  running: "执行中",
  done: "已完成",
  failed: "失败",
  blocked: "被阻塞",
  skipped: "已跳过",
};

function scheduleRender() {
  if (state.renderQueued) return;
  state.renderQueued = true;
  // 以前用 requestAnimationFrame：事件密集时最多每秒重建 60 次整个界面。
  // 改成 120ms 合并一次，人眼无感，负载降一个数量级。
  setTimeout(() => {
    state.renderQueued = false;
    render();
  }, 120);
}

/** 流式文本：把多个 token 事件合并到一帧里写 DOM（逐块写会触发强制重排）。 */
function flushStreams() {
  streamFrame = null;
  for (const key of pendingStreams) {
    const node = document.querySelector(`[data-stream="${key}"]`);
    if (!node) {
      scheduleRender();
      continue;
    }
    node.textContent = state.buffers[key] || "";
    node.scrollTop = node.scrollHeight;
  }
  pendingStreams.clear();
}

function queueStreamUpdate(key) {
  pendingStreams.add(key);
  if (streamFrame !== null) return;
  streamFrame = requestAnimationFrame(flushStreams);
}

function streamKeyFor(event) {
  if (event.data.phase === "architect") return "architect";
  if (event.data.phase === "chat") return "chat";
  return `step:${event.data.step_id}`;
}

/* ── 启动 ── */

async function boot() {
  bindEvents();
  applySidebarMode();
  // 运行中每秒走一次：让步骤卡上的「已用 N 秒 / 已生成 X KB」真的在动。
  // 1 秒一次、且只在有 running 步骤时才重绘，不会回到"逐 token 重排"的老路上。
  setInterval(() => {
    const steps = state.run?.steps || [];
    if (steps.some((step) => step.status === "running")) scheduleRender();
  }, 1000);
  // 明细面板默认收起；只恢复用户上次的显式选择
  setPanelOpen(localStorage.getItem(PANEL_KEY) === "1");
  try {
    state.settings = await api.settings();
  } catch (error) {
    state.settings = null;
  }
  await refreshProviders();
  await loadPlugins();
  await refreshRuns();
  const requested = new URLSearchParams(location.search).get("run");
  const target = requested || localStorage.getItem(STORAGE_KEY);
  if (target && state.runs.some((item) => item.id === target)) {
    await openRun(target);
  } else {
    render();
  }
}

function bindEvents() {
  // 统一走 on()：元素缺失或处理函数抛错都会被记录上报，而不是"点了没反应"
  on("new-run-btn", "click", startNewRun);
  on("send-btn", "click", submitTask);
  on("cancel-btn", "click", async () => {
    if (!state.run) return;
    await api.cancel(state.run.id);
  });
  on("task-input", "keydown", (event) => {
    // Enter 发送，Shift+Enter 换行（Ctrl/Cmd+Enter 也保留，老习惯不用改）
    if (event.key !== "Enter" || event.shiftKey) return;
    // 输入法组合中的 Enter 是"选词确认"：中文输入敲到一半按回车不该把任务发出去
    if (event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    submitTask();
  });
  // 注意：这里不能直接把 openSettings 当处理器——它第一个参数是"要显示哪个分区"，
  // 直接传会把 MouseEvent 当成分区名，结果四个分区全部隐藏。
  on("settings-btn", "click", () => openSettings());
  // 长上下文自动拆分：开关一变，下面的可选项即时启用/禁用并说明效果
  on("f-chat-context", "change", updateChatContextOptionState);
  on("f-chat-auto-split", "change", updateChatContextOptionState);
  on("settings-nav", "click", (event) => {
    const item = event.target.closest(".settings-nav-item");
    if (item) switchSettingsSection(item.dataset.section);
  });
  on("settings-open-market", "click", () => {
    closeSettings();
    openMarket("market");
  });
  on("settings-open-capabilities", "click", () => {
    closeSettings();
    openCapabilities();
  });
  on("settings-open-git", "click", () => {
    closeSettings();
    openGitPanel();
  });
  on("settings-open-stats", "click", () => {
    closeSettings();
    switchInspectorTab("plan");
  });
  on("settings-close", "click", closeSettings);
  on("settings-cancel", "click", closeSettings);
  on("route-close", "click", closeRouteModal);
  on("route-cancel", "click", closeRouteModal);
  on("route-switch", "click", () => {
    renderRouteStage(state.routeStage === "architect" ? "editor" : "architect");
  });
  on("route-save", "click", saveRouteModal);
  on("route-follow", "click", followCurrentProvider);
  on("route-modal", "click", (event) => {
    if (event.target.id === "route-modal") closeRouteModal();
  });
  on("dev-preset-btn", "click", applyDevPreset);
  on("dev-mode", "change", (event) => applyDevMode(event.target.checked));
  on("settings-save", "click", saveSettings);
  on("settings-test", "click", testConnection);
  on("provider-new", "click", newProvider);
  on("provider-copy", "click", copyProvider);
  on("provider-delete", "click", deleteProvider);
  on("provider-activate", "click", activateProvider);
  on("f-provider-select", "change", (event) => {
    selectProvider(event.target.value);
  });
  on("f-provider-kind", "change", renderPresets);
  on("f-split-mode", "change", (event) => {
    document.getElementById("split-fields").hidden = !event.target.checked;
  });
  on("provider-quick", "change", (event) => {
    quickSwitchProvider(event.target.value);
  });
  on("sidebar-toggle", "click", () => {
    state.sidebarWide = !state.sidebarWide;
    localStorage.setItem(SIDEBAR_KEY, state.sidebarWide ? "1" : "0");
    applySidebarMode();
  });
  on("capabilities-btn", "click", () => openCapabilities());
  on("capabilities-close", "click", closeCapabilities);
  on("capabilities-modal", "click", (event) => {
    if (event.target.id === "capabilities-modal") closeCapabilities();
  });
  on("git-btn", "click", openGitPanel);
  on("git-close", "click", closeGitPanel);
  on("git-modal", "click", (event) => {
    if (event.target.id === "git-modal") closeGitPanel();
  });
  on("updates-btn", "click", showVersionInfo);
  on("market-close", "click", closeMarket);
  on("market-tabs", "click", (event) => {
    const tab = event.target.closest(".tab");
    if (!tab) return;
    state.marketTab = tab.dataset.mtab;
    document.querySelectorAll("#market-tabs .tab").forEach((node) => {
      node.classList.toggle("active", node === tab);
    });
    renderMarket();
  });
  on("inspector-tabs", "click", (event) => {
    const tab = event.target.closest(".tab");
    if (!tab) return;
    switchInspectorTab(tab.dataset.tab);
  });
  on("inspector-toggle", "click", togglePanel);
  on("inspector-close", "click", () => setPanelOpen(false));
  // 任务列表：搜索 / 归档开关
  on("run-search", "input", (event) => {
    state.runQuery = event.target.value.trim();
    refreshRuns();
  });
  on("run-archive-toggle", "click", (event) => {
    state.showArchived = !state.showArchived;
    event.currentTarget.classList.toggle("active", state.showArchived);
    refreshRuns();
  });
  on("palette-input", "input", renderPalette);
  on("palette-input", "keydown", handlePaletteKey);
  on("palette-modal", "click", (event) => {
    if (event.target.id === "palette-modal") closePalette();
  });
  // 所有弹窗都支持"点遮罩关闭"：否则遮罩会静默吃掉后续点击（表现为"点了没反应"）
  on("settings-modal", "click", (event) => {
    if (event.target.id === "settings-modal") closeSettings();
  });
  on("market-modal", "click", (event) => {
    if (event.target.id === "market-modal") closeMarket();
  });
  on("confirm-modal", "click", (event) => {
    if (event.target.id === "confirm-modal") {
      const cancel = document.getElementById("confirm-cancel");
      if (cancel) cancel.click();
    }
  });
  // 全局键盘：Ctrl+K 命令面板、Ctrl+/ 快捷键、Esc 关闭最上层
  document.addEventListener("keydown", safe(handleGlobalKeydown));
}

/** 键盘快捷键总入口（Codex 式：Ctrl+K 命令面板）。 */
function handleGlobalKeydown(event) {
  const key = event.key.toLowerCase();
  if ((event.ctrlKey || event.metaKey) && key === "k") {
    event.preventDefault();
    togglePalette();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key === "/") {
    event.preventDefault();
    showShortcutHelp();
    return;
  }
  // Ctrl/⌘+1..4：直接切右侧明细标签（并自动展开面板）
  if ((event.ctrlKey || event.metaKey) && ["1", "2", "3", "4"].includes(event.key)) {
    const tabs = ["plan", "changes", "files", "docs"];
    event.preventDefault();
    switchInspectorTab(tabs[Number(event.key) - 1]);
    return;
  }
  if (event.key === "Escape") {
    if (state.palette.open) {
      closePalette();
      return;
    }
    for (const id of [
      "settings-modal",
      "market-modal",
      "capabilities-modal",
      "route-modal",
      "confirm-modal",
    ]) {
      const modal = document.getElementById(id);
      if (modal && !modal.hidden) {
        modal.hidden = true;
        return;
      }
    }
    // 没有弹窗时，Esc 收起右侧明细面板
    if (state.panelOpen) {
      setPanelOpen(false);
      return;
    }
  }
}

/** 绑定助手：元素缺失或处理函数抛错都会被记录并提示，而不是静默失效。 */
function on(elementId, eventName, handler) {
  const element = document.getElementById(elementId);
  if (!element) {
    reportClientError("binding", new Error(`缺少元素 #${elementId}`), { event: eventName });
    return null;
  }
  element.addEventListener(eventName, safe(handler));
  return element;
}

function safe(handler) {
  return async (...args) => {
    try {
      return await handler(...args);
    } catch (error) {
      reportClientError("handler", error, { handler: handler.name || "anonymous" });
      return undefined;
    }
  };
}
