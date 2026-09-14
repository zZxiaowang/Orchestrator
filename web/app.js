/* 编排器前端：Codex 风格的三栏工作台。
   数据来源：REST 拉取运行状态 + SSE 实时事件流。
   渲染策略：结构性事件触发整体重排；流式 token 追加到缓冲区，
   因此在执行中重排也不会丢字。 */

const STORAGE_KEY = "orchestrator.lastRun";
const SIDEBAR_KEY = "orchestrator.sidebarWide";
const PANEL_KEY = "orchestrator.panelOpen";

//: 统计看板的刷新入口：宿主元素在「统计」标签里按需创建，所以不能只在加载时抓一次
let dashboardRefresh = null;

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
  tokens: { runs: 0, plugins: 0, market: 0, providers: 0 },
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
  requestAnimationFrame(() => {
    state.renderQueued = false;
    render();
  });
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
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      submitTask();
    }
  });
  // 注意：这里不能直接把 openSettings 当处理器——它第一个参数是"要显示哪个分区"，
  // 直接传会把 MouseEvent 当成分区名，结果四个分区全部隐藏。
  on("settings-btn", "click", () => openSettings());
  on("settings-nav", "click", (event) => {
    const item = event.target.closest(".settings-nav-item");
    if (item) switchSettingsSection(item.dataset.section);
  });
  on("settings-open-market", "click", () => {
    closeSettings();
    openMarket("market");
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
  on("market-btn", "click", () => openMarket("market"));
  on("plugins-btn", "click", () => openMarket("installed"));
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
    for (const id of ["settings-modal", "market-modal", "route-modal", "confirm-modal"]) {
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

/* ── 运行列表 ── */

async function refreshRuns() {
  const token = ++state.tokens.runs;
  try {
    const payload = await api.runs({
      include_archived: state.showArchived,
      q: state.runQuery,
    });
    if (token !== state.tokens.runs) return; // 有更新的请求在飞，丢弃旧响应
    state.runs = payload.runs || [];
  } catch (error) {
    if (token !== state.tokens.runs) return;
    reportClientError("runs", error);
    state.runs = [];
  }
  renderRunList();
}

function renderRunList() {
  dom.runList.replaceChildren(
    ...(state.runs.length
      ? state.runs.map((item) => {
          const actions = h(
            "div",
            { class: "run-actions" },
            runActionButton(item.pinned ? "★" : "☆", item.pinned ? "取消置顶" : "置顶", () =>
              patchRun(item.id, { pinned: !item.pinned })
            ),
            runActionButton("✎", "重命名", () => renameRun(item)),
            runActionButton("▣", item.archived ? "取消归档" : "归档", () =>
              patchRun(item.id, { archived: !item.archived })
            )
          );
          return h(
            "div",
            {
              class: `run-item${state.run && state.run.id === item.id ? " active" : ""}${
                item.pinned ? " pinned" : ""
              }${item.archived ? " archived" : ""}`,
              title: item.title || item.task || item.id,
              dataset: { run: item.id },
              onclick: () => openRun(item.id),
            },
            h("span", { class: "run-dot" }),
            h("span", { class: "run-name", text: item.title || item.task || item.id }),
            h(
              "span",
              { class: "run-meta" },
              h("span", { text: STATUS_TEXT[item.status] || item.status }),
              h("span", { text: `${item.steps_done}/${item.steps_total} 步` }),
              item.files_changed ? h("span", { text: `${item.files_changed} 文件` }) : null
            ),
            actions
          );
        })
      : [h("p", { class: "muted-small", text: state.runQuery ? "没有匹配的任务" : "还没有任务" })])
  );
}

function runActionButton(label, title, handler) {
  const button = h("button", { type: "button", title, text: label });
  button.addEventListener(
    "click",
    safe(async (event) => {
      event.stopPropagation();
      await handler();
    })
  );
  return button;
}

async function patchRun(runId, patch) {
  await api.patchRun(runId, patch);
  await refreshRuns();
  if (state.run && state.run.id === runId && patch.title) {
    state.run.title = patch.title;
    updateStatus();
  }
}

async function renameRun(item) {
  const title = await appPrompt({
    title: "重命名任务",
    label: "新名称",
    value: item.title || "",
    confirmText: "保存",
  });
  if (title === null) return;
  await patchRun(item.id, { title });
}

/* ── 打开运行 + SSE ── */

async function openRun(id) {
  disconnectStream();
  try {
    const payload = await api.run(id);
    state.run = payload.run;
    state.lastSeq = payload.event_seq || 0;
    state.buffers = {};
    state.fileView = null;
    state.docs = [];
    localStorage.setItem(STORAGE_KEY, id);
    renderRunList();
    render();
    // stream=0：只渲染快照，不订阅实时事件（用于静态截图/无 SSE 环境）
    const params = new URLSearchParams(location.search);
    if (params.get("stream") !== "0") connectStream(id, state.lastSeq);
    loadDocs();
  } catch (error) {
    showToast(error.message);
  }
}

function connectStream(id, since) {
  disconnectStream();
  const url = `/api/v1/runs/${id}/events?since=${Number(since) || 0}`;
  const source = new EventSource(url);
  state.es = source;
  source.onmessage = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    handleEvent(payload);
  };
  source.onerror = () => {
    source.close();
    if (state.es !== source) return;
    state.es = null;
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(() => {
      if (state.run && state.run.id === id) connectStream(id, state.lastSeq);
    }, 1500);
  };
}

function disconnectStream() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  clearTimeout(state.reconnectTimer);
}

function handleEvent(event) {
  if (event.type === "ping") return;
  if (typeof event.seq === "number") state.lastSeq = event.seq;

  switch (event.type) {
    case "token": {
      const key = streamKeyFor(event);
      state.buffers[key] = (state.buffers[key] || "") + (event.data.text || "");
      const node = document.querySelector(`[data-stream="${key}"]`);
      if (node) {
        node.textContent = state.buffers[key];
        node.scrollTop = node.scrollHeight;
      } else {
        scheduleRender();
      }
      if (event.data.phase === "executor") {
        const card = document.querySelector(`[data-step="${event.data.step_id}"]`);
        if (card) card.dataset.status = "running";
      }
      break;
    }
    case "plan": {
      state.buffers.architect = "";
      if (state.run) {
        state.run.plan = event.data.plan;
        state.run.steps = (event.data.steps || []).map((step) => ({
          ...step,
          status: step.status || "pending",
          files: step.files || [],
          commands: step.commands || [],
          notes: step.notes || [],
          summary: step.summary || "",
        }));
      }
      scheduleRender();
      refreshRuns();
      break;
    }
    case "step_start": {
      if (state.run) {
        const step = (state.run.steps || []).find((item) => item.id === event.data.step_id);
        if (step) step.status = "running";
      }
      scheduleRender();
      break;
    }
    case "step_done": {
      if (state.run) {
        const index = (state.run.steps || []).findIndex((item) => item.id === event.data.step.id);
        if (index >= 0) state.run.steps[index] = event.data.step;
      }
      delete state.buffers[`step:${event.data.step.id}`];
      scheduleRender();
      refreshRuns();
      break;
    }
    case "file": {
      if (state.run) {
        const step = (state.run.steps || []).find((item) => item.id === event.data.step_id);
        if (step && !step.files.some((file) => file.path === event.data.file.path)) {
          step.files.push(event.data.file);
        }
      }
      scheduleRender();
      break;
    }
    case "status": {
      if (state.run) state.run.status = event.data.status;
      state.statusHint = event.data.message || "";
      if (["done", "failed", "cancelled", "blocked"].includes(event.data.status)) {
        state.statusHint = "";
      }
      updateStatus();
      updateComposer();
      if (["done", "failed", "cancelled"].includes(event.data.status)) refreshRuns();
      break;
    }
    case "verify": {
      // 客观验收结果：本步过没过，界面必须能立刻看到
      if (state.run) {
        const step = (state.run.steps || []).find((item) => item.id === event.data.step_id);
        if (step) step.verification = event.data.results || [];
      }
      scheduleRender();
      break;
    }
    case "error": {
      if (state.run) state.run.error = event.data.error;
      scheduleRender();
      break;
    }
    case "done": {
      refreshRun();
      break;
    }
    case "metrics_updated": {
      // 指标账本只增不减：按 phase + step_id 就地替换，避免整页重拉打断流式输出
      if (state.run && event.data.metrics) {
        const list = Array.isArray(state.run.metrics) ? state.run.metrics.slice() : [];
        const incoming = event.data.metrics;
        const index = list.findIndex(
          (item) =>
            item.phase === incoming.phase &&
            (item.step_id ?? null) === (incoming.step_id ?? null),
        );
        if (index >= 0) list[index] = incoming;
        else list.push(incoming);
        state.run.metrics = list;
        scheduleRender();
      }
      break;
    }
    case "artifact": {
      loadDocs();
      break;
    }
    default:
      break;
  }
}

async function refreshRun() {
  if (!state.run) return;
  try {
    const payload = await api.run(state.run.id);
    const wasExecuting = state.run.status;
    state.run = payload.run;
    state.lastSeq = payload.event_seq || state.lastSeq;
    if (wasExecuting !== "executing") state.buffers = {};
    scheduleRender();
    refreshRuns();
  } catch (error) {
    showToast(error.message);
  }
}

/* ── 渲染 ── */

function render() {
  renderRunList();
  updateStatus();
  updateRouteChips();
  renderRunActions();
  renderTimeline();
  renderInspector();
  updateComposer();
  // 看板宿主在明细面板里：整体重绘后补一次刷新（宿主可能是刚创建出来的）
  if (typeof dashboardRefresh === "function") dashboardRefresh();
}

/**
 * 运行操作条：把"这个运行现在能做什么"收在一处。

 * 以前确认在纲领卡片里、重试/回滚在步骤卡片里、继续在时间线底部，
 * 用户得先找到那张卡片才知道能做什么。
 */
function renderRunActions() {
  const host = document.getElementById("run-actions");
  if (!host) return;
  const run = state.run;
  const status = run ? run.status : "";
  const nodes = [];
  const button = (label, cls, handler, title = "") => {
    const node = h("button", { class: `btn ${cls} small`, type: "button", text: label, title });
    node.addEventListener("click", safe(handler));
    return node;
  };

  if (status === "awaiting_approval") {
    nodes.push(
      button("确认并开始执行", "primary", async () => {
        await api.approve(run.id, "");
        state.run.status = "executing";
        render();
      })
    );
  }
  if (status === "planning" || status === "executing") {
    nodes.push(button("停止", "ghost", () => api.cancel(run.id)));
  }
  if (run && ["done", "blocked", "failed", "paused"].includes(status)) {
    nodes.push(
      button("继续说下一步", "ghost", () => {
        const input = document.getElementById("continue-input");
        if (input) {
          input.scrollIntoView({ block: "center" });
          input.focus();
        }
      })
    );
  }
  const hasBlocked = (run?.steps || []).some((step) => step.status === "blocked");
  if (status === "blocked" || (status === "failed" && hasBlocked)) {
    nodes.push(
      button("补充信息并继续", "ghost", () => {
        const box = document.querySelector(".approval textarea.feedback");
        if (box) {
          box.scrollIntoView({ block: "center" });
          box.focus();
        }
      })
    );
  }
  nodes.push(
    button("⟳ 打包重启", "ghost", rebuildAndRestart, "改完源码后重新打包并重启，让改动生效")
  );
  host.replaceChildren(...nodes);
}

function updateStatus() {
  const status = state.run ? state.run.status : "idle";
  dom.statusPill.dataset.status = status;
  // 运行中优先显示后端说明：等待模型返回的那段时间里，用户得知道系统在干什么
  const transient = status === "planning" || status === "executing";
  dom.statusPill.textContent =
    (transient && state.statusHint) || STATUS_TEXT[status] || status;
  dom.runTitle.textContent = state.run ? state.run.title : "新任务";
}

function updateRouteChips() {
  const settings = state.settings;
  if (!settings) {
    dom.routeChips.replaceChildren(h("span", { class: "chip missing", text: "设置未加载" }));
    return;
  }
  const architect = settings.architect;
  const editor = settings.editor;
  const routes = settings.routes || {};
  const split = Boolean(routes.architect?.provider_id || routes.editor?.provider_id);
  const architectLabel = `架构 ${architect.model}${architect.host ? ` @ ${architect.host}` : ""}`;
  const editorLabel = `执行 ${editor.model}${editor.host ? ` @ ${editor.host}` : ""}`;
  // 两个标签本身就是入口：点它就能改"哪一段用哪套配置、哪个模型"，
  // 不必再去设置弹窗里翻分段模式。
  const chip = (role, label, configured) => {
    const node = h("button", {
      class: `chip ${role}${configured ? "" : " missing"}`,
      type: "button",
      title: `修改${role === "architect" ? "架构段" : "执行段"}：用哪套配置、哪个模型`,
      text: `${label}${configured ? "" : " · 未配置"}`,
    });
    // 点哪个标签就只改哪一段（之前两个标签打开同一个"两段都在"的弹窗，容易让人困惑）
    node.addEventListener("click", safe(() => openRouteModal(role)));
    return node;
  };
  dom.routeChips.replaceChildren(
    chip("architect", architectLabel, architect.configured),
    h("span", { class: "muted", text: "→" }),
    chip("editor", editorLabel, editor.configured),
    h("button", {
      class: "icon-btn route-edit",
      id: "route-edit",
      type: "button",
      title: "修改分段路由（两段各用哪套配置 / 模型）",
      text: "✎",
      onclick: safe(() => openRouteModal("both")),
    })
  );
  if (!architect.configured || !editor.configured) {
    dom.sidebarRoute.textContent = "尚未配置中转地址或 Key";
  } else if (split) {
    dom.sidebarRoute.textContent = `分段：架构 ${architect.host || "—"} → 执行 ${editor.host || "—"}`;
  } else {
    dom.sidebarRoute.textContent = `${settings.active_provider?.name || "当前配置"} · ${architect.host || "—"}`;
  }
}

function renderTimeline() {
  const run = state.run;
  if (!run) {
    dom.timeline.replaceChildren(renderEmptyState());
    return;
  }

  const nodes = [];
  nodes.push(
    h(
      "div",
      { class: "card msg-user" },
      h("div", { class: "card-head" }, h("div", { class: "avatar", text: "你" }), h("strong", { text: "需求" })),
      h("div", { class: "card-body", text: run.task })
    )
  );

  // 问答路由：分流判定「这条不是需求」时，只有一条回答，没有纲领也没有步骤
  if (run.kind === "chat") {
    const answer = (run.messages || []).find((message) => message.phase === "chat");
    const streaming = state.buffers.chat || "";
    nodes.push(
      h(
        "div",
        { class: "card msg-assistant" },
        h(
          "div",
          { class: "card-head" },
          h("div", { class: "avatar", text: "答" }),
          h("strong", { text: "直接回答" }),
          h("span", { class: "muted", text: "判定为问答 · 未进入编排" }),
          copyButton(() => state.buffers.chat || answer?.content || "")
        ),
        h(
          "div",
          { class: "card-body" },
          h("pre", {
            class: "stream",
            dataset: { stream: "chat" },
            text: streaming || (answer ? answer.content : "正在组织回答…"),
          })
        )
      )
    );
    dom.timeline.replaceChildren(...nodes);
    return;
  }

  const architectRaw = (run.messages || []).find((message) => message.phase === "architect");
  const architectBuffer = state.buffers.architect || "";
  if (architectBuffer || architectRaw) {
    const body = h("pre", {
      class: "stream",
      dataset: { stream: "architect" },
      text: architectBuffer || (architectRaw ? architectRaw.content : ""),
    });
    nodes.push(
      h(
        "div",
        { class: "card" },
        h(
          "div",
          { class: "card-head" },
          h("div", { class: "avatar", text: "G" }),
          h("strong", { text: "架构段输出 · " + (run.route?.architect?.model || "GPT") }),
          h("span", { class: "muted", text: run.plan ? "（已解析为纲领）" : "流式生成中…" })
        ),
        h("div", { class: "card-body" }, body)
      )
    );
  }

  if (run.plan) nodes.push(renderPlanCard(run.plan));
  if (run.status === "awaiting_approval") nodes.push(renderApprovalCard());
  // 被阻塞（含旧版本记录为 failed 但某一步确实 blocked 的运行）都可以续跑
  const hasBlockedStep = (run.steps || []).some((step) => step.status === "blocked");
  if (run.status === "blocked" || (run.status === "failed" && hasBlockedStep)) {
    nodes.push(renderResumeCard(run));
  }

  for (const step of run.steps || []) nodes.push(renderStepCard(step));

  // 多轮续聊：跑完之后还能接着说"下一步做什么"，追加步骤而不重跑已完成的部分
  const canContinue =
    (run.steps || []).length > 0 &&
    ["done", "blocked", "failed", "paused"].includes(run.status);
  if (canContinue) nodes.push(renderContinueCard(run));

  if (run.error) {
    nodes.push(h("div", { class: "error-box", text: `运行失败：${run.error.message || JSON.stringify(run.error)}` }));
  }

  if (run.status === "done") {
    const done = (run.steps || []).filter((step) => step.status === "done").length;
    const files = (run.steps || []).reduce((total, step) => total + (step.files || []).length, 0);
    nodes.push(
      h(
        "div",
        { class: "card" },
        h("div", { class: "card-head" }, h("strong", { text: "执行完成" })),
        h("div", { class: "card-body", text: `共 ${done} 步完成，${files} 个文件发生变更。可切换到右侧「变更 / 文档」查看细节。` })
      )
    );
  }

  dom.timeline.replaceChildren(...nodes);
  const active = dom.timeline.querySelector('[data-status="running"]');
  if (active) active.scrollIntoView({ block: "nearest" });
}

function renderEmptyState() {
  const architectReady = Boolean(state.settings?.architect?.configured);
  const editorReady = Boolean(state.settings?.editor?.configured);
  // 首次使用：别先讲一堆功能，直接告诉用户"现在做哪三步"
  if (state.settings && !(architectReady && editorReady)) {
    const missing = !architectReady && !editorReady ? "中转地址与 Key" : "缺少的那一段配置";
    const openButton = h("button", { class: "btn primary", type: "button", text: "去配置" });
    openButton.addEventListener("click", safe(() => openSettings("model")));
    const testButton = h("button", { class: "btn ghost", type: "button", text: "测试连接" });
    testButton.addEventListener(
      "click",
      safe(() => {
        openSettings("model");
        setTimeout(() => document.getElementById("settings-test")?.click(), 400);
      })
    );
    return h(
      "div",
      { class: "empty" },
      h("h2", { text: "还差一步：先配好模型端点" }),
      h("p", { text: `当前缺少：${missing}。填好后就能开始第一个任务。` }),
      h(
        "div",
        { class: "empty-flow" },
        h("div", { class: "flow-node" }, h("b", { text: "① 填地址与 Key" }), "中转网关或官方直连都行"),
        h("div", { class: "flow-node" }, h("b", { text: "② 测一下连接" }), "确认 Key 有效、模型可用"),
        h("div", { class: "flow-node" }, h("b", { text: "③ 描述目标" }), "架构出纲领，确认后执行")
      ),
      h("div", { class: "empty-actions" }, openButton, testButton),
      h("p", {
        class: "muted",
        text: "配置只保存在本机 data/settings.json，接口不回传明文 Key。",
      })
    );
  }
  return h(
    "div",
    { class: "empty" },
    h("h2", { text: "GPT 定纲领，DeepSeek V4 落执行" }),
    h("p", {
      text: "描述目标后，架构段先产出结构化纲领（目标 / 原则 / 组件 / 步骤 / 验收标准），你确认后执行段逐步落地为真实文件改动。",
    }),
    h(
      "div",
      { class: "empty-flow" },
      h("div", { class: "flow-node" }, h("b", { text: "① 需求" }), "用自然语言描述目标"),
      h("div", { class: "flow-node" }, h("b", { text: "② 纲领（GPT）" }), "架构与步骤拆分"),
      h("div", { class: "flow-node" }, h("b", { text: "③ 执行（DeepSeek）" }), "按步落地并给出改动")
    ),
    h("p", { class: "muted", text: "先在右上角 ⚙ 配置中转地址与 Key。" })
  );
}

function renderPlanCard(plan) {
  const body = h("div", { class: "card-body" });
  body.append(h("p", { class: "plan-goal", text: plan.goal || "（未命名目标）" }));
  if (plan.summary) body.append(h("p", { class: "muted", text: plan.summary }));
  if (plan.principles?.length) {
    body.append(h("h3", { class: "section-label", text: "设计原则" }));
    body.append(h("ul", { class: "list" }, ...plan.principles.map((item) => h("li", { text: item }))));
  }
  if (plan.components?.length) {
    body.append(h("h3", { class: "section-label", text: "组件" }));
    body.append(
      h(
        "ul",
        { class: "list" },
        ...plan.components.map((item) =>
          h("li", {}, h("b", { text: item.name }), item.responsibility ? `：${item.responsibility}` : "")
        )
      )
    );
  }
  if (plan.risks?.length) {
    body.append(h("h3", { class: "section-label", text: "风险" }));
    body.append(h("ul", { class: "list" }, ...plan.risks.map((item) => h("li", { text: item }))));
  }
  return h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "avatar", text: "纲" }),
      h("strong", { text: "纲领性架构" }),
      h("span", { class: "muted", text: `${plan.steps?.length || 0} 步` })
      ,
      copyButton(() =>
        [
          `目标：${plan.goal || ""}`,
          plan.summary ? `思路：${plan.summary}` : "",
          ...(plan.steps || []).map(
            (step) => `${step.id}. ${step.title}${step.goal ? " — " + step.goal : ""}`
          ),
        ]
          .filter(Boolean)
          .join("\n")
      )
    ),
    body
  );
}

