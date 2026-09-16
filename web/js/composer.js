/* 底部输入区与提交任务（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
/* ── 输入区 ── */

function updateComposer() {
  const ready = state.settings?.ready !== false;
  if (ProjectWorkspace.contextType === CONTEXT_CHAT) {
    dom.sendBtn.disabled = false;
    dom.cancelBtn.hidden = true;
    dom.taskInput.placeholder = "说点什么…（Enter 发送，Shift+Enter 换行）";
    dom.composerHint.textContent = ready
      ? "普通对话：Enter 发送 · 只收发消息，不产生纲领与步骤"
      : "尚未配置中转地址 / Key：点右上角 ⚙ 填写并保存后即可对话。";
    return;
  }
  dom.taskInput.placeholder =
    "描述目标，例如：为现有 FastAPI 项目补一套可回滚的数据迁移流程（Enter 发送，Shift+Enter 换行）";
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
    dom.composerHint.textContent = "Enter 发送 · Shift+Enter 换行 · 将开启一个新的架构→执行流程";
  }
}

async function submitTask() {
  const task = dom.taskInput.value.trim();
  // 普通对话：同一套输入框发消息，不建运行、不动文件
  if (ProjectWorkspace.contextType === CONTEXT_CHAT) {
    if (!task) {
      showToast("说点什么再发送。");
      return;
    }
    if (state.settings && state.settings.ready === false) {
      showToast("尚未配置中转地址 / Key：点右上角 ⚙ 填写并保存后再发送。");
      return;
    }
    dom.taskInput.value = "";
    await sendChatMessage(task);
    return;
  }
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
      // 运行必须属于某个项目：没选项目时落到默认项目（历史数据容器）
      project_id: ProjectWorkspace.projectId || "default",
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

