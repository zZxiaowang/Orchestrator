/* 工作区导航 / 路由 / 数据加载（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
/* ── 工作区：普通对话 / 项目 / 项目内模块 ──
   左侧栏只暴露 CONTEXT_CHAT / CONTEXT_PROJECT 两个功能入口；架构 / 计划 / 执行 /
   验证 / 日志 / 设置只作为项目内的二级导航出现。上下文由 **hash 路由** 决定，
   localStorage 只记"上次看过的项目 / 模块"，用于没有 hash 时恢复。 */
const STORE_CONTEXT_KEY = "orchestrator.contextType";
const STORE_PROJECT_KEY = "orchestrator.projectId";
const STORE_SECTION_KEY = "orchestrator.projectSection";

//: 二级功能中文名：同样来自后端注入（缺项时回落到英文标识，新增模块不会让导航崩掉）
const PROJECT_SECTION_LABELS = Object.fromEntries(
  PROJECT_MODULE_DEFS.map((item) => [item.id, item.label || item.id])
);

//: 概览里最多列多少次运行（一百条折叠卡片既慢又没人看）
const OVERVIEW_RUN_LIMIT = 20;

//: 主区两块视图的结构签名：数据没变就不重建 DOM（和运行时间线同一套防重排思路）
let lastChatSignature = "";
let lastModuleSignature = "";
let lastProjectListSignature = "";

const ProjectWorkspace = {
  contextType: DEFAULT_CONTEXT_TYPE,
  projectId: "",
  module: PROJECT_SECTIONS[0],
  chatId: "",
};

//: 真实数据缓存：项目列表、对话列表、当前模块载荷、当前会话详情
const WorkspaceState = {
  projects: [],
  chats: [],
  modulePayload: null,
  //: 模块加载失败的原因（四态里的"错误态"：可重试，而不是一直转圈）
  moduleError: "",
  chatPayload: null,
  chatError: "",
};

function safeReadStorage(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch (error) {
    return "";
  }
}

function safeWriteStorage(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (error) {
    /* 隐私模式 / 配额溢出：忽略，内存状态仍然生效 */
  }
}

function dispatchWorkspaceEvent(name) {
  document.dispatchEvent(
    new CustomEvent(name, {
      detail: {
        contextType: ProjectWorkspace.contextType,
        projectId: ProjectWorkspace.projectId,
        module: ProjectWorkspace.module,
        chatId: ProjectWorkspace.chatId,
      },
    }),
  );
}

function renderProjectName() {
  const node = document.getElementById("project-name");
  if (!node) return;
  const id = ProjectWorkspace.projectId;
  const card = (WorkspaceState.projects || []).find((item) => item.project_id === id);
  node.textContent = card ? card.name : id || "未选择项目";
  node.title = id ? `当前项目：${card ? card.name : id}（${id}）` : "还没有选择项目";
  node.dataset.projectId = id;
}

function renderProjectNav() {
  const host = document.getElementById("project-nav");
  if (!host) return;
  host.replaceChildren(
    ...PROJECT_SECTIONS.map((section) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "project-nav-btn";
      btn.dataset.navSection = section;
      btn.textContent = PROJECT_SECTION_LABELS[section] || section;
      btn.classList.toggle("active", section === ProjectWorkspace.module);
      btn.disabled = !ProjectWorkspace.projectId;
      btn.addEventListener("click", () => selectProjectSection(section));
      return btn;
    }),
  );
}

/** 项目列表：来自 ``GET /api/v1/projects``（真实容器，不是从运行记录里拼出来的候选）。 */
function renderProjectOptions() {
  const host = document.getElementById("project-options");
  if (!host) return;
  const projects = WorkspaceState.projects || [];
  if (!projects.length) {
    host.replaceChildren(
      h("div", { class: "chat-empty", text: "还没有项目：在上面填名称即可新建。" })
    );
    return;
  }
  host.replaceChildren(
    ...projects.map((project) => {
      const btn = h("button", {
        type: "button",
        class: "list-item project-item",
        dataset: { projectId: project.project_id },
      });
      btn.classList.toggle("active", project.project_id === ProjectWorkspace.projectId);
      btn.append(
        h("div", { class: "run-name", text: project.name }),
        h("div", {
          class: "run-meta",
          text: [
            project.project_id === "default" ? "默认项目" : project.project_id,
            `${project.runs || 0} 次运行`,
            project.workspace?.root_path ? project.workspace.root_path : "未绑定工作区",
          ].join(" · "),
        })
      );
      btn.addEventListener("click", () => openProject(project.project_id).catch(showToast));
      return btn;
    })
  );
}

