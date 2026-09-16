/* 命令面板 + 侧栏槽位 + 插件市场入口（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
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