/** 复制按钮（Codex 式：卡片右上角一键复制）。 */
function copyButton(getText, label = "复制") {
  const button = h("button", {
    class: "copy-btn",
    type: "button",
    text: label,
    title: "复制到剪贴板",
  });
  button.addEventListener(
    "click",
    safe(async (event) => {
      event.stopPropagation();
      const text = String(getText() || "");
      if (!text.trim()) {
        showToast("没有可复制的内容。");
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        showToast("已复制到剪贴板。");
        return;
      } catch (error) {
        // WebView / 非安全上下文里 clipboard 可能不可用，退回 execCommand
      }
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.append(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      showToast(ok ? "已复制到剪贴板。" : "复制失败，请手动选择文本。");
    })
  );
  return button;
}

function renderApprovalCard() {
  const feedback = h("textarea", {
    class: "feedback",
    placeholder: "可选：写下要调整的点（例如「把第 2 步拆成迁移与回滚两条」），点「按反馈重做纲领」",
  });
  const approveBtn = h("button", { class: "btn primary", text: "确认并开始执行" });
  const replanBtn = h("button", { class: "btn ghost", text: "按反馈重做纲领" });

  approveBtn.addEventListener("click", async () => {
    approveBtn.disabled = true;
    try {
      await api.approve(state.run.id, "");
      if (state.run) state.run.status = "executing";
      render();
    } catch (error) {
      showToast(error.message);
      approveBtn.disabled = false;
    }
  });
  replanBtn.addEventListener("click", async () => {
    const text = feedback.value.trim();
    if (!text) {
      showToast("请先写下要调整的点。");
      return;
    }
    replanBtn.disabled = true;
    try {
      state.buffers = {};
      await api.approve(state.run.id, text);
      if (state.run) {
        state.run.status = "planning";
        state.run.steps = [];
      }
      render();
    } catch (error) {
      showToast(error.message);
      replanBtn.disabled = false;
    }
  });

  return h(
    "div",
    { class: "card approval" },
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "avatar", text: "✓" }),
      h("strong", { text: "等待你确认" }),
      h("span", { class: "muted", text: "确认后才进入执行段" })
    ),
    h("div", { class: "card-body" }, feedback, h("div", { class: "approval-actions" }, approveBtn, replanBtn))
  );
}

/** 被阻塞时的恢复卡片：补信息/指定目录后可只重跑那一步。 */
function renderContinueCard(run) {
  const input = h("textarea", {
    class: "feedback",
    id: "continue-input",
    placeholder:
      "接着说下一步要做什么，例如：「把这个改动跑一遍测试，没过就修到过」或「再补一份验收清单」",
  });
  const button = h("button", {
    class: "btn primary",
    id: "continue-btn",
    type: "button",
    text: "继续这项任务",
  });
  button.addEventListener(
    "click",
    safe(async () => {
      const instruction = input.value.trim();
      if (!instruction) {
        showToast("先写下要继续做什么。");
        return;
      }
      button.disabled = true;
      try {
        state.buffers = {};
        const payload = await api.continueRun(run.id, instruction);
        state.run = payload.run;
        connectStream(run.id, state.lastSeq);
        render();
      } catch (error) {
        showToast(error.message);
        button.disabled = false;
      }
    })
  );
  return h(
    "div",
    { class: "card approval" },
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "avatar", text: "＋" }),
      h("strong", { text: "继续说下一步" }),
      h("span", { class: "muted", text: "只追加新步骤，已完成的部分不重跑" })
    ),
    h("div", { class: "card-body" }, input, h("div", { class: "approval-actions" }, button))
  );
}

function renderResumeCard(run) {
  const blocked = (run.steps || []).find((step) => step.status === "blocked");
  const note = h("textarea", {
    class: "feedback",
    placeholder:
      "缺什么补什么。例如：「仓库在 D:\\\\work\\\\taskbar，入口是 src/App.tsx」；不想接现有代码就写「从零新建」",
  });
  const dir = h("input", {
    placeholder: "可选：要改动的现有目录，如 D:\\\\work\\\\taskbar",
  });
  const resumeBtn = h("button", { class: "btn primary", text: "补充信息并继续" });
  const replanBtn = h("button", { class: "btn ghost", text: "按这些信息重做纲领" });

  resumeBtn.addEventListener("click", async () => {
    const text = note.value.trim();
    const targetDir = dir.value.trim();
    if (!text && !targetDir) {
      showToast("请填写补充说明或目标目录。");
      return;
    }
    resumeBtn.disabled = true;
    try {
      await api.resume(run.id, { note: text, target_dir: targetDir });
      if (state.run) state.run.status = "executing";
      render();
    } catch (error) {
      showToast(error.message);
      resumeBtn.disabled = false;
    }
  });

  replanBtn.addEventListener("click", async () => {
    const text = note.value.trim();
    if (!text) {
      showToast("请写下要补的信息，再重做纲领。");
      return;
    }
    replanBtn.disabled = true;
    try {
      state.buffers = {};
      await api.approve(run.id, text);
      if (state.run) {
        state.run.status = "planning";
        state.run.steps = [];
      }
      render();
    } catch (error) {
      showToast(error.message);
      replanBtn.disabled = false;
    }
  });

  return h(
    "div",
    { class: "card approval" },
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "avatar", text: "!" }),
      h("strong", { text: "执行段被阻塞：需要你补充信息" }),
      h("span", { class: "muted", text: `第 ${blocked ? blocked.id : "?"} 步` })
    ),
    h(
      "div",
      { class: "card-body" },
      h("p", {
        text: blocked?.error || run.error?.message || "执行段表示信息不足，无法继续。",
      }),
      note,
      dir,
      h("div", { class: "approval-actions" }, resumeBtn, replanBtn)
    )
  );
}