/** 普通对话列表：来自 ``GET /api/v1/chats``（会话真的存在后端，不再是 localStorage）。 */
function renderChatList() {
  const host = document.getElementById("chat-list");
  if (!host) return;
  const chats = WorkspaceState.chats || [];
  if (!chats.length) {
    host.replaceChildren(
      h("div", {
        class: "chat-empty",
        text: "普通对话是轻量工作区：只收发消息，不产生纲领、步骤与验证记录。点「＋ 新对话」开始。",
      })
    );
    return;
  }
  host.replaceChildren(
    ...chats.map((chat) => {
      const node = h("button", {
        type: "button",
        class: "list-item chat-item",
        dataset: { chatId: chat.id },
      });
      node.classList.toggle("active", chat.id === ProjectWorkspace.chatId);
      node.append(
        h("div", { class: "run-name", text: chat.title || "未命名对话" }),
        h("div", {
          class: "run-meta",
          text: `${chat.messages || 0} 条消息${chat.preview ? ` · ${chat.preview}` : ""}`,
        })
      );
      node.addEventListener("click", () => enterChat(chat.id).catch(showToast));
      return node;
    })
  );
}

function applyWorkspaceContext(contextType, options = {}) {
  return switchWorkspace(contextType, options);
}

/* ── 工作区：普通对话 / 项目 / 项目内模块（真实数据 + hash 路由）────────────
   路由：#/chat、#/chat/<chatId>、#/projects、#/projects/<projectId>[/<module>]。
   旧链接（#/runs/<id>、#/settings、#/plugins）按契约重定向到项目内模块。 */

function chatRoute(chatId) {
  return chatId ? `#/chat/${encodeURIComponent(chatId)}` : "#/chat";
}

function projectRoute(projectId, module) {
  if (!projectId) return "#/projects";
  const base = `#/projects/${encodeURIComponent(projectId)}`;
  return module && module !== "overview" ? `${base}/${module}` : base;
}

//: 自己设进去的 hash：只忽略"恰好是这一个"的回调，避免丢一次事件后把后续导航也吞掉
let expectedHash = "";

function setRouteHash(route) {
  if (!route) return;
  const next = route.startsWith("#") ? route : `#${route}`;
  if (location.hash === next) return;
  expectedHash = next;
  location.hash = next;
}

function parseRoute(hash = location.hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const parts = raw
    .split("?")[0]
    .split("/")
    .filter(Boolean)
    .map((item) => decodeURIComponent(item));
  if (!parts.length) return { kind: "projects" };
  if (parts[0] === "chat") return { kind: "chat", chatId: parts[1] || "" };
  if (parts[0] === "projects") {
    if (!parts[1]) return { kind: "projects" };
    return {
      kind: "project",
      projectId: parts[1],
      module: PROJECT_SECTIONS.includes(parts[2]) ? parts[2] : "overview",
    };
  }
  if (parts[0] === "runs" && parts[1]) return { kind: "legacy-run", runId: parts[1] };
  if (parts[0] === "settings" || parts[0] === "plugins") return { kind: "legacy-settings" };
  return { kind: "projects" };
}

