/* 主区渲染（时间线 / 步骤卡片 / 折叠） + 检查器（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
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
  // 普通对话没有运行操作（架构 / 执行 / 取消 / 重启都属于项目）
  if (ProjectWorkspace.contextType === CONTEXT_CHAT) {
    host.replaceChildren();
    return;
  }
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
  if (status === "failed") {
    // 网关抖动（502/503）重试一次往往就过了，不该重建任务
    nodes.push(
      button("重试", "primary", async () => {
        const payload = await api.retryRun(run.id);
        state.run = payload.run;
        state.buffers = {};
        connectStream(run.id, state.lastSeq);
        render();
      })
    );
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
  if (ProjectWorkspace.contextType === CONTEXT_CHAT) {
    dom.statusPill.dataset.status = "chat";
    const chat = WorkspaceState.chatPayload;
    dom.statusPill.textContent = chat && chat.status === "planning" ? "正在回答" : "普通对话";
    dom.runTitle.textContent = chat ? chat.title : "新对话";
    return;
  }
  if (ProjectWorkspace.module !== "execution" || !ProjectWorkspace.projectId) {
    const card = (WorkspaceState.projects || []).find(
      (item) => item.project_id === ProjectWorkspace.projectId
    );
    dom.statusPill.dataset.status = "project";
    dom.statusPill.textContent = PROJECT_SECTION_LABELS[ProjectWorkspace.module] || "项目";
    dom.runTitle.textContent = card
      ? `${card.name} · ${PROJECT_SECTION_LABELS[ProjectWorkspace.module] || ""}`
      : "项目";
    return;
  }
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
  // 主区内容取决于"现在在哪个工作区 / 哪个模块"：
  // 普通对话 → 消息线程；项目 → 该模块的真实数据（执行模块走下面的运行时间线）。
  if (ProjectWorkspace.contextType === CONTEXT_CHAT) {
    lastTimelineSignature = "";
    renderChatThread();
    return;
  }
  // 项目模块加载失败（例如项目不存在）：直接进错误态，不要落到"运行时间线"这种空视图
  if (WorkspaceState.moduleError) {
    lastTimelineSignature = "";
    lastModuleSignature = "";
    renderModuleView();
    return;
  }
  if (ProjectWorkspace.module !== "execution" || !ProjectWorkspace.projectId) {
    lastTimelineSignature = "";
    renderModuleView();
    return;
  }
  const run = state.run;
  if (!run) {
    dom.timeline.replaceChildren(renderEmptyState());
    lastTimelineSignature = "";
    return;
  }
  // 结构签名：状态/步骤/文件/验收/命令/错误没变就不重建卡片。
  // 流式文本由 queueStreamUpdate 就地更新，不依赖重建。
  const signature = JSON.stringify([
    run.id,
    run.kind || "task",
    run.status,
    run.plan_revision,
    run.error && run.error.message,
    (run.steps || []).map((step) => [
      step.id,
      step.status,
      (step.summary || "").length,
      (step.files || []).length,
      (step.verification || []).length,
      (step.command_results || []).length,
      step.retries,
    ]),
  ]);
  if (signature === lastTimelineSignature) return;
  lastTimelineSignature = signature;

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
    const rawText = architectBuffer || (architectRaw ? architectRaw.content : "");
    const parsed = Boolean(run.plan);
    // 和 Codex 一样：正在流式生成时展开（看得见输出），出完纲领就折起来只留一行摘要
    nodes.push(
      collapsibleCard(`architect:${run.id}`, "架构段输出 · " + (run.route?.architect?.model || "GPT"), {
        defaultOpen: !parsed,
        subtitle: parsed ? `已解析为纲领 · ${rawText.length} 字` : "流式生成中…",
        children: [
          h("pre", {
            class: "stream",
            dataset: { stream: "architect" },
            text: rawText,
          }),
        ],
      })
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
    // 可操作的提示（怎么处理）必须一起显示，不能只丢网关原文
    const hint = run.error.hint || run.error.details?.hint || "";
    const retryBtn = h("button", { class: "btn ghost small", type: "button", text: "重试" });
    retryBtn.addEventListener(
      "click",
      safe(async () => {
        const payload = await api.retryRun(run.id);
        state.run = payload.run;
        state.buffers = {};
        connectStream(run.id, state.lastSeq);
        render();
      })
    );
    nodes.push(
      h(
        "div",
        { class: "error-box" },
        h("div", { text: `运行失败：${run.error.message || JSON.stringify(run.error)}` }),
        hint && hint !== run.error.message
          ? h("div", { class: "error-hint", text: `建议：${hint}` })
          : null,
        h("div", { class: "step-actions" }, retryBtn),
      )
    );
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

/** 把"这一步花了多少"压成一行：耗时 · token（估算标注）· 上下文 · 轮次。 */
function stepSpend(step) {
  const metric = (state.run?.metrics || []).find(
    (item) => item.phase === "executor" && Number(item.step_id) === Number(step.id)
  );
  if (!metric) {
    return step.context_chars
      ? `上下文 ${(step.context_chars / 1000).toFixed(1)}k 字符`
      : "";
  }
  const parts = [];
  if (metric.duration_ms) {
    const seconds = metric.duration_ms / 1000;
    parts.push(
      seconds < 60 ? `⏱ ${seconds.toFixed(1)}s` : `⏱ ${Math.floor(seconds / 60)}m${Math.round(seconds % 60)}s`
    );
  }
  if (typeof metric.total_tokens === "number") {
    const tag = metric.usage_source === "estimated" ? "（估算）" : "";
    parts.push(`🪙 ${metric.total_tokens.toLocaleString("zh-CN")} tokens${tag}`);
  } else {
    parts.push("🪙 用量未知");
  }
  const contextChars = metric.context_chars || step.context_chars || 0;
  if (contextChars) parts.push(`上下文 ${(contextChars / 1000).toFixed(1)}k`);
  const rounds = metric.rounds || {};
  const extra = (rounds.fetch || 0) + (rounds.repair || 0);
  parts.push(`${metric.calls || 1} 次调用${extra ? `（含索取 ${rounds.fetch || 0} / 修错 ${rounds.repair || 0}）` : ""}`);
  return parts.join(" · ");
}