function renderStepCard(step) {
  const status = step.status || "pending";
  const body = h("div", { class: "card-body" });
  const bufferKey = `step:${step.id}`;
  const buffer = state.buffers[bufferKey] || "";

  if (status === "running") {
    body.append(
      h("pre", { class: "stream", dataset: { stream: bufferKey }, text: buffer })
    );
  } else if (step.summary) {
    body.append(h("p", { text: step.summary }));
  } else if (step.error) {
    body.append(h("p", { text: step.error }));
  }

  if (step.files?.length) {
    for (const file of step.files) body.append(renderFileRow(file));
  }
  if (step.commands?.length) {
    body.append(h("h3", { class: "section-label", text: "建议命令（不会自动执行）" }));
    body.append(
      h(
        "ul",
        { class: "list" },
        ...step.commands.map((item) => h("li", {}, h("code", { text: item.cmd }), item.why ? ` — ${item.why}` : ""))
      )
    );
  }
  // 系统实际执行过的验证命令：退出码与输出都要能看见，否则"跑没跑"无从判断
  if (step.command_results?.length) {
    const ran = step.command_results.filter((item) => !item.skipped);
    const passed = ran.filter((item) => item.ok).length;
    body.append(
      h("h3", {
        class: "section-label",
        text: ran.length
          ? `验证命令（系统已执行 ${passed}/${ran.length} 通过）`
          : "验证命令（未执行：不在白名单或未开启）",
      })
    );
    for (const item of step.command_results) {
      const line = item.skipped
        ? `— ${item.cmd}：${item.error || "未执行"}`
        : `${item.ok ? "✓" : "✗"} ${item.cmd}（退出码 ${item.exit_code ?? "—"}，${(
            (item.duration_ms || 0) / 1000
          ).toFixed(1)}s）`;
      const row = h("div", {
        class: `cmd-row ${item.skipped ? "muted" : item.ok ? "verify-ok" : "verify-fail"}`,
        text: line,
      });
      if (item.output) {
        row.classList.add("clickable");
        const pre = h("pre", { class: "cmd-output", hidden: true, text: item.output });
        row.addEventListener("click", () => {
          pre.hidden = !pre.hidden;
        });
        body.append(row, pre);
      } else {
        body.append(row);
      }
    }
  }
  if (step.notes?.length) {
    body.append(h("h3", { class: "section-label", text: "说明" }));
    body.append(h("ul", { class: "list" }, ...step.notes.map((item) => h("li", { text: item }))));
  }
  // 客观验收：这是"模型说完成"和"确实完成"的区别，必须摊开给用户看
  if (step.verification?.length) {
    const passed = step.verification.filter((item) => item.ok).length;
    const total = step.verification.length;
    body.append(
      h("h3", {
        class: "section-label",
        text: `客观验收 ${passed}/${total} ${passed === total ? "全部通过" : "有未通过项"}`,
      })
    );
    body.append(
      h(
        "ul",
        { class: "verify-list" },
        ...step.verification.map((item) =>
          h(
            "li",
            { class: item.ok ? "verify-ok" : "verify-fail" },
            `${item.ok ? "✓" : "✗"} ${item.label || item.path}${item.detail ? ` — ${item.detail}` : ""}`
          )
        )
      )
    );
  } else if (status === "done") {
    body.append(
      h("p", {
        class: "muted",
        text: "本步没有可自动判定的验收项（未验证，不要当成已验证）。",
      })
    );
  }
  if (step.fetched_files?.length) {
    body.append(
      h("p", {
        class: "muted",
        text: `按需读取（未全量注入上下文）：${step.fetched_files.join("、")}`,
      })
    );
  }
  if (step.acceptance?.length) {
    body.append(h("h3", { class: "section-label", text: "验收标准" }));
    body.append(h("ul", { class: "list" }, ...step.acceptance.map((item) => h("li", { text: item }))));
  }

  // 自开发的安全网：改错了可以一键把这一步的改动还原（只动这一步碰过的文件）
  const busy = ["planning", "executing"].includes(state.run?.status);
  if ((step.files || []).length > 0 && !busy) {
    const revert = h("button", { class: "btn ghost small", type: "button", text: "回滚这一步" });
    revert.addEventListener(
      "click",
      safe(async () => {
        const ok = await appConfirm({
          title: `回滚第 ${step.id} 步`,
          message:
            "将把这一步改动过的文件还原到改动前（这一步新建的文件会被删除）。\n" +
            "其他未提交的改动不受影响。",
          confirmText: "回滚",
          danger: true,
        });
        if (!ok) return;
        const payload = await api.revertStep(state.run.id, step.id);
        state.run = payload.run;
        state.buffers = {};
        render();
        const reverted = payload.reverted || {};
        showToast(
          `已回滚：还原 ${(reverted.restored || []).length} 个、删除 ${
            (reverted.removed || []).length
          } 个文件。`
        );
      })
    );
    body.append(h("div", { class: "step-actions" }, revert));
  }

  return h(
    "div",
    { class: "card step", dataset: { step: step.id, status } },
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "step-index", text: step.id }),
      h("strong", { text: step.title || `第 ${step.id} 步` }),
      h("span", {
        class: "muted",
        text:
          (STEP_STATUS_TEXT[status] || status) +
          (step.context_chars
            ? ` · 上下文 ${(step.context_chars / 1000).toFixed(1)}k 字符`
            : "") +
          (step.verification?.length
            ? ` · 验收 ${step.verification.filter((item) => item.ok).length}/${step.verification.length}`
            : "") +
          (step.command_results?.length
            ? ` · 命令 ${
                step.command_results.filter((item) => !item.skipped && item.ok).length
              }/${step.command_results.filter((item) => !item.skipped).length} 通过`
            : ""),
      }),
      (step.files || []).length
        ? (() => {
            const link = h("button", {
              class: "btn link step-changes",
              type: "button",
              text: "看变更 →",
            });
            link.addEventListener("click", safe(() => switchInspectorTab("changes")));
            return link;
          })()
        : null,
      copyButton(
        () =>
          [
            `第 ${step.id} 步：${step.title}`,
            step.summary,
            ...(step.files || []).map(
              (file) => `${file.path} (+${file.additions}/-${file.deletions})`
            ),
            ...(step.commands || []).map((item) => `$ ${item.cmd}`),
          ]
            .filter(Boolean)
            .join("\n"),
        "复制"
      )
    ),
    body
  );
}

function renderFileRow(file) {
  const wrapper = h("div", {});
  const row = h(
    "div",
    { class: "file-row" },
    h("span", { class: `badge ${file.action}`, text: file.action }),
    h("span", { class: "file-path", text: file.path }),
    file.additions ? h("span", { class: "stat-add", text: `+${file.additions}` }) : null,
    file.deletions ? h("span", { class: "stat-del", text: `-${file.deletions}` }) : null,
    file.error ? h("span", { class: "badge delete", text: "失败" }) : null
  );
  wrapper.append(row);
  if (file.error) wrapper.append(h("div", { class: "error-box", text: file.error }));
  if (file.diff) {
    const diff = h("div", { class: "diff", hidden: true });
    for (const line of file.diff.split("\n")) {
      let cls = "diff-line";
      if (line.startsWith("@@")) cls += " hunk";
      else if (line.startsWith("+") && !line.startsWith("+++")) cls += " add";
      else if (line.startsWith("-") && !line.startsWith("---")) cls += " del";
      diff.append(h("div", { class: cls, text: line }));
    }
    row.addEventListener("click", () => {
      diff.hidden = !diff.hidden;
    });
    wrapper.append(diff);
  }
  return wrapper;
}

/* ── 检查器 ── */

function renderInspector() {
  const run = state.run;
  if (!run) {
    dom.inspectorBody.replaceChildren(h("p", { class: "muted", text: "暂无运行。" }));
    return;
  }
  if (state.tab === "plan") return renderPlanInspector(run);
  if (state.tab === "changes") return renderChangesInspector(run);
  if (state.tab === "files") return renderFilesInspector(run);
  if (state.tab === "stats") return renderStatsInspector();
  return renderDocsInspector(run);
}

/** 「统计」标签：看板从主区挪到这里，只在打开时渲染。 */
function renderStatsInspector() {
  let host = document.getElementById("run-dashboard");
  if (!host || !dom.inspectorBody.contains(host)) {
    // 复用已有的宿主，避免每次重绘都把看板内容清空再重建
    host = h("div", { class: "dashboard", id: "run-dashboard" });
    dom.inspectorBody.replaceChildren(host);
  }
  if (typeof dashboardRefresh === "function") dashboardRefresh();
}

function renderPlanInspector(run) {
  const nodes = [];
  if (run.kind === "chat") {
    const answer = (run.messages || []).find((message) => message.phase === "chat");
    nodes.push(h("p", { class: "muted", text: "这条是问答，没有纲领。" }));
    nodes.push(
      h("pre", { class: "stream", text: answer ? answer.content : "（还没有回答内容）" })
    );
    dom.inspectorBody.replaceChildren(...nodes);
    return;
  }
  if (!run.plan) {
    nodes.push(h("p", { class: "muted", text: "纲领尚未生成。" }));
  } else {
    nodes.push(h("div", { class: "kv" }, h("h3", { text: "目标" }), h("div", { text: run.plan.goal })));
    if (run.plan.summary) {
      nodes.push(h("div", { class: "kv" }, h("h3", { text: "纲领性说明" }), h("div", { text: run.plan.summary })));
    }
  }
  nodes.push(h("h3", { class: "section-label", text: "执行步骤" }));
  nodes.push(
    h(
      "div",
      { class: "checklist" },
      ...(run.steps || []).map((step) =>
        h(
          "div",
          { class: "check-item", dataset: { status: step.status || "pending" } },
          h(
            "div",
            { class: "check-title" },
            h("span", { class: "idx", text: `#${step.id}` }),
            h("span", { text: step.title }),
            h("span", { class: "muted", text: ` · ${STEP_STATUS_TEXT[step.status] || step.status}` })
          ),
          step.goal ? h("div", { class: "check-goal", text: step.goal }) : null,
          (step.acceptance || []).length
            ? h("ul", { class: "list" }, ...step.acceptance.map((item) => h("li", { text: item })))
            : null
        )
      )
    )
  );
  dom.inspectorBody.replaceChildren(...nodes);
}

function renderChangesInspector(run) {
  const files = (run.steps || []).flatMap((step) => step.files || []);
  if (!files.length) {
    dom.inspectorBody.replaceChildren(h("p", { class: "muted", text: "还没有文件变更。" }));
    return;
  }
  dom.inspectorBody.replaceChildren(...files.map((file) => renderFileRow(file)));
}

async function renderFilesInspector(run) {
  if (state.fileView) {
    const back = h("button", { class: "btn ghost", text: "← 返回文件列表" });
    back.addEventListener("click", () => {
      state.fileView = null;
      renderInspector();
    });
    dom.inspectorBody.replaceChildren(
      back,
      h("h3", { class: "section-label", text: state.fileView.path }),
      h("pre", { class: "stream", text: state.fileView.content })
    );
    return;
  }
  dom.inspectorBody.replaceChildren(h("p", { class: "muted", text: "加载中…" }));
  let payload;
  try {
    payload = await api.tree(run.id);
  } catch (error) {
    dom.inspectorBody.replaceChildren(h("p", { class: "muted", text: error.message }));
    return;
  }
  if (!payload.files.length) {
    dom.inspectorBody.replaceChildren(h("p", { class: "muted", text: "工作区暂无文件。" }));
    return;
  }
  dom.inspectorBody.replaceChildren(
    h("h3", { class: "section-label", text: `工作区：${payload.root}` }),
    ...payload.files.map((path) => {
      const row = h("div", { class: "file-row" }, h("span", { class: "file-path", text: path }));
      row.addEventListener("click", async () => {
        try {
          const file = await api.file(run.id, path);
          state.fileView = file;
          renderInspector();
        } catch (error) {
          showToast(error.message);
        }
      });
      return row;
    })
  );
}

async function loadDocs() {
  if (!state.run) return;
  try {
    const payload = await api.docs(state.run.id);
    state.docs = payload.docs || [];
    if (state.tab === "docs") renderInspector();
  } catch (error) {
    state.docs = [];
  }
}

function renderDocsInspector() {
  if (!state.docs.length) {
    dom.inspectorBody.replaceChildren(h("p", { class: "muted", text: "尚无纲领/报告文档。" }));
    return;
  }
  dom.inspectorBody.replaceChildren(
    ...state.docs.map((doc) =>
      h("div", { class: "kv" }, h("h3", { class: "section-label", text: doc.name }), h("div", { class: "doc", text: doc.content }))
    )
  );
}

/* ── 输入区 ── */

function updateComposer() {
  const ready = state.settings?.ready !== false;
  const status = state.run?.status || "idle";
  const busy = status === "planning" || status === "executing";
  // 未配置时**不禁用**按钮：点击会给出"去哪儿配"的提示，而不是"点了没反应"
  dom.sendBtn.disabled = false;
  dom.cancelBtn.hidden = !busy;
  // 输入框始终保持可用：运行中也允许选中/复制/粘贴，只是发送会被拦下并提示
  if (!ready) {
    const problem = state.settings?.problems?.[0];
    dom.composerHint.textContent = problem
      ? `配置待修正：${problem}`
      : "尚未配置中转地址 / Key：点右上角 ⚙ 填写并保存后即可开始。";
  } else if (status === "planning") {
    dom.composerHint.textContent = "架构段运行中，纲领生成后需要你确认才会执行。";
  } else if (status === "executing") {
    dom.composerHint.textContent = "执行段运行中…";
  } else if (status === "awaiting_approval") {
    dom.composerHint.textContent = "纲领已生成：可在上方确认执行，或输入新需求另起一个任务。";
  } else if (status === "blocked") {
    dom.composerHint.textContent =
      "执行段被阻塞：在时间线顶部补充信息（或指定目录）即可只重跑那一步。";
  } else {
    dom.composerHint.textContent = "Ctrl+Enter 发送 · 将开启一个新的架构→执行流程";
  }
}