/** 界面上与"现在看哪儿"有关的一切：两个工作区面板、导航高亮、上下文相关的控件。 */
function syncWorkspaceChrome() {
  const type = ProjectWorkspace.contextType;
  const projectPane = document.getElementById("project-pane");
  const chatPane = document.getElementById("chat-pane");
  const navChat = document.getElementById("nav-chat");
  const navProject = document.getElementById("nav-project");
  if (projectPane) projectPane.hidden = type !== CONTEXT_PROJECT;
  if (chatPane) chatPane.hidden = type !== CONTEXT_CHAT;
  if (navChat) {
    navChat.classList.toggle("active", type === CONTEXT_CHAT);
    navChat.setAttribute("aria-pressed", type === CONTEXT_CHAT ? "true" : "false");
  }
  if (navProject) {
    navProject.classList.toggle("active", type === CONTEXT_PROJECT);
    navProject.setAttribute("aria-pressed", type === CONTEXT_PROJECT ? "true" : "false");
  }
  document.body.dataset.contextType = type;
  // 普通对话没有落地目录，也没有运行明细面板——项目控件一个都不渲染
  const meta = document.getElementById("composer-meta");
  if (meta) meta.hidden = type === CONTEXT_CHAT;
  const inspectorToggle = document.getElementById("inspector-toggle");
  if (inspectorToggle) inspectorToggle.hidden = type === CONTEXT_CHAT;
  if (type === CONTEXT_CHAT && state.panelOpen) setPanelOpen(false);

  safeWriteStorage(STORE_CONTEXT_KEY, type);
  if (type === CONTEXT_PROJECT && ProjectWorkspace.projectId) {
    safeWriteStorage(STORE_PROJECT_KEY, ProjectWorkspace.projectId);
  }
  renderProjectName();
  renderProjectNav();
  renderChatList();
  dispatchWorkspaceEvent("orchestrator:context-change");
}

/** 加载项目列表（左侧栏 + 项目选择器都用它）。 */
async function loadProjects() {
  try {
    const payload = await api.projects();
    WorkspaceState.projects = payload.projects || [];
  } catch (error) {
    reportClientError("projects", error);
    WorkspaceState.projects = [];
  }
  renderProjectOptions();
  return WorkspaceState.projects;
}

/** 加载普通对话列表。 */
async function loadChats() {
  try {
    const payload = await api.chats({ q: state.chatQuery || "" });
    WorkspaceState.chats = payload.chats || [];
  } catch (error) {
    reportClientError("chats", error);
    WorkspaceState.chats = [];
  }
  renderChatList();
  return WorkspaceState.chats;
}

/** 项目列表页：左侧栏给项目，主区给"选一个项目 / 新建项目"。 */
async function showProjects({ navigate = true } = {}) {
  ProjectWorkspace.contextType = CONTEXT_PROJECT;
  ProjectWorkspace.projectId = "";
  WorkspaceState.modulePayload = null;
  if (navigate) setRouteHash("#/projects");
  syncWorkspaceChrome();
  render();
}

/** 进入某个项目的某个模块：拉模块真实数据 + 选中该项目最近一次运行。 */
async function enterProject(projectId, module = "overview", { navigate = true } = {}) {
  const id = String(projectId || "").trim();
  ProjectWorkspace.contextType = CONTEXT_PROJECT;
  ProjectWorkspace.projectId = id;
  ProjectWorkspace.module = PROJECT_SECTIONS.includes(module) ? module : "overview";
  if (navigate) setRouteHash(projectRoute(id, ProjectWorkspace.module));
  syncWorkspaceChrome();

  WorkspaceState.modulePayload = null;
  await loadModulePayload();

  // 执行模块要有一条"当前运行"：优先沿用已在看的，否则取最近一次
  const runs = WorkspaceState.modulePayload?.runs || [];
  const known = state.run && runs.some((item) => item.id === state.run.id);
  const targetId = known ? state.run.id : runs[0]?.id || "";
  if (targetId && (!state.run || state.run.id !== targetId)) {
    await openRun(targetId).catch(() => {});
  } else if (!targetId) {
    state.run = null;
    state.buffers = {};
    disconnectStream();
  }
  render();
}

/** 进入普通对话：没给 id 就停在"新对话"空态，发第一条消息时会真的建会话。 */
async function enterChat(chatId = "", { navigate = true } = {}) {
  ProjectWorkspace.contextType = CONTEXT_CHAT;
  ProjectWorkspace.chatId = String(chatId || "");
  if (navigate) setRouteHash(chatRoute(ProjectWorkspace.chatId));
  WorkspaceState.chatPayload = null;
  state.run = null;
  state.buffers = {};
  disconnectStream();
  syncWorkspaceChrome();
  if (ProjectWorkspace.chatId) await loadChatDetail(ProjectWorkspace.chatId);
  render();
}

/** 打开一个已有会话（列表点击 / 深链）。 */
async function openChat(chatId, { navigate = true } = {}) {
  return enterChat(chatId, { navigate });
}

