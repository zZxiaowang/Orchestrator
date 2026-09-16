/* 运行列表 + 打开运行 + SSE 事件处理（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
/* ── 运行列表 ── */

async function refreshRuns() {
  // 普通对话没有运行列表；项目的运行列表只列**本项目**的运行
  if (ProjectWorkspace.contextType === CONTEXT_CHAT) {
    state.runs = [];
    renderRunList();
    return;
  }
  const token = ++state.tokens.runs;
  try {
    const params = {
      include_archived: state.showArchived,
      q: state.runQuery,
    };
    if (ProjectWorkspace.projectId) params.project_id = ProjectWorkspace.projectId;
    const payload = await api.runs(params);
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
  // 运行列表是"全量重建"的（每条 4 个节点 + 3 个按钮）：
  // 记录一多，每次 render() 都要重建上百条 —— 实测一次运行里产生了 1992 次 DOM 变更，
  // 真实模型下 token 事件持续几分钟，界面就是这样被拖到卡死的。
  // 这里先比签名：数据没变就不动 DOM。
  const listSignature = JSON.stringify([
    state.run ? state.run.id : "",
    state.runQuery,
    state.showArchived,
    state.runs.map((item) => [
      item.id,
      item.status,
      item.pinned,
      item.archived,
      item.steps_done,
      item.steps_total,
      item.files_changed,
    ]),
  ]);
  if (listSignature === lastRunListSignature) return;
  lastRunListSignature = listSignature;
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

function connectStream(id, since, options = {}) {
  disconnectStream();
  const base = options.chat ? `/api/v1/chats/${id}/events` : `/api/v1/runs/${id}/events`;
  const url = `${base}?since=${Number(since) || 0}`;
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
      // 合并到每帧写一次：逐块写 textContent + scrollTop 会强制同步重排，
      // 长时间流式输出会把界面拖死（这就是"界面卡死"的主因之一）。
      queueStreamUpdate(key);
      if (event.data.phase === "executor") {
        const card = document.querySelector(`[data-step="${event.data.step_id}"]`);
        if (card) card.dataset.status = "running";
      }
      if (event.data.phase === "chat" && ProjectWorkspace.contextType === CONTEXT_CHAT) {
        // 普通对话：按帧合并重绘线程（和运行时间线同一套节流，避免逐 token 重排）
        const chat = WorkspaceState.chatPayload;
        if (chat && chat.status !== "planning") chat.status = "planning";
        scheduleRender();
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
      if (ProjectWorkspace.contextType === CONTEXT_CHAT && WorkspaceState.chatPayload) {
        WorkspaceState.chatPayload.status = event.data.status;
        scheduleRender();
        break;
      }
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
      // 普通对话会话的"回答完成"要把消息落进线程，而不是去拉运行详情
      if (ProjectWorkspace.contextType === CONTEXT_CHAT) {
        loadChatDetail(ProjectWorkspace.chatId).then(() => {
          loadChats();
          render();
        });
        break;
      }
      refreshRun();
      break;
    }
    case "context_folded": {
      // 历史被折叠进摘要：让用户看得见"省了什么"，而不是悄悄丢上下文
      const chat = WorkspaceState.chatPayload;
      if (chat) {
        chat.folded_turns = event.data.total_folded ?? chat.folded_turns;
        chat.summary_chars = event.data.summary_chars ?? chat.summary_chars;
        if (event.data.summary_preview) chat.summary = event.data.summary_preview;
      }
      showToast(event.data.message || "已折叠较早的历史为摘要");
      scheduleRender();
      break;
    }
    case "session_split": {
      // 后端开了新会话承接：跟着切过去，并把旧会话留在列表里
      const nextId = event.data.next_session_id;
      if (!nextId) break;
      showToast(event.data.message || "已自动开启新对话（承接摘要）");
      loadChats();
      enterChat(nextId).catch(() => {});
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
    case "fallback": {
      // 主用配置失败并切到备用：必须让用户知道，否则"怎么突然用别的配置了"
      if (state.run) {
        state.statusHint = event.data.message || "主用配置失败，已切换到备用配置";
        updateStatus();
      }
      showToast(event.data.message || "主用配置失败，已切换到备用配置");
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