/** 步骤卡片的展开状态。已完成（done）的默认折叠，其余一律展开——需要人关注的不该藏起来。 */
function stepIsOpen(step) {
  const remembered = state.stepOpen[`${state.run?.id || ""}:${step.id}`];
  if (remembered && remembered.status === (step.status || "pending")) return remembered.open;
  return (step.status || "pending") !== "done";
}

function setStepOpen(step, open) {
  state.stepOpen[`${state.run?.id || ""}:${step.id}`] = {
    status: step.status || "pending",
    open,
  };
}

function renderStepCard(step) {
  const status = step.status || "pending";
  const body = h("div", { class: "card-body" });
  // 这一步花了多少：耗时 / token（真实或估算）/ 调用轮次构成
  const spent = stepSpend(step);
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
  if (step.skills_used?.length) {
    body.append(
      h("p", {
        class: "muted",
        text: `本步注入的技能：${step.skills_used.join("、")}（按触发词匹配，来自「能力中心」）`,
      })
    );
  }
  if (step.tool_results?.length) {
    body.append(
      h("h3", {
        class: "section-label",
        text: `工具调用（${step.tool_results.filter((item) => item.ok).length}/${
          step.tool_results.length
        } 成功）`,
      })
    );
    for (const item of step.tool_results) {
      const ok = item.ok;
      const row = h("div", {
        class: `cmd-row ${ok ? "verify-ok" : "verify-fail"}`,
        text: `${ok ? "✓" : "✗"} ${item.capability_id} → ${item.tool}（${
          item.duration_ms || 0
        }ms）${ok ? "" : `：${item.error || "失败"}`}`,
      });
      if (item.content) {
        const pre = h("pre", { class: "cmd-output", hidden: true, text: item.content });
        row.classList.add("clickable");
        row.addEventListener("click", () => {
          pre.hidden = !pre.hidden;
        });
        body.append(row, pre);
      } else {
        body.append(row);
      }
    }
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

  // 执行长任务时，已完成的步骤把明细收起来（只留标题那一行摘要），
  // 视野留给正在跑的那一步；点标题行随时展开回去，选择会记住。
  const open = stepIsOpen(step);
  body.hidden = !open;
  const caret = h("span", { class: "step-caret", text: open ? "▾" : "▸", "aria-hidden": "true" });
  const head = h(
    "div",
    {
      class: `card-head step-head${open ? "" : " is-collapsed"}`,
      role: "button",
      tabindex: "0",
      "aria-expanded": String(open),
      title: open ? "点击收起这一步" : "点击展开这一步",
    },
    caret,
    h("div", { class: "step-index", text: step.id }),
    h("strong", { text: step.title || `第 ${step.id} 步` }),
    h("span", {
      class: "muted",
      text:
        (status === "running" ? stepLiveText(step) : STEP_STATUS_TEXT[status] || status) +
        (spent ? ` · ${spent}` : "") +
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
  );

  const applyOpen = (next) => {
    body.hidden = !next;
    caret.textContent = next ? "▾" : "▸";
    head.classList.toggle("is-collapsed", !next);
    head.setAttribute("aria-expanded", String(next));
    head.title = next ? "点击收起这一步" : "点击展开这一步";
  };
  const toggle = () => {
    const next = !stepIsOpen(step);
    setStepOpen(step, next);
    applyOpen(next);
  };
  head.addEventListener(
    "click",
    safe((event) => {
      // 标题行里还有「看变更 →」「复制」这类按钮，别把它们的点击当成折叠
      if (event.target.closest("button")) return;
      toggle();
    })
  );
  head.addEventListener(
    "keydown",
    safe((event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      toggle();
    })
  );

  return h(
    "div",
    { class: "card step", dataset: { step: step.id, status } },
    head,
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