async function loadChatDetail(chatId) {
  try {
    const payload = await api.chat(chatId);
    WorkspaceState.chatPayload = payload.chat;
    WorkspaceState.chatError = "";
    connectStream(chatId, payload.event_seq || 0, { chat: true });
  } catch (error) {
    reportClientError("chat", error);
    WorkspaceState.chatPayload = null;
    WorkspaceState.chatError = error.message;
    showToast(error.message);
  }
}

async function loadModulePayload() {
  const projectId = ProjectWorkspace.projectId;
  if (!projectId) {
    WorkspaceState.modulePayload = null;
    WorkspaceState.moduleError = "";
    return null;
  }
  try {
    WorkspaceState.modulePayload = await api.projectModule(projectId, ProjectWorkspace.module);
    WorkspaceState.moduleError = "";
  } catch (error) {
    WorkspaceState.modulePayload = null;
    WorkspaceState.moduleError = error.message;
    reportClientError("project-module", error);
    showToast(error.message);
  }
  return WorkspaceState.modulePayload;
}

/** 按 hash 决定看哪儿（刷新、深链、浏览器前进后退都走这里）。 */
async function applyRoute(hash = location.hash) {
  const route = parseRoute(hash);
  if (route.kind === "chat") {
    await enterChat(route.chatId, { navigate: false });
    return;
  }
  if (route.kind === "project") {
    await enterProject(route.projectId, route.module, { navigate: false });
    return;
  }
  if (route.kind === "legacy-run") {
    // 旧运行链接：解析出项目后重定向进项目的执行模块（普通对话会话则进对话）
    try {
      const payload = await api.run(route.runId);
      const run = payload.run;
      if (run?.context_type === "chat") {
        await enterChat(route.runId);
        return;
      }
      state.run = run;
      state.lastSeq = payload.event_seq || 0;
      await enterProject(run.project_id || "default", "execution");
      return;
    } catch (error) {
      showToast("这条运行记录不存在或已被清理。");
      await showProjects();
      return;
    }
  }
  if (route.kind === "legacy-settings") {
    await showProjects();
    openSettings();
    return;
  }
  await showProjects({ navigate: false });
}

async function openProject(projectId, module = "") {
  const id = String(projectId || "").trim();
  const picker = document.getElementById("project-picker");
  if (picker) picker.hidden = true;
  if (!id) return showProjects();
  await enterProject(id, module || ProjectWorkspace.module || "overview");
  dispatchWorkspaceEvent("orchestrator:project-open");
}

/** 项目内模块切换（二级导航）。 */
async function selectProjectSection(module) {
  if (ProjectWorkspace.contextType !== CONTEXT_PROJECT) return;
  if (!PROJECT_SECTIONS.includes(module)) return;
  ProjectWorkspace.module = module;
  safeWriteStorage(STORE_SECTION_KEY, module);
  document.querySelectorAll("#project-nav .project-nav-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.navSection === module);
  });
  if (!ProjectWorkspace.projectId) {
    await showProjects({ navigate: false });
    render();
    return;
  }
  setRouteHash(projectRoute(ProjectWorkspace.projectId, module));
  await loadModulePayload();
  render();
  dispatchWorkspaceEvent("orchestrator:section-change");
}