async function submitTask() {
  const task = dom.taskInput.value.trim();
  const status = state.run?.status || "idle";
  const targetDir = dom.targetDir.value.trim();
  if (status === "planning" || status === "executing") {
    showToast("当前任务正在运行：等它结束，或点「停止」后再提交新任务。");
    return;
  }
  if (state.settings && state.settings.ready === false) {
    showToast("尚未配置中转地址 / Key：点右上角 ⚙ 填写并保存后再发送。");
    return;
  }
  if (!task) {
    showToast("请先输入目标描述。");
    return;
  }
  // 提到"现有代码"却没给目录 → 执行段看不到你的代码，先提醒一次
  if (!targetDir && /(现有|已有|既有|当前|仓库|代码库|重构|改造|迁移|修复|对接|集成|盘点)/.test(task)) {
    const ok = await appConfirm({
      title: "看起来要改现有代码",
      message:
        "「落地目录」是空的：本次会从全新空工作区开始，执行段看不到你现有的代码。\n\n" +
        "建议先填上项目目录；也可以继续，之后在阻塞提示里补充。",
      confirmText: "仍然继续",
    });
    if (!ok) return;
  }
  dom.sendBtn.disabled = true;
  try {
    const payload = await api.createRun({
      task,
      target_dir: targetDir,
      context: "",
    });
    dom.taskInput.value = "";
    state.run = payload.run;
    state.buffers = {};
    state.lastSeq = 0;
    localStorage.setItem(STORAGE_KEY, payload.run.id);
    render();
    connectStream(payload.run.id, state.lastSeq);
    await refreshRuns();
  } catch (error) {
    showToast(error.message);
  } finally {
    dom.sendBtn.disabled = false;
    updateComposer();
  }
}

function startNewRun() {
  disconnectStream();
  state.run = null;
  state.buffers = {};
  state.fileView = null;
  state.docs = [];
  localStorage.removeItem(STORAGE_KEY);
  render();
  dom.taskInput.focus();
}

/* ── 设置 ── */

/** 设置分区切换（模型与路由 / 命令与安全 / 行为与上下文 / 插件与 Git）。 */
function switchSettingsSection(section) {
  const target = section || "model";
  state.settingsSection = target;
  document.querySelectorAll("#settings-nav .settings-nav-item").forEach((node) => {
    node.classList.toggle("active", node.dataset.section === target);
  });
  document.querySelectorAll(".settings-section").forEach((node) => {
    node.hidden = node.dataset.section !== target;
  });
}

function openSettings(section = "model") {
  const settings = state.settings;
  if (!settings) return;
  document.getElementById("f-max-steps").value = settings.max_plan_steps || 8;
  document.getElementById("f-allow-cmd").checked = Boolean(settings.allow_command_execution);
  document.getElementById("f-command-allowlist").value = (
    settings.command_allowlist || []
  ).join("\n");
  document.getElementById("f-command-rounds").value = settings.step_command_rounds ?? 2;
  switchSettingsSection(section || state.settingsSection || "model");
  document.getElementById("settings-modal").hidden = false;
  refreshProviders().then(() => {
    const target = state.editingProviderId || state.activeProviderId;
    selectProvider(target || state.providers[0]?.id || "");
    loadRouteForm();
    renderSettingsStatus(state.settings, "");
  });
}

function closeSettings() {
  document.getElementById("settings-modal").hidden = true;
}

/** 一键配置自开发：白名单 = 本项目质量门，并打开命令执行。 */
async function applyDevPreset() {
  const status = document.getElementById("settings-status");
  try {
    const payload = await api.devPreset();
    applySettingsPayload(payload);
    document.getElementById("f-allow-cmd").checked = Boolean(payload.allow_command_execution);
    document.getElementById("f-command-allowlist").value = (payload.command_allowlist || []).join(
      "\n"
    );
    document.getElementById("f-command-rounds").value = payload.step_command_rounds ?? 2;
    status.style.color = "var(--ok)";
    status.textContent = `已配置自开发：白名单 ${(payload.command_allowlist || []).length} 条。`;
  } catch (error) {
    status.style.color = "var(--danger)";
    status.textContent = error.message;
  }
}

/** 开发模式：把落地目录指向本仓库，避免每次手填。 */
async function applyDevMode(checked) {
  const field = document.getElementById("target-dir");
  if (!checked) {
    field.value = "";
    showToast("已退出开发模式。");
    return;
  }
  try {
    const info = await api.systemInfo();
    field.value = info.project_root || "";
    showToast(
      `开发模式：改动会直接落在 ${info.project_root}${
        info.is_git_repo ? "（已检测到 git，可一键回滚）" : "（该目录不是 git 仓库，回滚只能用备份）"
      }`
    );
  } catch (error) {
    document.getElementById("dev-mode").checked = false;
    showToast(error.message);
  }
}

/* ── 分段路由（右上角标签点开的弹窗）── */

/**
 * 打开分段路由弹窗。
 *
 * ``stage``：``"architect"`` / ``"editor"`` = 只编辑那一段（点顶栏标签进来的默认行为）；
 * ``"both"`` = 两段一起改（点 ✎ 进来）。
 */
async function openRouteModal(stage = "both") {
  const modal = document.getElementById("route-modal");
  if (!modal) return;
  document.getElementById("route-status").textContent = "";
  // 配置列表可能还没加载（刚启动就点右上角），补一次再填表单
  if (!state.providers.length) {
    try {
      applySettingsPayload(await api.settings());
    } catch (error) {
      reportClientError("route", error, { step: "load settings" });
    }
  }
  fillRouteSelects();
  const routes = state.settings?.routes || {};
  const architect = routes.architect || {};
  const editor = routes.editor || {};
  document.getElementById("route-architect-provider").value = architect.provider_id || "";
  document.getElementById("route-architect-model").value = architect.model || "";
  document.getElementById("route-editor-provider").value = editor.provider_id || "";
  document.getElementById("route-editor-model").value = editor.model || "";
  renderRouteStage(stage);
  modal.hidden = false;
}

/** 只显示当前要改的那一段；另一段留一个切换入口，不一起挤在眼前。 */
function renderRouteStage(stage) {
  const view = stage === "architect" || stage === "editor" ? stage : "both";
  state.routeStage = view;
  // 注意：id 在遮罩层上，控制显示的是内层 .route-modal（CSS 按它匹配）
  const dialog = document.querySelector("#route-modal .route-modal") || document.getElementById("route-modal");
  dialog.dataset.stage = view;
  const titles = {
    architect: "架构段：用哪套配置 / 哪个模型",
    editor: "执行段：用哪套配置 / 哪个模型",
    both: "分段路由：架构段 → 执行段",
  };
  document.getElementById("route-title").textContent = titles[view];
  document.getElementById("route-switch").textContent =
    view === "architect" ? "也改执行段 →" : view === "editor" ? "← 改架构段" : "";
  document.getElementById("route-switch").hidden = view === "both";
  // 打开就聚焦到该段第一个输入，键盘用户可以直接改
  const focusId = view === "editor" ? "route-editor-provider" : "route-architect-provider";
  if (view !== "both") document.getElementById(focusId).focus();
}

function closeRouteModal() {
  document.getElementById("route-modal").hidden = true;
}

/** 两个下拉：空值 = "跟随当前配置"。 */
function fillRouteSelects() {
  for (const elementId of ["route-architect-provider", "route-editor-provider"]) {
    const select = document.getElementById(elementId);
    const previous = select.value;
    select.replaceChildren(
      h("option", { value: "", text: "（跟随当前配置）" }),
      ...state.providers.map((profile) => h("option", { value: profile.id, text: profile.name }))
    );
    select.value = previous;
  }
}

async function saveRouteModal() {
  const status = document.getElementById("route-status");
  const saveBtn = document.getElementById("route-save");
  saveBtn.disabled = true;
  status.textContent = "";
  try {
    const payload = await api.routes({
      architect: {
        provider_id: document.getElementById("route-architect-provider").value,
        model: document.getElementById("route-architect-model").value.trim(),
      },
      editor: {
        provider_id: document.getElementById("route-editor-provider").value,
        model: document.getElementById("route-editor-model").value.trim(),
      },
    });
    applySettingsPayload(payload);
    updateRouteChips();
    closeRouteModal();
    showToast("分段路由已更新。");
  } catch (error) {
    status.textContent = error.message;
  } finally {
    saveBtn.disabled = false;
  }
}

/** 一键回到"两段都跟随当前配置"。 */
async function followCurrentProvider() {
  const status = document.getElementById("route-status");
  try {
    const payload = await api.routes({
      architect: { provider_id: "", model: "" },
      editor: { provider_id: "", model: "" },
    });
    applySettingsPayload(payload);
    updateRouteChips();
    closeRouteModal();
    showToast("两段都跟随当前配置。");
  } catch (error) {
    status.textContent = error.message;
  }
}

/* ── 配置（Provider）：中转 / 个人 Key 直连，可多套切换 ── */

