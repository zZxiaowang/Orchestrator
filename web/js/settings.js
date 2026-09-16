/* 设置弹窗 / 分段路由 / 多套配置 / 对话框（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
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

/** 长上下文自动拆分：总开关关掉时，下面的可选项一起禁用（并说明会发生什么）。 */
function updateChatContextOptionState() {
  const master = document.getElementById("f-chat-context");
  const host = document.getElementById("chat-context-options");
  const autoSplit = document.getElementById("f-chat-auto-split");
  const status = document.getElementById("chat-context-status");
  if (!master || !host) return;
  const enabled = master.checked;
  host.querySelectorAll("input").forEach((input) => {
    input.disabled = !enabled;
  });
  if (autoSplit) autoSplit.disabled = !enabled;
  if (!status) return;
  status.textContent = enabled
    ? `已开启：只发最近 ${document.getElementById("f-chat-window-turns").value || 12} 轮原文，` +
      `更早的折叠进摘要${autoSplit && autoSplit.checked ? "；摘要超限时自动开新对话" : "（不自动开新对话，只提示）"}。`
    : "已关闭：普通对话会把全部历史原样发出去（最贵，仅建议排查问题时临时关闭）。";
}

function openSettings(section = "model") {
  const settings = state.settings;
  if (!settings) return;
  document.getElementById("f-max-steps").value = settings.max_plan_steps || 8;
  // 普通对话：长上下文自动拆分（开关 + 可选项）
  document.getElementById("f-chat-context").checked = settings.chat_context_enabled !== false;
  document.getElementById("f-chat-window-turns").value = settings.chat_window_turns ?? 12;
  document.getElementById("f-chat-window-chars").value = settings.chat_window_chars ?? 6000;
  document.getElementById("f-chat-fold-batch").value = settings.chat_fold_batch ?? 4;
  document.getElementById("f-chat-summary-max").value = settings.chat_summary_max_chars ?? 2000;
  document.getElementById("f-chat-auto-split").checked = settings.chat_auto_split !== false;
  updateChatContextOptionState();
  // MCP 工具调用（模型可自主请求）
  document.getElementById("f-mcp-enabled").checked = settings.mcp_enabled !== false;
  document.getElementById("f-mcp-rounds").value = settings.mcp_call_rounds ?? 2;
  document.getElementById("f-mcp-max-calls").value = settings.mcp_max_calls_per_step ?? 3;
  // 上下文预算与验收补轮
  document.getElementById("f-verify-rounds").value = settings.step_verify_rounds ?? 2;
  document.getElementById("f-context-budget").value = settings.context_budget_chars ?? 24000;
  document.getElementById("f-file-max").value = settings.file_context_max_chars ?? 2400;
  document.getElementById("f-completed-log").value = settings.completed_log_max_chars ?? 1200;
  document.getElementById("f-fetch-rounds").value = settings.step_fetch_rounds ?? 3;
  document.getElementById("f-allow-cmd").checked = Boolean(settings.allow_command_execution);
  document.getElementById("f-command-allowlist").value = (
    settings.command_allowlist || []
  ).join("\n");
  document.getElementById("f-command-rounds").value = settings.step_command_rounds ?? 2;
  const backups = [
    ["architect", settings.architect_backup || {}],
    ["editor", settings.editor_backup || {}],
  ];
  for (const [role, backup] of backups) {
    document.getElementById(`f-${role}-backup-base`).value = backup.base_url || "";
    document.getElementById(`f-${role}-backup-model`).value = backup.model || "";
    document.getElementById(`f-${role}-backup-label`).value = backup.label || "";
    document.getElementById(`f-${role}-backup-wire`).value = backup.wire_api || "";
    const keyField = document.getElementById(`f-${role}-backup-key`);
    keyField.value = "";
    keyField.placeholder = backup.api_key_set
      ? `已保存：${backup.api_key_masked}（留空表示不修改）`
      : "留空表示不修改";
  }
  document.getElementById("backup-status").textContent = backups.some(
    ([, backup]) => backup.base_url && backup.api_key_set
  )
    ? "备用配置已启用：主用失败时自动切换。"
    : "未配置备用：主用失败会如实报错（会给出可操作建议）。";
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
    // 普通对话的长上下文管理：开关 + 可选项一起保存
    chat_context_enabled: document.getElementById("f-chat-context").checked,
    chat_auto_split: document.getElementById("f-chat-auto-split").checked,
    chat_window_turns: Number(document.getElementById("f-chat-window-turns").value) || 12,
    chat_window_chars: Number(document.getElementById("f-chat-window-chars").value) || 6000,
    chat_fold_batch: Number(document.getElementById("f-chat-fold-batch").value) || 4,
    chat_summary_max_chars:
      Number(document.getElementById("f-chat-summary-max").value) || 2000,
    // MCP：模型可请求调用工具（工具本身仍要已启用 + 已确认信任）
    mcp_enabled: document.getElementById("f-mcp-enabled").checked,
    mcp_call_rounds: Number(document.getElementById("f-mcp-rounds").value) || 2,
    mcp_max_calls_per_step: Number(document.getElementById("f-mcp-max-calls").value) || 3,
    // 上下文预算与验收补轮
    step_verify_rounds: Number(document.getElementById("f-verify-rounds").value) || 0,
    context_budget_chars: Number(document.getElementById("f-context-budget").value) || 24000,
    file_context_max_chars: Number(document.getElementById("f-file-max").value) || 2400,
    completed_log_max_chars: Number(document.getElementById("f-completed-log").value) || 1200,
    step_fetch_rounds: Number(document.getElementById("f-fetch-rounds").value) || 0,
    allow_command_execution: document.getElementById("f-allow-cmd").checked,
    command_allowlist: String(document.getElementById("f-command-allowlist").value || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
    step_command_rounds: Number(document.getElementById("f-command-rounds").value) || 0,
    // 备用配置：Key 留空 = 不修改（和主 Key 一致的约定）
    architect_backup_base_url: document.getElementById("f-architect-backup-base").value.trim(),
    architect_backup_wire_api: document.getElementById("f-architect-backup-wire").value,
    architect_backup_model: document.getElementById("f-architect-backup-model").value.trim(),
    architect_backup_label: document.getElementById("f-architect-backup-label").value.trim(),
    editor_backup_base_url: document.getElementById("f-editor-backup-base").value.trim(),
    editor_backup_wire_api: document.getElementById("f-editor-backup-wire").value,
    editor_backup_model: document.getElementById("f-editor-backup-model").value.trim(),
    editor_backup_label: document.getElementById("f-editor-backup-label").value.trim(),
  };
  for (const [field, elementId] of [
    ["architect_backup_api_key", "f-architect-backup-key"],
    ["editor_backup_api_key", "f-editor-backup-key"],
  ]) {
    const typed = document.getElementById(elementId).value.trim();
    if (typed) globals[field] = typed;
  }
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