/** 新建项目：名称 + 工作区目录（留空则由后端分配 data/projects/<id>/workspace）。 */
async function createProjectFromForm() {
  const nameInput = document.getElementById("project-name-input");
  const rootInput = document.getElementById("project-root-input");
  const name = (nameInput?.value || "").trim();
  if (!name) {
    showToast("先给项目起个名字。");
    nameInput?.focus();
    return;
  }
  const button = document.getElementById("project-create-btn");
  if (button) button.disabled = true;
  try {
    const payload = await api.createProject({
      name,
      root_path: (rootInput?.value || "").trim(),
    });
    if (nameInput) nameInput.value = "";
    if (rootInput) rootInput.value = "";
    await loadProjects();
    showToast(`已新建项目「${payload.project.name}」`);
    await openProject(payload.project.project_id, "overview");
  } catch (error) {
    showToast(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

/** 新建普通对话：先建空会话再进入（发第一条消息时才会请求模型）。 */
async function createChatSession() {
  const button = document.getElementById("new-chat-btn");
  if (button) button.disabled = true;
  try {
    const payload = await api.createChat({});
    await loadChats();
    await enterChat(payload.chat.id);
    document.getElementById("task-input")?.focus();
  } catch (error) {
    showToast(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

/** 普通对话里发消息：没有会话就先建一个，然后交给后端流式回答。 */
async function sendChatMessage(text) {
  const message = (text || "").trim();
  if (!message) return;
  let chatId = ProjectWorkspace.chatId;
  try {
    if (!chatId) {
      const created = await api.createChat({});
      chatId = created.chat.id;
      ProjectWorkspace.chatId = chatId;
      setRouteHash(chatRoute(chatId));
    }
    state.buffers.chat = "";
    const payload = await api.sendChat(chatId, message);
    WorkspaceState.chatPayload = payload.chat;
    if (payload.split_from) {
      // 后端判定"摘要撑不住了"，已开新会话承接：跟着切过去（旧会话留在列表里）
      showToast(payload.split_reason || "已自动开启新对话（承接摘要）");
      await loadChats();
      await enterChat(payload.chat.id);
      render();
      return;
    }
    connectStream(chatId, state.lastSeq, { chat: true });
    await loadChats();
    render();
  } catch (error) {
    showToast(error.message);
  }
}

function bindPrimaryNav() {
  const navChat = document.getElementById("nav-chat");
  const navProject = document.getElementById("nav-project");
  if (navChat) {
    navChat.addEventListener("click", () => {
      const remembered = ProjectWorkspace.chatId || "";
      enterChat(remembered).catch(showToast);
    });
  }
  if (navProject) {
    navProject.addEventListener("click", () => {
      const remembered = ProjectWorkspace.projectId || safeReadStorage(STORE_PROJECT_KEY) || "";
      if (remembered) enterProject(remembered, ProjectWorkspace.module).catch(showToast);
      else showProjects().catch(showToast);
    });
  }
  const newChat = document.getElementById("new-chat-btn");
  if (newChat) newChat.addEventListener("click", () => createChatSession().catch(showToast));
  const createProject = document.getElementById("project-create-btn");
  if (createProject) {
    createProject.addEventListener("click", () => createProjectFromForm().catch(showToast));
  }
  const toggle = document.getElementById("project-picker-btn");
  const picker = document.getElementById("project-picker");
  const input = document.getElementById("project-id-input");
  if (toggle && picker) {
    toggle.addEventListener("click", () => {
      const willOpen = picker.hidden;
      picker.hidden = !willOpen;
      toggle.setAttribute("aria-expanded", willOpen ? "true" : "false");
      if (willOpen) {
        renderProjectOptions();
        if (input) input.focus();
      }
    });
  }
  if (input) {
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const value = input.value.trim();
      input.value = "";
      if (value) openProject(value).catch(showToast);
    });
  }
  const chatSearch = document.getElementById("chat-search");
  const chatList = document.getElementById("chat-list");
  if (chatSearch && chatList) {
    chatSearch.addEventListener("input", () => {
      state.chatQuery = chatSearch.value.trim();
      loadChats().catch(() => {});
    });
  }
  window.addEventListener("hashchange", () => {
    // 自己设的 hash 不再重复导航；但只认"那一个"，不会吞掉随后用户点的导航
    if (expectedHash && location.hash === expectedHash) {
      expectedHash = "";
      return;
    }
    expectedHash = "";
    applyRoute().catch(showToast);
  });
}

/** 门面：切换工作区（左侧栏一级入口 / window.OrchestratorContext.switchTo 都用它）。 */
function switchWorkspace(contextType, options = {}) {
  const type = normalizeContextType(contextType);
  if (type === CONTEXT_CHAT) {
    ProjectWorkspace.projectId = "";
    enterChat(options.chatId || ProjectWorkspace.chatId || "").catch(showToast);
    return type;
  }
  const remembered = options.projectId || ProjectWorkspace.projectId || "";
  if (remembered) {
    enterProject(remembered, options.module || ProjectWorkspace.module).catch(showToast);
  } else {
    showProjects().catch(showToast);
  }
  return type;
}