function providerOptionLabel(profile) {
  const host = (profile.base_url || "").replace(/^https?:\/\//, "").split("/")[0];
  const activeMark = profile.id === state.activeProviderId ? "● " : "";
  const incomplete = profile.complete ? "" : "（未完成）";
  return `${activeMark}${profile.name}${incomplete}${host ? " · " + host : ""}`;
}

function applySettingsPayload(payload) {
  state.settings = payload;
  state.providers = payload.providers || [];
  state.activeProviderId = payload.active_provider?.id || "";
  renderProviderSelects();
  render();
}

function renderProviderSelects() {
  const quick = document.getElementById("provider-quick");
  if (state.providers.length) {
    quick.replaceChildren(
      ...state.providers.map((profile) =>
        h("option", { value: profile.id, text: providerOptionLabel(profile) })
      )
    );
    quick.value = state.activeProviderId;
    quick.disabled = false;
  } else {
    quick.replaceChildren(h("option", { value: "", text: "未配置（点设置添加）" }));
    quick.disabled = true;
  }
  const select = document.getElementById("f-provider-select");
  select.replaceChildren(
    ...state.providers.map((profile) =>
      h("option", { value: profile.id, text: providerOptionLabel(profile) })
    )
  );
  if (state.editingProviderId) select.value = state.editingProviderId;
  renderRouteSelects();
}

/** 分段路由的两个下拉：空值 = "跟随当前配置"。 */
function renderRouteSelects() {
  for (const elementId of ["f-architect-provider", "f-editor-provider"]) {
    const select = document.getElementById(elementId);
    const previous = select.value;
    select.replaceChildren(
      h("option", { value: "", text: "（跟随当前配置）" }),
      ...state.providers.map((profile) =>
        h("option", { value: profile.id, text: profile.name })
      )
    );
    select.value = previous;
  }
}

/** 把已保存的分段路由填进表单。 */
function loadRouteForm() {
  const routes = state.settings?.routes || {};
  const architect = routes.architect || {};
  const editor = routes.editor || {};
  document.getElementById("f-architect-provider").value = architect.provider_id || "";
  document.getElementById("f-architect-model-route").value = architect.model || "";
  document.getElementById("f-editor-provider").value = editor.provider_id || "";
  document.getElementById("f-editor-model-route").value = editor.model || "";
  const split = Boolean(architect.provider_id || editor.provider_id);
  const toggle = document.getElementById("f-split-mode");
  toggle.checked = split;
  document.getElementById("split-fields").hidden = !split;
}

/** 保存分段路由；分段模式关闭时清空路由（两段都跟随当前配置）。 */
async function saveRoutes() {
  const split = document.getElementById("f-split-mode").checked;
  if (!split) {
    const payload = await api.routes({
      architect: { provider_id: "", model: "" },
      editor: { provider_id: "", model: "" },
    });
    applySettingsPayload(payload);
    return;
  }
  const payload = await api.routes({
    architect: {
      provider_id: document.getElementById("f-architect-provider").value,
      model: document.getElementById("f-architect-model-route").value.trim(),
    },
    editor: {
      provider_id: document.getElementById("f-editor-provider").value,
      model: document.getElementById("f-editor-model-route").value.trim(),
    },
  });
  applySettingsPayload(payload);
}

/** 把某套配置填进表单；keepKey=true 时保留用户刚输入的 Key（避免"Key 消失"的错觉）。 */
function selectProvider(providerId, keepKey = false) {
  const profile = state.providers.find((item) => item.id === providerId) || null;
  state.editingProviderId = profile ? profile.id : "";
  const setValue = (elementId, value) => {
    document.getElementById(elementId).value = value || "";
  };
  setValue("f-provider-name", profile ? profile.name : "");
  setValue("f-provider-kind", profile ? profile.kind : "relay");
  setValue("f-relay-base", profile ? profile.base_url : "");
  setValue("f-relay-wire", profile ? profile.wire_api : "chat_completions");
  setValue("f-architect-model", profile ? profile.architect_model : "");
  setValue("f-editor-model", profile ? profile.editor_model : "");
  if (!keepKey) document.getElementById("f-relay-key").value = "";

  const keyField = document.getElementById("f-relay-key");
  keyField.placeholder =
    profile && profile.api_key_set
      ? `已保存：${profile.api_key_masked}（留空表示不修改，粘贴新 Key 直接覆盖）`
      : "sk-xxxx（填 Key，不是地址）";

  const hint = document.getElementById("provider-hint");
  if (!profile) {
    hint.textContent = "还没有配置：点「＋ 新建」创建一套（中转或个人 Key 直连）。";
  } else if (profile.id === state.activeProviderId) {
    hint.textContent = profile.complete
      ? "这是当前正在使用的配置。"
      : "当前配置还没填完（地址 / Key / 两个模型）。";
  } else {
    hint.textContent = "这是备用配置：点「设为当前」即可切换，切换后新任务立即使用它。";
  }
  renderPresets();
  renderProviderSelects();
}

function renderPresets() {
  const kind = document.getElementById("f-provider-kind").value || "relay";
  const list = (state.presets || {})[kind] || [];
  const row = document.getElementById("provider-presets");
  row.replaceChildren(
    ...list
      .filter((item) => item.base_url)
      .map((item) => {
        const chip = h("button", { class: "preset-chip", type: "button", text: item.label });
        chip.addEventListener("click", () => {
          document.getElementById("f-relay-base").value = item.base_url;
        });
        return chip;
      })
  );
}

async function refreshProviders() {
  const token = ++state.tokens.providers;
  try {
    const payload = await api.providers();
    if (token !== state.tokens.providers) return;
    state.providers = payload.providers || [];
    state.activeProviderId = payload.active_provider_id || "";
    state.presets = payload.presets || {};
    if (!state.editingProviderId && state.activeProviderId) {
      state.editingProviderId = state.activeProviderId;
    }
    renderProviderSelects();
  } catch (error) {
    showToast(error.message);
  }
}

function collectProviderForm() {
  return {
    name: document.getElementById("f-provider-name").value.trim() || "未命名配置",
    kind: document.getElementById("f-provider-kind").value,
    base_url: document.getElementById("f-relay-base").value.trim(),
    api_key: document.getElementById("f-relay-key").value.trim(),
    wire_api: document.getElementById("f-relay-wire").value,
    architect_model: document.getElementById("f-architect-model").value.trim(),
    editor_model: document.getElementById("f-editor-model").value.trim(),
  };
}

/** 保存当前表单：先存这套配置，再存全局选项。返回被保存的配置 id。 */
async function saveProviderForm() {
  const data = collectProviderForm();
  const globals = {
    max_plan_steps: Number(document.getElementById("f-max-steps").value) || 8,
    allow_command_execution: document.getElementById("f-allow-cmd").checked,
    command_allowlist: String(document.getElementById("f-command-allowlist").value || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
    step_command_rounds: Number(document.getElementById("f-command-rounds").value) || 0,
  };
  const result = state.editingProviderId
    ? await api.updateProvider(state.editingProviderId, data)
    : await api.createProvider({ ...data, activate: false });
  const settingsPayload = await api.saveSettings(globals);
  applySettingsPayload({ ...settingsPayload, provider_id: result.provider_id });
  const savedId = result.provider_id || state.editingProviderId;
  state.editingProviderId = savedId;
  // 分段路由与配置一起保存：架构段/执行段可各自指向不同配置
  await saveRoutes();
  return savedId;
}

function renderSettingsStatus(settings, prefix) {
  const status = document.getElementById("settings-status");
  const problems = settings.problems || [];
  const keyText = settings.relay_api_key_set
    ? `当前 Key：${settings.relay_api_key_masked}`
    : "尚未设置 Key";
  if (problems.length) {
    status.style.color = "var(--danger)";
    status.textContent = `${prefix}${problems[0]}`;
    return;
  }
  status.style.color = prefix.includes("测试通过") ? "var(--ok)" : "var(--muted)";
  status.textContent = `${prefix}${keyText}`;
}

async function saveSettings() {
  const status = document.getElementById("settings-status");
  status.style.color = "var(--muted)";
  status.textContent = "保存中…";
  const typedKey = document.getElementById("f-relay-key").value;
  try {
    const savedId = await saveProviderForm();
    selectProvider(savedId, true);
    document.getElementById("f-relay-key").value = typedKey;
    renderSettingsStatus(state.settings, "已保存 · ");
  } catch (error) {
    status.style.color = "var(--danger)";
    status.textContent = error.message;
  }
}

async function testConnection() {
  const status = document.getElementById("settings-status");
  const result = document.getElementById("settings-test-result");
  status.textContent = "";
  result.style.color = "var(--muted)";
  result.textContent = "正在保存并测试…";
  const typedKey = document.getElementById("f-relay-key").value;
  try {
    // 先把当前填写内容存下来，保证"测试的就是要用的配置"
    const savedId = await saveProviderForm();
    selectProvider(savedId, true);
    document.getElementById("f-relay-key").value = typedKey;
    const payload = await api.testProvider(savedId);
    const lines = Object.values(payload.results).map((item) => {
      const who = item.model ? `${item.model}` : "端点";
      if (item.ok) {
        const count = item.model_count ? `${item.model_count} 个模型` : "已连通";
        const missing = item.has_target_model === false ? "，但模型列表中没有该模型" : "";
        return `✓ ${who}：${count}${missing}`;
      }
      return `✗ ${who}：${item.message || "失败"}`;
    });
    result.style.color = payload.ok ? "var(--ok)" : "var(--danger)";
    result.textContent = lines.join("　|　");
    renderSettingsStatus(state.settings, payload.ok ? "测试通过 · " : "");
  } catch (error) {
    result.style.color = "var(--danger)";
    result.textContent = error.message;
  }
}

/* ── 配置的增删与切换 ── */

async function newProvider() {
  try {
    const payload = await api.createProvider({
      name: "新配置",
      kind: "relay",
      activate: false,
    });
    applySettingsPayload(payload);
    state.editingProviderId = payload.provider_id;
    selectProvider(payload.provider_id);
    document.getElementById("settings-status").textContent =
      "已新建配置：填好地址 / Key / 两个模型，点「保存」，再点「设为当前」。";
  } catch (error) {
    showToast(error.message);
  }
}

async function copyProvider() {
  try {
    const data = collectProviderForm();
    const payload = await api.createProvider({
      ...data,
      name: `${data.name} 副本`,
      activate: false,
    });
    applySettingsPayload(payload);
    state.editingProviderId = payload.provider_id;
    selectProvider(payload.provider_id);
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteProvider() {
  const profile = state.providers.find((item) => item.id === state.editingProviderId);
  if (!profile) {
    showToast("没有可删除的配置。");
    return;
  }
  const confirmed = await appConfirm({
    title: "删除配置",
    message: `确定删除配置「${profile.name}」？此操作不影响已保存的其他配置。`,
    confirmText: "删除",
    danger: true,
  });
  if (!confirmed) return;
  try {
    const payload = await api.deleteProvider(profile.id);
    state.editingProviderId = "";
    applySettingsPayload(payload);
    selectProvider(state.activeProviderId || state.providers[0]?.id || "");
  } catch (error) {
    showToast(error.message);
  }
}

async function activateProvider() {
  const status = document.getElementById("settings-status");
  try {
    const savedId = await saveProviderForm();
    const payload = await api.activateProvider(savedId);
    state.editingProviderId = savedId;
    applySettingsPayload(payload);
    selectProvider(savedId, true);
    status.style.color = "var(--ok)";
    const profile = state.providers.find((item) => item.id === savedId);
    status.textContent = `已切换为「${profile ? profile.name : savedId}」，新任务将使用它。`;
  } catch (error) {
    status.style.color = "var(--danger)";
    status.textContent = error.message;
  }
}

/** 侧栏一键切换：选中即激活。 */
async function quickSwitchProvider(providerId) {
  if (!providerId || providerId === state.activeProviderId) return;
  try {
    const payload = await api.activateProvider(providerId);
    applySettingsPayload(payload);
    const profile = state.providers.find((item) => item.id === providerId);
    showToast(`已切换到「${profile ? profile.name : providerId}」`);
  } catch (error) {
    showToast(error.message);
    renderProviderSelects();
  }
}

/* ── 提示 ── */

/* ── 应用内对话框（替代 window.confirm / prompt）──
   某些 WebView（含打包后的桌面客户端）对原生 confirm 支持不一致：
   调用后可能一直不返回，表现为"点了没反应"。所以全部改成页内实现。 */

function appConfirm({ title = "确认操作", message = "", confirmText = "确定", danger = false } = {}) {
  return new Promise((resolve) => {
    const modal = document.getElementById("confirm-modal");
    if (!modal) {
      resolve(true); // 极端情况下不阻塞主流程
      return;
    }
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-message").textContent = message;
    const ok = document.getElementById("confirm-ok");
    const cancel = document.getElementById("confirm-cancel");
    ok.textContent = confirmText;
    ok.classList.toggle("danger", Boolean(danger));

    const finish = (value) => {
      modal.hidden = true;
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      resolve(value);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    modal.hidden = false;
    ok.focus();
  });
}

function appPrompt({ title = "输入", label = "", value = "", confirmText = "确定" } = {}) {
  return new Promise((resolve) => {
    const modal = document.getElementById("confirm-modal");
    if (!modal) {
      resolve(value);
      return;
    }
    document.getElementById("confirm-title").textContent = title;
    const message = document.getElementById("confirm-message");
    message.replaceChildren();
    const input = h("input", { type: "text", value });
    input.style.width = "100%";
    message.append(label ? h("div", { class: "muted-small", text: label }) : "", input);

    const ok = document.getElementById("confirm-ok");
    const cancel = document.getElementById("confirm-cancel");
    ok.textContent = confirmText;

    const finish = (result) => {
      modal.hidden = true;
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      input.removeEventListener("keydown", onKey);
      resolve(result);
    };
    const onOk = () => finish(input.value.trim() || value);
    const onCancel = () => finish(null);
    const onKey = (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        onOk();
      }
    };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    input.addEventListener("keydown", onKey);
    modal.hidden = false;
    input.focus();
    input.select();
  });
}

/* ── 左侧任务栏形态（展开 / 折叠 rail，参照 dsh-desktop 的 wide 契约）── */

function applySidebarMode() {
  const sidebar = document.getElementById("sidebar");
  sidebar.dataset.wide = state.sidebarWide ? "true" : "false";
  document.querySelector(".app").classList.toggle("collapsed", !state.sidebarWide);
  const toggle = document.getElementById("sidebar-toggle");
  toggle.textContent = state.sidebarWide ? "⇤" : "⇥";
  toggle.title = state.sidebarWide ? "折叠左侧任务栏" : "展开左侧任务栏";
}

function showVersionInfo() {
  const tag = document.querySelector(".build-tag")?.textContent || "";
  showToast(
    `${tag}　更新方式：双击 stop.cmd 后重新运行 start.cmd（或 git pull 后重启）。`
  );
}

/* ── Git 面板（简化版 IDEA Git 工具窗）──────────────────────── */

async function openGitPanel() {
  const modal = document.getElementById("git-modal");
  if (!modal) return;
  modal.hidden = false;
  document.getElementById("git-body").replaceChildren(h("p", { class: "muted", text: "读取仓库状态…" }));
  await renderGitPanel();
}

function closeGitPanel() {
  const modal = document.getElementById("git-modal");
  if (modal) modal.hidden = true;
}

async function renderGitPanel() {
  const body = document.getElementById("git-body");
  let status;
  let history = { commits: [], branches: { current: "", all: [] } };
  let auto;
  try {
    [status, history, auto] = await Promise.all([
      api.gitStatus(),
      api.gitLog(),
      api.gitAutoCommit(),
    ]);
  } catch (error) {
    body.replaceChildren(h("div", { class: "error-box", text: error.message }));
    return;
  }
  if (!status.is_repo) {
    body.replaceChildren(
      h("p", { class: "muted", text: `当前目录不是 git 仓库：${status.repo}` })
    );
    return;
  }
  document.getElementById("git-summary").textContent =
    `${status.branch}　↑${status.ahead} ↓${status.behind}　${status.remote || "无远端"}`;

  const nodes = [];

  // 顶部操作
  const actions = h("div", { class: "market-toolbar" });
  for (const [label, handler] of [
    ["刷新", async () => renderGitPanel()],
    ["拉取", async () => runGitAction(() => api.gitPull(), "拉取")],
    ["推送", async () => runGitAction(() => api.gitPush(), "推送")],
  ]) {
    const button = h("button", { class: "btn ghost", text: label });
    button.addEventListener("click", safe(handler));
    actions.append(button);
  }
  nodes.push(actions);

  // 代理：Windows 系统代理与 git 不通用，这里给按钮直接用
  const proxy = await api.gitProxyStatus().catch(() => null);
  if (proxy) {
    const proxyInput = h("input", { type: "text", placeholder: "http://127.0.0.1:10809" });
    proxyInput.value = proxy.git_proxy || proxy.system_proxy || "";
    const useSystem = h("button", { class: "btn ghost", text: "使用系统代理" });
    useSystem.addEventListener(
      "click",
      safe(async () => {
        const result = await api.gitProxySet("");
        showToast(`git 已走代理：${result.git_proxy}`);
        await renderGitPanel();
      })
    );
    const applyProxy = h("button", { class: "btn ghost", text: "设为代理" });
    applyProxy.addEventListener(
      "click",
      safe(async () => {
        const result = await api.gitProxySet(proxyInput.value.trim());
        showToast(`git 已走代理：${result.git_proxy}`);
        await renderGitPanel();
      })
    );
    const clearProxy = h("button", { class: "btn ghost", text: "清除代理" });
    clearProxy.addEventListener(
      "click",
      safe(async () => {
        await api.gitProxyClear();
        showToast("已清除 git 代理（回到直连）");
        await renderGitPanel();
      })
    );
    nodes.push(
      h(
        "details",
        { class: "git-section" },
        h("summary", { text: "网络代理" }),
        h("div", {
          class: "muted-small",
          text: proxy.active
            ? `当前 git 代理：${proxy.git_proxy}（${proxy.scoped ? "仅 github.com" : "全局"}）`
            : `未配置 git 代理${proxy.system_proxy ? `；系统代理为 ${proxy.system_proxy}` : ""}`,
        }),
        h("div", { class: "market-toolbar" }, proxyInput, useSystem, applyProxy, clearProxy)
      )
    );
  }

  // 分支：切换 / 新建
  const branchBox = h("div", { class: "check-item" });
  const branchSelect = h(
    "select",
    {},
    ...(history.branches.all || []).map((name) =>
      h("option", { value: name, text: name === history.branches.current ? `${name}（当前）` : name })
    )
  );
  const switchButton = h("button", { class: "btn ghost", text: "切换分支" });
  switchButton.addEventListener(
    "click",
    safe(async () => {
      const result = await api.gitCheckout(branchSelect.value, false);
      showToast(result.ok ? `已切换到 ${branchSelect.value}` : result.hint || "切换失败");
      await renderGitPanel();
    })
  );
  const newBranchInput = h("input", { type: "text", placeholder: "新分支名，如 feature/git-panel" });
  const createButton = h("button", { class: "btn ghost", text: "新建并切换" });
  createButton.addEventListener(
    "click",
    safe(async () => {
      const name = newBranchInput.value.trim();
      if (!name) {
        showToast("请先填分支名。");
        return;
      }
      const result = await api.gitCheckout(name, true);
      showToast(result.ok ? `已创建并切换到 ${name}` : result.hint || "创建失败");
      await renderGitPanel();
    })
  );
  branchBox.append(
    h("div", { class: "muted-small", text: `当前分支：${history.branches.current}` }),
    h("div", { class: "market-toolbar" }, branchSelect, switchButton),
    h("div", { class: "market-toolbar" }, newBranchInput, createButton)
  );
  nodes.push(
    h(
      "details",
      { class: "git-section" },
      h("summary", { text: `分支（当前 ${history.branches.current}）` }),
      branchBox
    )
  );

  // 变更列表
  const selected = new Set();
  const selectedPaths = () => [...selected];
  nodes.push(h("h3", { class: "section-label", text: `变更（${status.files.length}）` }));
  if (!status.files.length) {
    nodes.push(h("p", { class: "muted-small", text: "工作区干净，没有待提交的改动。" }));
  } else {
    const batch = h("div", { class: "market-toolbar" });
    const stageAll = h("button", { class: "btn ghost", text: "全部暂存" });
    stageAll.addEventListener("click", safe(async () => {
      await api.gitStage(status.files.map((file) => file.path));
      showToast("已全部暂存");
      await renderGitPanel();
    }));
    const unstageAll = h("button", { class: "btn ghost", text: "全部取消暂存" });
    unstageAll.addEventListener("click", safe(async () => {
      await api.gitUnstage(status.files.map((file) => file.path));
      showToast("已全部取消暂存");
      await renderGitPanel();
    }));
    const stageSelected = h("button", { class: "btn ghost", text: "暂存选中" });
    stageSelected.addEventListener("click", safe(async () => {
      if (!selected.size) { showToast("请先勾选文件。"); return; }
      await api.gitStage(selectedPaths());
      await renderGitPanel();
    }));
    const discardSelected = h("button", { class: "btn ghost", text: "丢弃选中改动" });
    discardSelected.addEventListener("click", safe(async () => {
      if (!selected.size) { showToast("请先勾选文件。"); return; }
      const ok = await appConfirm({
        title: "丢弃改动",
        message: `将丢弃 ${selected.size} 个文件的改动：已跟踪文件回滚到上次提交，未跟踪文件会被删除。此操作不可撤销。`,
        confirmText: "丢弃",
        danger: true,
      });
      if (!ok) return;
      const result = await api.gitDiscard(selectedPaths());
      showToast(`已丢弃 ${result.discarded.length} 个文件的改动`);
      await renderGitPanel();
    }));
    batch.append(stageAll, unstageAll, stageSelected, discardSelected);
    // 批量操作收进折叠块：默认视图只留主操作，避免一屏全是按钮
    nodes.push(
      h(
        "details",
        { class: "git-section" },
        h("summary", { text: `批量操作（暂存 / 丢弃，共 ${status.files.length} 个改动）` }),
        batch
      )
    );

    // 文件列表：一行一个文件，行内只留一个「⋯」菜单——
    // 以前每个文件铺 3 个按钮，改动一多就是几十个按钮的墙。
    const list = h("div", { class: "git-files" });
    for (const file of status.files) {
      const check = h("input", { type: "checkbox" });
      check.addEventListener("change", () => {
        if (check.checked) selected.add(file.path);
        else selected.delete(file.path);
      });
      const diff = h("div", { class: "diff", hidden: true });
      const fillDiff = async () => {
        if (diff.childElementCount) return;
        const payload = await api.gitDiff(file.path);
        for (const line of (payload.diff || "（无差异）").split("\n")) {
          let cls = "diff-line";
          if (line.startsWith("@@")) cls += " hunk";
          else if (line.startsWith("+") && !line.startsWith("+++")) cls += " add";
          else if (line.startsWith("-") && !line.startsWith("---")) cls += " del";
          diff.append(h("div", { class: cls, text: line }));
        }
      };
      const toggleDiff = safe(async (event) => {
        event?.stopPropagation();
        if (!diff.hidden) {
          diff.hidden = true;
          return;
        }
        await fillDiff();
        diff.hidden = false;
      });

      const seeDiff = h("button", { class: "git-menu-item", type: "button", text: "看差异" });
      seeDiff.addEventListener("click", toggleDiff);
      const stageToggle = h("button", {
        class: "git-menu-item",
        type: "button",
        text: file.staged ? "取消暂存" : "暂存",
      });
      stageToggle.addEventListener(
        "click",
        safe(async (event) => {
          event.stopPropagation();
          if (file.staged) await api.gitUnstage([file.path]);
          else await api.gitStage([file.path]);
          await renderGitPanel();
        })
      );
      const discardOne = h("button", { class: "git-menu-item danger", type: "button", text: "丢弃改动" });
      discardOne.addEventListener(
        "click",
        safe(async (event) => {
          event.stopPropagation();
          const ok = await appConfirm({
            title: "丢弃这个文件的改动",
            message: `确定丢弃 ${file.path} 的改动？已跟踪文件回滚到上次提交，未跟踪文件会被删除。`,
            confirmText: "丢弃",
            danger: true,
          });
          if (!ok) return;
          await api.gitDiscard([file.path]);
          showToast(`已丢弃 ${file.path}`);
          await renderGitPanel();
        })
      );

      const head = h(
        "div",
        { class: "git-file-head" },
        check,
        h("span", { class: "badge", text: file.label }),
        h("span", { class: "file-path", title: "点击展开差异", text: file.path }),
        h("span", { class: "muted-small", text: file.staged ? "已暂存" : "未暂存" }),
        h(
          "details",
          { class: "git-file-menu" },
          h("summary", { title: "更多操作", text: "⋯" }),
          h("div", { class: "git-file-menu-body" }, seeDiff, stageToggle, discardOne)
        )
      );
      head.querySelector(".file-path").addEventListener("click", toggleDiff);
      list.append(h("div", { class: "git-file", dataset: { file: file.path } }, head, diff));
    }
    nodes.push(list);
  }

  // 提交
  const message = h("textarea", {
    class: "feedback",
    placeholder: "提交信息，例如：feat: 增加 Git 面板与每日自动提交开关",
  });
  const commitButton = h("button", {
    class: "btn primary",
    text: `提交全部改动（${status.files.length} 个文件）`,
  });
  commitButton.disabled = !status.files.length;
  commitButton.addEventListener(
    "click",
    safe(async () => {
      commitButton.disabled = true;
      try {
        const result = await api.gitCommit({ message: message.value.trim(), paths: [], add_all: true });
        showToast(
          result.committed
            ? `已提交：${result.commit?.hash || ""} ${result.commit?.subject || ""}`
            : result.reason || "没有可提交的改动"
        );
        await renderGitPanel();
      } finally {
        commitButton.disabled = false;
      }
    })
  );
  const commitSelectedButton = h("button", { class: "btn ghost", text: "只提交已暂存的文件" });
  commitSelectedButton.addEventListener(
    "click",
    safe(async () => {
      const text = message.value.trim();
      if (!text) {
        showToast("请先填写提交信息。");
        return;
      }
      const result = await api.gitCommit({ message: text, paths: [], add_all: false });
      showToast(result.committed ? `已提交：${result.commit?.subject || ""}` : result.reason || "没有暂存的改动");
      await renderGitPanel();
    })
  );
  nodes.push(
    h("h3", { class: "section-label", text: "提交" }),
    message,
    h("div", { class: "approval-actions" }, commitButton, commitSelectedButton)
  );

  // 每日开机自动提交
  const toggle = h("input", { type: "checkbox" });
  toggle.checked = Boolean(auto.enabled);
  toggle.addEventListener(
    "change",
    safe(async () => {
      try {
        const result = toggle.checked
          ? await api.gitAutoCommitEnable(pushInput.checked)
          : await api.gitAutoCommitDisable();
        toggle.checked = Boolean(result.enabled);
        autoStatus.textContent = result.detail || "";
        showToast(result.enabled ? "已开启：登录 Windows 时自动提交" : "已关闭自动提交");
      } catch (error) {
        toggle.checked = !toggle.checked;
        showToast(error.message);
      }
    })
  );
  const pushInput = h("input", { type: "checkbox" });
  pushInput.checked = Boolean(auto.push);
  const autoStatus = h("div", { class: "muted-small", text: auto.detail || "" });
  const runNow = h("button", { class: "btn ghost", text: "立即执行一次" });
  runNow.addEventListener(
    "click",
    safe(async () => {
      const result = await api.gitAutoCommitRun();
      showToast(result.ok ? "已执行（详见 .logs\\auto-commit.log）" : "执行失败，详见日志");
      await renderGitPanel();
    })
  );
  nodes.push(
    h(
      "details",
      { class: "git-section" },
      h("summary", { text: "每日开机自动提交" }),
      h(
        "label",
        { class: "checkbox" },
        toggle,
        h("span", { text: "每天打开电脑（登录 Windows）时自动提交一次" })
      ),
      h("label", { class: "checkbox" }, pushInput, h("span", { text: "提交后同时推送到远端" })),
      autoStatus,
      h("div", { class: "approval-actions" }, runNow)
    )
  );

  // 历史
  nodes.push(h("h3", { class: "section-label", text: "最近提交" }));
  nodes.push(
    h(
      "div",
      { class: "checklist" },
      ...history.commits.map((commit) =>
        (() => {
          const row = h(
          "div",
          { class: "check-item" },
          h(
            "div",
            { class: "check-title" },
            h("span", { class: "badge", text: commit.hash }),
            h("span", { text: commit.subject }),
            h("span", { class: "muted-small", text: "点击查看改动" })
          ),
          h("div", { class: "check-goal", text: `${commit.author} · ${commit.date}` })
        );
          // 整行可点：以前每条提交都带一个按钮，10 条历史就是 10 个按钮
          row.classList.add("git-commit-row");
          row.addEventListener(
            "click",
            safe(async () => {
              const existing = row.querySelector(".diff");
              if (existing) {
                existing.remove();
                return;
              }
              const payload = await api.gitShow(commit.hash);
              const block = h("div", { class: "diff" });
              for (const line of (payload.diff || "（无差异）").split("\n")) {
                let cls = "diff-line";
                if (line.startsWith("@@")) cls += " hunk";
                else if (line.startsWith("+") && !line.startsWith("+++")) cls += " add";
                else if (line.startsWith("-") && !line.startsWith("---")) cls += " del";
                block.append(h("div", { class: cls, text: line }));
              }
              row.append(block);
            })
          );
          return row;
        })()
      )
    )
  );

  body.replaceChildren(...nodes);
}

async function runGitAction(action, label) {
  const result = await action();
  showToast(
    result.ok
      ? `${label}完成`
      : `${label}失败：${(result.hint || result.stderr || "").slice(0, 120)}`
  );
  await renderGitPanel();
}

/* ── 命令面板（Ctrl+K，Codex 式）── */

/** 重新打包并重启：改完自己的源码后，让改动真的生效。 */
async function rebuildAndRestart() {
  const ok = await appConfirm({
    title: "重新打包并重启",
    message:
      "会先重新打包（包含运行测试与静态检查），再结束当前程序并启动新版本。\n" +
      "当前窗口几秒后会关闭；正在跑的运行会被标记为「已暂停」，可在新实例里继续。",
    confirmText: "开始",
    danger: true,
  });
  if (!ok) return;
  try {
    const result = await api.restart(true);
    showToast(result.detail || "正在重新打包并重启…");
  } catch (error) {
    showToast(error.message);
  }
}

function paletteCommands() {
  const commands = [
    { id: "new-run", label: "新建任务", hint: "清空输入，开始一个新任务", run: startNewRun },
    { id: "open-market", label: "打开插件市场", hint: "浏览并安装声明式插件", run: () => openMarket("market") },
    { id: "open-plugins", label: "查看已装插件", hint: "启用 / 禁用 / 卸载", run: () => openMarket("installed") },
    { id: "open-sources", label: "管理目录来源", hint: "添加 HTTPS 目录清单", run: () => openMarket("sources") },
    { id: "open-settings", label: "打开设置", hint: "中转 / 个人 Key、分段路由", run: openSettings },
    { id: "open-git", label: "打开 Git 面板", hint: "提交 / 推送 / 每日自动提交开关", run: openGitPanel },
    {
      id: "toggle-sidebar",
      label: "折叠 / 展开左侧任务栏",
      hint: "图标栏模式",
      run: () => document.getElementById("sidebar-toggle")?.click(),
    },
    { id: "check-version", label: "查看版本与更新方式", hint: "构建号", run: showVersionInfo },
    {
      id: "rebuild-restart",
      label: "重新打包并重启",
      hint: "改完源码后用：打包 → 重启 → 中断的运行可继续",
      run: rebuildAndRestart,
    },
    { id: "shortcuts", label: "查看快捷键", hint: "Ctrl+/", run: showShortcutHelp },
  ];
  if (state.run) {
    commands.push(
      { id: "go-plan", label: "跳到「纲领」面板", hint: state.run.title || "", run: () => switchInspectorTab("plan") },
      { id: "go-changes", label: "跳到「变更」面板", hint: "查看 diff", run: () => switchInspectorTab("changes") },
      { id: "go-docs", label: "跳到「文档」面板", hint: "plan.md / report.md", run: () => switchInspectorTab("docs") },
    );
    if (state.run.status === "awaiting_approval") {
      commands.push({
        id: "approve",
        label: "确认并开始执行",
        hint: "当前纲领等待确认",
        run: async () => {
          await api.approve(state.run.id, "");
          state.run.status = "executing";
          render();
        },
      });
    }
    if (state.run.status === "blocked") {
      commands.push({
        id: "resume",
        label: "补充信息并继续",
        hint: "从被阻塞的那一步继续",
        run: () => document.querySelector(".approval textarea")?.focus(),
      });
    }
  }
  for (const profile of state.providers || []) {
    if (profile.id === state.activeProviderId) continue;
    commands.push({
      id: `switch-${profile.id}`,
      label: `切换到配置：${profile.name}`,
      hint: profile.base_url || "",
      run: () => quickSwitchProvider(profile.id),
    });
  }
  for (const run of (state.runs || []).slice(0, 8)) {
    commands.push({
      id: `open-${run.id}`,
      label: `打开任务：${run.title || run.id}`,
      hint: STATUS_TEXT[run.status] || run.status,
      run: () => openRun(run.id),
    });
  }
  return commands;
}

function switchInspectorTab(tab) {
  // 切换明细标签一律把面板打开：用户点它就是想看
  setPanelOpen(true);
  state.tab = tab;
  document.querySelectorAll("#inspector-tabs .tab").forEach((node) => {
    node.classList.toggle("active", node.dataset.tab === tab);
  });
  renderInspector();
}

/** 右侧明细面板开合（默认收起，让主区占满）。 */
function setPanelOpen(open) {
  state.panelOpen = Boolean(open);
  document.body.dataset.panel = state.panelOpen ? "open" : "closed";
  const toggle = document.getElementById("inspector-toggle");
  if (toggle) toggle.setAttribute("aria-pressed", String(state.panelOpen));
  try {
    localStorage.setItem(PANEL_KEY, state.panelOpen ? "1" : "0");
  } catch {
    /* 隐私模式下忽略 */
  }
}

function togglePanel() {
  setPanelOpen(!state.panelOpen);
}

function togglePalette() {
  if (state.palette.open) {
    closePalette();
    return;
  }
  openPalette();
}

function openPalette() {
  state.palette.open = true;
  state.palette.active = 0;
  const modal = document.getElementById("palette-modal");
  const input = document.getElementById("palette-input");
  if (!modal || !input) {
    reportClientError("palette", new Error("命令面板元素缺失"));
    return;
  }
  input.value = "";
  modal.hidden = false;
  renderPalette();
  input.focus();
}

function closePalette() {
  state.palette.open = false;
  const modal = document.getElementById("palette-modal");
  if (modal) modal.hidden = true;
}

function renderPalette() {
  const input = document.getElementById("palette-input");
  const list = document.getElementById("palette-list");
  if (!list) return;
  const needle = (input?.value || "").trim().toLowerCase();
  const commands = paletteCommands().filter(
    (command) =>
      !needle ||
      command.label.toLowerCase().includes(needle) ||
      (command.hint || "").toLowerCase().includes(needle)
  );
  state.palette.items = commands;
  if (state.palette.active >= commands.length) state.palette.active = 0;
  if (!commands.length) {
    list.replaceChildren(h("div", { class: "palette-empty", text: "没有匹配的命令" }));
    return;
  }
  list.replaceChildren(
    ...commands.map((command, index) => {
      const item = h(
        "div",
        {
          class: `palette-item${index === state.palette.active ? " active" : ""}`,
          dataset: { command: command.id },
        },
        h("span", { text: command.label }),
        command.hint ? h("span", { class: "hint", text: command.hint }) : null
      );
      item.addEventListener("click", safe(() => runPaletteCommand(command)));
      return item;
    })
  );
}

async function runPaletteCommand(command) {
  closePalette();
  await command.run();
}

function handlePaletteKey(event) {
  if (!state.palette.open) return;
  const items = state.palette.items || [];
  if (event.key === "ArrowDown") {
    event.preventDefault();
    state.palette.active = items.length ? (state.palette.active + 1) % items.length : 0;
    renderPalette();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    state.palette.active = items.length ? (state.palette.active - 1 + items.length) % items.length : 0;
    renderPalette();
  } else if (event.key === "Enter") {
    event.preventDefault();
    const command = items[state.palette.active];
    if (command) runPaletteCommand(command);
  }
}

/* ── 插件：侧栏底部槽位入口 ── */

async function loadPlugins() {
  const token = ++state.tokens.plugins;
  try {
    const payload = await api.plugins();
    if (token !== state.tokens.plugins) return; // 旧响应不能覆盖新状态
    state.plugins = payload;
  } catch (error) {
    if (token !== state.tokens.plugins) return;
    state.plugins = { plugins: [], footer_actions: [] };
  }
  renderPluginFooterActions();
}

function renderPluginFooterActions() {
  const host = document.getElementById("plugin-footer-actions");
  host.replaceChildren(
    ...(state.plugins.footer_actions || []).map((action) => {
      const button = h(
        "button",
        {
          class: "foot-btn",
          type: "button",
          title: `由插件提供：${action.label}`,
          dataset: { entry: action.plugin_id, slot: "sidebar.footer.action" },
        },
        h("span", { class: "foot-icon", text: action.icon || "◆" }),
        h("span", { class: "foot-label", text: action.label })
      );
      button.addEventListener("click", () => {
        if (action.action === "open.market") {
          openMarket("market");
          return;
        }
        showToast(
          `「${action.label}」由插件 ${action.plugin_id} 提供；当前插件为声明式插件，只登记入口，不执行第三方代码。`
        );
      });
      return button;
    })
  );
}

/* ── 插件市场 ── */

async function openMarket(tab) {
  if (tab) state.marketTab = tab;
  document.querySelectorAll("#market-tabs .tab").forEach((node) => {
    node.classList.toggle("active", node.dataset.mtab === state.marketTab);
  });
  document.getElementById("market-modal").hidden = false;
  document.getElementById("market-body").replaceChildren(h("p", { class: "muted", text: "加载中…" }));
  try {
    if (!state.market.capabilities) {
      state.market.capabilities = await api.marketCapabilities();
    }
    await Promise.all([refreshMarketSources(), loadPlugins()]);
    await refreshMarketItems();
  } catch (error) {
    showToast(error.message);
  }
  renderMarket();
}

function closeMarket() {
  document.getElementById("market-modal").hidden = true;
}

async function refreshMarketSources() {
  const payload = await api.marketSources();
  state.market.sources = payload.sources || [];
}

async function refreshMarketItems() {
  const token = ++state.tokens.market;
  const payload = await api.marketItems({
    q: state.market.query,
    source_id: state.market.sourceId,
    limit: 50,
  });
  if (token !== state.tokens.market) return;
  state.market.items = payload.items || [];
}

function renderMarket() {
  const body = document.getElementById("market-body");
  if (state.marketTab === "installed") {
    body.replaceChildren(renderInstalledPane());
    return;
  }
  if (state.marketTab === "sources") {
    body.replaceChildren(renderSourcesPane());
    return;
  }
  body.replaceChildren(renderBrowsePane());
}

function renderBrowsePane() {
  const search = h("input", {
    type: "search",
    placeholder: "搜索插件（名称 / 关键词）",
    value: state.market.query,
  });
  const submit = async () => {
    state.market.query = search.value.trim();
    try {
      await refreshMarketItems();
      renderMarket();
    } catch (error) {
      showToast(error.message);
    }
  };
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submit();
  });
  const sourceSelect = h(
    "select",
    {},
    h("option", { value: "", text: "全部来源" }),
    ...state.market.sources.map((source) =>
      h("option", { value: source.source_record_id, text: source.name })
    )
  );
  sourceSelect.value = state.market.sourceId;
  sourceSelect.addEventListener("change", async () => {
    state.market.sourceId = sourceSelect.value;
    try {
      await refreshMarketItems();
      renderMarket();
    } catch (error) {
      showToast(error.message);
    }
  });
  const refresh = h("button", { class: "btn ghost", text: "刷新" });
  refresh.addEventListener("click", submit);

  const cards = state.market.items.map(marketCard);
  return h(
    "div",
    {},
    h("div", { class: "market-toolbar" }, search, sourceSelect, refresh),
    h("p", {
      class: "muted-small",
      text: `共 ${state.market.items.length} 个插件。市场只安装「声明式插件」：登记入口与能力声明，不下载、不执行第三方代码。`,
    }),
    cards.length
      ? h("div", { class: "market-grid" }, ...cards)
      : h("p", { class: "muted", text: "没有匹配的插件（可到「来源」页添加目录，或换个关键词）。" })
  );
}

function marketCard(item) {
  const required = (item.capabilities?.required || []).map((cap) => ({ ...cap, required: true }));
  const optional = (item.capabilities?.optional || []).map((cap) => ({ ...cap, required: false }));
  const chips = [...required, ...optional].map((cap) =>
    h("span", {
      class: `cap-chip${cap.required ? " danger" : ""}`,
      title: cap.description || "",
      text: cap.required ? `${cap.id}（必需）` : cap.id,
    })
  );
  const contributions = (item.contributions || []).map(
    (entry) => `槽位 ${entry.slot}${entry.label ? "：" + entry.label : ""}`
  );
  const installButton = h("button", {
    class: "btn primary",
    text: item.installed ? "已安装" : "安装",
  });
  installButton.disabled = Boolean(item.installed);
  installButton.addEventListener("click", () => installPlugin(item));

  const card = h(
    "div",
    { class: "market-card", dataset: { item: item.id } },
    h(
      "h4",
      {},
      h("span", { text: item.title }),
      h("span", { class: "muted-small", text: item.latest_version || "" })
    ),
    h("p", { text: item.summary || "（该插件没有填写说明）" }),
    chips.length ? h("div", { class: "cap-list" }, ...chips) : null,
    contributions.length
      ? h("div", { class: "muted-small", text: contributions.join("；") })
      : h("div", { class: "muted-small", text: "不占用界面槽位" }),
    h("div", {
      class: "muted-small",
      text: `来源：${item.provenance?.provider_id || "未知"} · ${item.license || "许可未标注"}`,
    }),
    h("div", { class: "card-actions" }, installButton)
  );
  return card;
}

async function installPlugin(item) {
  try {
    const payload = await api.installPlugin({
      source_record_id: item.provenance?.source_record_id || "",
      item_id: item.id,
    });
    state.plugins = payload;
    renderPluginFooterActions();
    await refreshMarketItems();
    renderMarket();
    showToast(`已安装「${item.title}」，左侧任务栏底部已出现它的入口。`);
  } catch (error) {
    if (token !== state.tokens.providers) return;
    showToast(error.message);
  }
}

function renderInstalledPane() {
  const plugins = state.plugins.plugins || [];
  if (!plugins.length) {
    return h("p", { class: "muted", text: "还没有安装任何插件。到「市场」页挑一个试试。" });
  }
  return h(
    "div",
    { class: "market-grid" },
    ...plugins.map((plugin) => {
      const chips = (plugin.capabilities || []).map((cap) =>
        h("span", { class: "cap-chip", title: cap.description || "", text: cap.id })
      );
      const toggle = h("button", {
        class: "btn ghost",
        text: plugin.enabled ? "禁用" : "启用",
      });
      toggle.addEventListener("click", async () => {
        try {
          state.plugins = plugin.enabled
            ? await api.disablePlugin(plugin.id)
            : await api.enablePlugin(plugin.id);
          renderPluginFooterActions();
          renderMarket();
        } catch (error) {
          showToast(error.message);
        }
      });
      const remove = h("button", { class: "btn ghost", text: "卸载" });
      remove.addEventListener("click", async () => {
        const confirmed = await appConfirm({
          title: "卸载插件",
          message: `确定卸载「${plugin.name}」？它在侧栏的入口会一并消失。`,
          confirmText: "卸载",
          danger: true,
        });
        if (!confirmed) return;
        try {
          state.plugins = await api.uninstallPlugin(plugin.id);
          renderPluginFooterActions();
          await refreshMarketItems();
          renderMarket();
        } catch (error) {
          showToast(error.message);
        }
      });
      return h(
        "div",
        { class: "market-card", dataset: { plugin: plugin.id } },
        h(
          "h4",
          {},
          h("span", { text: plugin.name }),
          h("span", { class: "muted-small", text: plugin.version }),
          h("span", {
            class: `cap-chip${plugin.enabled ? "" : " danger"}`,
            text: plugin.enabled ? "已启用" : "已禁用",
          })
        ),
        h("p", { text: plugin.description || "（无说明）" }),
        chips.length ? h("div", { class: "cap-list" }, ...chips) : null,
        h("div", { class: "muted-small", text: `来源：${plugin.origin?.provider_id || "本地"}` }),
        h("div", { class: "card-actions" }, toggle, remove)
      );
    })
  );
}

function renderSourcesPane() {
  const input = h("input", {
    type: "url",
    placeholder: "https://example.com/catalog/manifest.json",
  });
  const addButton = h("button", { class: "btn primary", text: "添加来源" });
  addButton.addEventListener("click", async () => {
    const url = input.value.trim();
    if (!url) {
      showToast("请填写目录清单地址。");
      return;
    }
    addButton.disabled = true;
    try {
      const payload = await api.addMarketSource(url);
      state.market.sources = payload.sources || [];
      input.value = "";
      await refreshMarketItems();
      renderMarket();
      showToast("来源已添加。");
    } catch (error) {
      showToast(error.message);
    } finally {
      addButton.disabled = false;
    }
  });

  const rows = state.market.sources.map((source) => {
    const remove = h("button", { class: "btn ghost", text: "删除" });
    remove.disabled = Boolean(source.builtin);
    remove.addEventListener("click", async () => {
      try {
        const payload = await api.removeMarketSource(source.source_record_id);
        state.market.sources = payload.sources || [];
        await refreshMarketItems();
        renderMarket();
      } catch (error) {
        showToast(error.message);
      }
    });
    return h(
      "div",
      { class: "source-row", dataset: { source: source.source_record_id } },
      h(
        "div",
        { class: "grow" },
        h("div", {}, h("b", { text: source.name }), source.builtin ? h("span", { class: "cap-chip", text: "内置" }) : null),
        h("div", { class: "host", text: source.transport?.endpoint || "" }),
        h("div", { class: "muted-small", text: `提供方：${source.provider_id}${source.attribution?.name ? ` · ${source.attribution.name}` : ""}` })
      ),
      remove
    );
  });

  return h(
    "div",
    { class: "market-body-col" },
    h("div", { class: "market-toolbar" }, input, addButton),
    h("p", {
      class: "muted-small",
      text: "只接受 HTTPS 的目录清单（manifestVersion 1.0.0，含 providerId / attribution / transport / query）。本地会为每条来源生成独立 id，条目的来源信息会随插件一起保留。",
    }),
    h("div", { class: "market-grid" }, ...rows)
  );
}

let toastTimer = null;

/** 快捷键说明（Ctrl+/）。 */
function showShortcutHelp() {
  const lines = [
    "Ctrl+K　命令面板（新建任务 / 打开市场 / 切换配置 / 跳转任务）",
    "Ctrl+/　本帮助",
    "Ctrl+Enter　发送任务",
    "Esc　关闭最上层的弹窗",
    "侧栏 ▤　显示 / 隐藏已归档任务",
    "任务项悬停　★ 置顶、✎ 重命名、▣ 归档",
  ];
  showToast(lines.join("　|　"));
}

function showToast(message) {
  let node = document.getElementById("toast");
  if (!node) {
    node = h("div", { id: "toast" });
    node.style.cssText =
      "position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:#1f2328;" +
      "color:#fff;border-radius:10px;padding:9px 16px;font-size:12.5px;z-index:60;max-width:70vw;" +
      "box-shadow:0 6px 20px rgba(31,35,40,0.25);";
    document.body.append(node);
  }
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.hidden = true;
  }, 4200);
}

/* ── 运行统计看板（第 4 步交付）────────────────────────────
   数据契约：run.metrics 为 PhaseMetrics[]（见 app/schemas/run.py 与
   docs/state-and-event-contracts.md）：phase / step_id / calls / retries /
   context_chars / prompt_tokens / completion_tokens / total_tokens /
   usage_source / usage_reason / duration_ms / route。
   token 为 null 表示“未知”，界面绝不把它渲染成 0。
   刷新路径：SSE 事件与 REST 首帧都会给 state.run 赋值，赋值即触发重绘；
   timeline 的 DOM 变更作为兜底触发。断线重连后 app.js 会重新拉取运行，
   看板随之恢复为最终数据。 */
(function installRunDashboard() {
  const STATUS_TEXT = {
    planning: "规划中",
    awaiting_approval: "待确认",
    executing: "执行中",
    blocked: "已阻塞",
    done: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };

  const STATUS_BADGE = {
    done: "ok",
    failed: "danger",
    cancelled: "danger",
    blocked: "warn",
    awaiting_approval: "warn",
  };

  const USAGE_SOURCE_TEXT = {
    provider: "提供方真实用量",
    estimated: "按字符估算",
    unknown: "用量未知",
  };

  const USAGE_REASON_TEXT = {
    provider_no_usage: "提供方未返回 usage",
    provider_stream_no_usage: "流式响应未带 usage",
    provider_partial_usage: "提供方只返回部分 usage",
    estimated_from_chars: "由字符数估算",
    unknown: "没有可用的用量信息",
  };

  let expanded = false;
  let signature = "";
  let pending = null;
  //: 上次渲染用的宿主元素；宿主被重建时缓存签名必须失效，否则会渲染成空白
  let lastHost = null;

  const numOf = (value) =>
    typeof value === "number" && Number.isFinite(value) ? value : null;
  const intOf = (value) => (numOf(value) || 0).toLocaleString("zh-CN");
  const tokenOf = (value) => {
    const n = numOf(value);
    return n === null ? "未知" : n.toLocaleString("zh-CN");
  };

  function durationOf(ms) {
    const n = numOf(ms);
    if (n === null || n <= 0) return "—";
    if (n < 1000) return `${n} 毫秒`;
    const seconds = n / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes} 分 ${Math.round(seconds - minutes * 60)} 秒`;
  }

  function readMetrics(run) {
    if (Array.isArray(run.metrics)) return run.metrics;
    if (run.metrics_summary && Array.isArray(run.metrics_summary.items)) {
      return run.metrics_summary.items;
    }
    return [];
  }

  function sumField(items, field) {
    let total = 0;
    let known = false;
    for (const item of items) {
      const value = numOf(item[field]);
      if (value === null) continue;
      total += value;
      known = true;
    }
    return known ? total : null;
  }

  function fallbackAliases(metrics) {
    const counts = new Map();
    for (const item of metrics) {
      const alias = item.route && item.route.alias ? String(item.route.alias) : "";
      if (!alias) continue;
      counts.set(alias, (counts.get(alias) || 0) + 1);
    }
    const others = new Set();
    if (counts.size <= 1) return others;
    let base = "";
    let best = -1;
    for (const [alias, count] of counts) {
      if (count > best) {
        base = alias;
        best = count;
      }
    }
    for (const alias of counts.keys()) {
      if (alias !== base) others.add(alias);
    }
    return others;
  }

  function firstUnknownReason(metrics) {
    const item = metrics.find((entry) => numOf(entry.total_tokens) === null);
    if (!item) return "";
    const reason = item.usage_reason ? String(item.usage_reason) : "";
    return USAGE_REASON_TEXT[reason] || reason || "提供方未返回用量";
  }

  function card(label, value, hint, unknown) {
    return h(
      "div",
      { class: "dash-card" },
      h("span", { class: "dash-card-label" }, label),
      h("strong", { class: `dash-card-value${unknown ? " unknown" : ""}` }, value),
      hint ? h("span", { class: "dash-card-hint", title: hint }, hint) : null,
    );
  }

  function cardsNode(metrics) {
    const totals = {
      calls: sumField(metrics, "calls") || 0,
      retries: sumField(metrics, "retries") || 0,
      contextChars: sumField(metrics, "context_chars") || 0,
      duration: sumField(metrics, "duration_ms"),
      prompt: sumField(metrics, "prompt_tokens"),
      completion: sumField(metrics, "completion_tokens"),
      total: sumField(metrics, "total_tokens"),
    };
    const unknownHint = firstUnknownReason(metrics);
    return h(
      "div",
      { class: "dash-cards" },
      card("总耗时", durationOf(totals.duration), `${totals.calls} 次模型调用`),
      card("输入 token", tokenOf(totals.prompt), unknownHint || "各阶段提示词用量", totals.prompt === null),
      card("输出 token", tokenOf(totals.completion), unknownHint || "各阶段补全用量", totals.completion === null),
      card("合计 token", tokenOf(totals.total), unknownHint || "输入 + 输出", totals.total === null),
      card("上下文占用", `${intOf(totals.contextChars)} 字符`, "各阶段实际发送的上下文字符数"),
      card("重试次数", intOf(totals.retries), `含首次共 ${totals.calls} 次调用`),
    );
  }

  function metricRow(label, item, fallbacks) {
    const alias = item.route && item.route.alias ? String(item.route.alias) : "";
    const isFallback = Boolean(alias && fallbacks.has(alias));
    const source = USAGE_SOURCE_TEXT[item.usage_source] || "用量未知";
    const reason = item.usage_reason
      ? USAGE_REASON_TEXT[item.usage_reason] || String(item.usage_reason)
      : "";
    const model = item.route && item.route.model ? String(item.route.model) : "";
    const route = [alias, model].filter(Boolean).join(" · ") || "—";
    const cells = [
      ["耗时", durationOf(item.duration_ms)],
      ["输入 token", tokenOf(item.prompt_tokens)],
      ["输出 token", tokenOf(item.completion_tokens)],
      ["合计 token", tokenOf(item.total_tokens)],
      ["上下文", `${intOf(item.context_chars)} 字符`],
      ["用量来源", reason ? `${source}（${reason}）` : source],
      ["路由", route],
    ];
    return h(
      "div",
      { class: `dash-step-row${isFallback ? " is-fallback" : ""}` },
      h(
        "div",
        { class: "dash-step-head" },
        h("span", { class: "dash-step-name" }, label),
        isFallback ? h("span", { class: "dash-badge warn" }, "备用配置") : null,
        h("span", { class: "dash-step-status" }, `${intOf(item.calls)} 次调用 · 重试 ${intOf(item.retries)} 次`),
      ),
      h(
        "div",
        { class: "dash-step-grid" },
        cells.map(([name, value]) =>
          h(
            "div",
            { class: "dash-cell" },
            h("span", null, name),
            h("span", { title: String(value) }, value),
          ),
        ),
      ),
    );
  }

  function stepLabel(steps, stepId) {
    const id = numOf(stepId);
    if (id === null) return "执行段";
    const step = steps.find(
      (item) =>
        numOf(item.step_id) === id || numOf(item.index) === id || numOf(item.id) === id,
    );
    const title = step && typeof step.title === "string" ? step.title.trim() : "";
    return title ? `第 ${id} 步 · ${title}` : `第 ${id} 步`;
  }

  function detailNode(metrics, steps) {
    const fallbacks = fallbackAliases(metrics);
    const architect = metrics.filter((item) => (item.phase || "executor") === "architect");
    const executor = metrics
      .filter((item) => (item.phase || "executor") !== "architect")
      .slice()
      .sort((a, b) => (numOf(a.step_id) ?? -1) - (numOf(b.step_id) ?? -1));
    const rows = [
      ...architect.map((item) => metricRow("架构段", item, fallbacks)),
      ...executor.map((item) => metricRow(stepLabel(steps, item.step_id), item, fallbacks)),
    ];
    const detail = h(
      "div",
      { class: "dash-detail" },
      rows.length ? rows : h("div", { class: "dash-empty" }, "暂无步骤级指标。"),
    );
    detail.hidden = !expanded;
    return detail;
  }

  function toggleDetail() {
    expanded = !expanded;
    const host = document.getElementById("run-dashboard");
    if (!host) return;
    const detail = host.querySelector(".dash-detail");
    if (detail) detail.hidden = !expanded;
    const button = host.querySelector(".dash-toggle");
    if (button) {
      button.textContent = expanded ? "收起明细" : "展开明细";
      button.setAttribute("aria-expanded", String(expanded));
    }
  }

  function headNode(run, metrics) {
    const status = String(run.status || "");
    const badges = [
      h(
        "span",
        { class: `dash-badge ${STATUS_BADGE[status] || ""}`.trim() },
        STATUS_TEXT[status] || "状态未知",
      ),
    ];
    if (status === "planning" || status === "executing") {
      badges.push(h("span", { class: "dash-badge live" }, "运行中"));
    }
    const fallbacks = fallbackAliases(metrics);
    if (fallbacks.size) {
      badges.push(
        h(
          "span",
          { class: "dash-badge warn", title: "本次运行中途切换过配置" },
          `备用配置：${[...fallbacks].join("、")}`,
        ),
      );
    }
    const unknown = metrics.filter((item) => numOf(item.total_tokens) === null);
    if (unknown.length) {
      badges.push(
        h(
          "span",
          { class: "dash-badge warn", title: firstUnknownReason(metrics) },
          `用量未知 ${unknown.length} 处`,
        ),
      );
    }
    return h(
      "div",
      { class: "dash-head" },
      h("span", { class: "dash-title" }, "运行统计"),
      h("div", { class: "dash-badges" }, badges),
      h(
        "button",
        {
          class: "btn ghost dash-toggle",
          id: "dash-toggle",
          type: "button",
          "aria-expanded": String(expanded),
          onclick: toggleDetail,
        },
        expanded ? "收起明细" : "展开明细",
      ),
    );
  }

  function render() {
    // 宿主现在在「统计」标签里，是按需渲染的：每次都重新取，取不到就什么都不做
    const host = document.getElementById("run-dashboard");
    if (!host) return;
    if (host !== lastHost) {
      lastHost = host;
      signature = "";
    }
    const run = state.run;
    if (!run || typeof run !== "object") {
      host.hidden = true;
      host.replaceChildren();
      signature = "";
      return;
    }
    const metrics = readMetrics(run);
    const steps = Array.isArray(run.steps) ? run.steps : [];
    const sig = JSON.stringify([
      run.status,
      metrics,
      steps.map((step) => [step.status, step.retries]),
    ]);
    if (sig === signature) return;
    signature = sig;
    host.hidden = false;
    if (!metrics.length) {
      host.replaceChildren(
        h(
          "div",
          { class: "dash-head" },
          h("span", { class: "dash-title" }, "运行统计"),
          h(
            "div",
            { class: "dash-badges" },
            h(
              "span",
              { class: `dash-badge ${STATUS_BADGE[run.status] || ""}`.trim() },
              STATUS_TEXT[run.status] || "状态未知",
            ),
          ),
        ),
        h("div", { class: "dash-empty" }, "本次运行尚无指标（模型调用完成后自动出现）。"),
      );
      return;
    }
    host.replaceChildren(headNode(run, metrics), cardsNode(metrics), detailNode(metrics, steps));
  }

  function schedule() {
    if (pending !== null) return;
    pending = setTimeout(() => {
      pending = null;
      render();
    }, 120);
  }

  // 暴露给 renderInspector / render 触发（宿主是动态创建的，不能只在加载时抓一次）
  dashboardRefresh = schedule;

  // 主驱动：SSE 事件与 REST 首帧都会给 state.run 赋值
  let currentRun = state.run;
  Object.defineProperty(state, "run", {
    configurable: true,
    enumerable: true,
    get() {
      return currentRun;
    },
    set(value) {
      currentRun = value;
      schedule();
    },
  });

  // 兜底：原地变更 + 整体重排时，DOM 变化也能触发刷新
  if (dom.timeline) {
    new MutationObserver(schedule).observe(dom.timeline, { childList: true, subtree: true });
  }

  schedule();
})();

boot().catch((error) => reportClientError("boot", error));
