/* 主区视图：对话线程 / 项目模块（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
/* ── 主区渲染：对话线程 / 项目模块（真实数据）──────────────────────── */

function cardBlock(title, ...children) {
  return h(
    "div",
    { class: "card" },
    h("div", { class: "card-head" }, h("strong", { text: title })),
    h("div", { class: "card-body" }, ...children)
  );
}

function cardIsOpen(key, defaultOpen) {
  const remembered = state.cardOpen[key];
  return remembered === undefined ? Boolean(defaultOpen) : Boolean(remembered);
}

function setCardOpen(key, open) {
  state.cardOpen[key] = Boolean(open);
}

/** 可折叠卡片：折叠时只留标题行（标题 + 一行摘要），点标题行**原地**展开/收起。

  和 Codex 执行过程的折叠一致：已经输出完的默认折起来、只留总结，
  正在输出的（例如架构段流式生成中）默认展开；用户点过之后按用户的选择走。
*/
function collapsibleCard(key, title, options = {}) {
  const {
    subtitle = "",
    defaultOpen = false,
    children = [],
    actions = [],
    extraHead = [],
    cardClass = "",
    dataset = {},
  } = options;
  const open = cardIsOpen(key, defaultOpen);
  const body = h("div", { class: "card-body" }, ...children);
  body.hidden = !open;
  const caret = h("span", { class: "step-caret", text: open ? "▾" : "▸", "aria-hidden": "true" });
  const head = h(
    "div",
    {
      class: `card-head step-head${open ? "" : " is-collapsed"}`,
      role: "button",
      tabindex: "0",
      "aria-expanded": String(open),
      title: open ? "点击收起" : "点击展开",
    },
    caret,
    ...extraHead,
    h("strong", { text: title }),
    subtitle ? h("span", { class: "muted", text: subtitle }) : null,
    ...actions
  );
  const applyOpen = (next) => {
    body.hidden = !next;
    caret.textContent = next ? "▾" : "▸";
    head.classList.toggle("is-collapsed", !next);
    head.setAttribute("aria-expanded", String(next));
    head.title = next ? "点击收起" : "点击展开";
  };
  const toggle = () => {
    const next = !cardIsOpen(key, defaultOpen);
    setCardOpen(key, next);
    applyOpen(next);
  };
  head.addEventListener(
    "click",
    safe((event) => {
      // 标题行里的按钮（看变更 / 复制 / 去执行）有自己的动作，不算折叠
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
    { class: cardClass ? `card ${cardClass}` : "card", dataset },
    head,
    body
  );
}

function kvLine(label, value) {
  return h(
    "div",
    { class: "kv" },
    h("h3", { text: label }),
    h("div", { text: value || "—" })
  );
}

/** 没选项目时的主区：明确告诉用户去哪儿选 / 新建。 */
function renderProjectListView() {
  const projects = WorkspaceState.projects || [];
  const signature = JSON.stringify(
    projects.map((item) => [item.project_id, item.name, item.runs, item.steps_done])
  );
  if (signature === lastProjectListSignature && dom.timeline.childElementCount) return;
  lastProjectListSignature = signature;
  const nodes = [
    cardBlock(
      "项目",
      h("p", {
        text: "架构、计划、执行、验证都在项目内部进行；项目与工作区一一绑定，运行记录按项目隔离。",
      })
    ),
  ];
  if (!projects.length) {
    nodes.push(
      cardBlock(
        "还没有项目",
        h("p", {
          class: "muted",
          text: "点当前项目旁的 ▾ 展开项目面板，填一个名字即可新建（工作区目录可留空）。",
        })
      )
    );
  } else {
    const list = h("div", { class: "module-list" });
    for (const project of projects) {
      const row = h(
        "button",
        { class: "module-row", type: "button", dataset: { projectId: project.project_id } },
        h("div", { class: "run-name", text: project.name }),
        h("div", {
          class: "run-meta",
          text: [
            project.project_id,
            `${project.runs || 0} 次运行`,
            `${project.steps_done || 0}/${project.steps_total || 0} 步`,
            project.workspace?.root_path || "未绑定工作区",
          ].join(" · "),
        })
      );
      row.addEventListener("click", () => openProject(project.project_id).catch(showToast));
      list.append(row);
    }
    nodes.push(cardBlock("全部项目", list));
  }
  dom.timeline.replaceChildren(...nodes);
}

function chatMessageNode(message) {
  const isUser = message.role === "user";
  // 系统提示（例如"已自动开启新对话"）：单独一种样式，别混进对话气泡里
  if (message.role === "system") {
    return h("div", { class: "card chat-notice" }, h("div", { class: "card-body", text: message.content }));
  }
  const body = message.streaming
    ? // 流式回答就地写入（data-stream="chat"）：不再逐 token 重建整页
      h("pre", { class: "stream", dataset: { stream: "chat" }, text: message.content || "…" })
    : h("div", { class: "card-body", text: message.content });
  return h(
    "div",
    { class: `card ${isUser ? "msg-user" : "msg-assistant"}` },
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "avatar", text: isUser ? "你" : "答" }),
      h("strong", { text: isUser ? "你" : "回答" }),
      h("span", { class: "muted", text: message.model || "" })
    ),
    body
  );
}

/** 普通对话线程：消息只属于本会话，没有纲领 / 步骤 / 验收。 */
function renderChatThread() {
  const chat = WorkspaceState.chatPayload;
  if (!chat) {
    // 错误态：会话加载失败时给出原因与重试，不装成"新对话"
    if (WorkspaceState.chatError) {
      const retry = h("button", { class: "btn primary small", type: "button", text: "重试" });
      retry.addEventListener(
        "click",
        safe(async () => {
          await loadChatDetail(ProjectWorkspace.chatId);
          render();
        })
      );
      dom.timeline.replaceChildren(
        h(
          "div",
          { class: "card" },
          h("div", { class: "card-head" }, h("strong", { text: "对话加载失败" })),
          h(
            "div",
            { class: "card-body" },
            h("div", { class: "error-box", text: WorkspaceState.chatError }),
            h("div", { class: "approval-actions" }, retry)
          )
        )
      );
      return;
    }
    lastChatSignature = "";
    dom.timeline.replaceChildren(
      cardBlock(
        "开始一段新对话",
        h("p", {
          text: "普通对话不绑定项目：只收发消息，不生成纲领与步骤，也不改动任何文件。",
        }),
        h("p", { class: "muted", text: "在下面的输入框里说点什么，回车发送。" })
      )
    );
    return;
  }
  // 结构签名：会话数据没变就不重建 DOM（流式文本由 flushStreams 就地写入）
  const signature = JSON.stringify([
    chat.id,
    chat.status,
    (chat.messages_list || []).length,
    chat.summary_chars,
    chat.folded_turns,
    chat.prev_session_id,
    chat.next_session_id,
    chat.error,
  ]);
  if (signature === lastChatSignature && dom.timeline.querySelector(".chat-thread")) return;
  lastChatSignature = signature;
  const streaming = state.buffers.chat || "";
  const nodes = (chat.messages_list || []).map((message) => chatMessageNode(message));
  // 承接链与上下文账：这段对话从哪来、折叠了多少、上一轮实际发了多少字符
  const contextBits = [];
  if (chat.folded_turns) contextBits.push(`已折叠 ${chat.folded_turns} 轮为摘要`);
  if (chat.summary_chars) contextBits.push(`摘要 ${chat.summary_chars} 字`);
  if (chat.last_context_chars) contextBits.push(`上一轮发送 ${chat.last_context_chars} 字符`);
  if (contextBits.length || chat.prev_session_id || chat.next_session_id) {
    const actions = h("div", { class: "approval-actions" });
    if (chat.prev_session_id) {
      const back = h("button", { class: "btn ghost small", type: "button", text: "← 回到上一段对话" });
      back.addEventListener("click", safe(() => enterChat(chat.prev_session_id).catch(showToast)));
      actions.append(back);
    }
    if (chat.next_session_id) {
      const forward = h("button", { class: "btn ghost small", type: "button", text: "下一段对话 →" });
      forward.addEventListener("click", safe(() => enterChat(chat.next_session_id).catch(showToast)));
      actions.append(forward);
    }
    nodes.unshift(
      h(
        "div",
        { class: "card chat-context" },
        h(
          "div",
          { class: "card-head" },
          h("strong", { text: "长上下文管理" }),
          h("span", { class: "muted", text: contextBits.join(" · ") || "这段对话是新开的" })
        ),
        h(
          "div",
          { class: "card-body" },
          chat.prev_session_id
            ? h("p", { class: "muted", text: "本对话承接自上一段：更早的内容已压成摘要，其余原文留在上一段对话里。" })
            : null,
          chat.summary
            ? h(
                "details",
                {},
                h("summary", { text: `承接摘要（${chat.summary_chars || chat.summary.length} 字）` }),
                h("pre", { class: "stream", text: chat.summary })
              )
            : null,
          actions.childElementCount ? actions : null
        )
      )
    );
  }
  const answering = chat.status === "planning";
  if (answering) {
    nodes.push(
      chatMessageNode({ role: "assistant", content: streaming, streaming: true, model: "" })
    );
  }
  const thread = h("div", { class: "chat-thread" }, ...nodes);
  const extras = [];
  if (chat.error) extras.push(h("div", { class: "error-box", text: `回答失败：${chat.error}` }));
  dom.timeline.replaceChildren(thread, ...extras);
  dom.timeline.scrollTop = dom.timeline.scrollHeight;
}

/** 项目模块头：项目名 + 模块名 + 计数 + 主行动。 */
function moduleHeader(payload) {
  const project = payload.project || {};
  const counts = payload.counts || {};
  const actions = h("div", { class: "approval-actions" });
  const newRun = h("button", {
    class: "btn primary small",
    type: "button",
    text: "＋ 新建任务",
  });
  newRun.addEventListener(
    "click",
    safe(() => {
      selectProjectSection("execution");
      document.getElementById("task-input")?.focus();
    })
  );
  actions.append(newRun);
  return h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "avatar", text: "◈" }),
      h("strong", { text: `${project.name || project.project_id} · ${payload.label || ""}` }),
      h("span", {
        class: "muted",
        text: `${counts.runs || 0} 次运行 · ${counts.steps_done || 0}/${counts.steps_total || 0} 步完成 · ${counts.files_changed || 0} 个文件改动`,
      })
    ),
    h(
      "div",
      { class: "card-body" },
      h("p", {
        class: "muted",
        text: project.workspace?.root_path
          ? `工作区：${project.workspace.root_path}`
          : "该项目未绑定工作区（历史数据容器）",
      }),
      actions
    )
  );
}

function runRows(payload) {
  // 概览只列最近若干次运行：一百条折叠卡片既慢又没人看，全量在「执行」的列表里
  const all = payload.runs || [];
  const runs = all.slice(0, OVERVIEW_RUN_LIMIT);
  if (!runs.length) {
    return h("p", { class: "muted", text: "这个项目还没有运行记录。" });
  }
  const list = h("div", { class: "module-list" });
  for (const run of runs) {
    // 概览里点运行记录**原地展开**，不再把人甩到别的模块去
    const goExecution = h("button", {
      class: "btn ghost small",
      type: "button",
      text: "去执行看时间线 →",
    });
    goExecution.addEventListener(
      "click",
      safe(async () => {
        await selectProjectSection("execution");
        await openRun(run.id);
        render();
      })
    );
    list.append(
      collapsibleCard(`overview:run:${run.id}`, run.title || run.id, {
        subtitle: `${run.status} · ${run.steps?.done || 0}/${run.steps?.total || 0} 步 · ${
          run.files_changed || 0
        } 个文件改动`,
        children: [
          h("p", { text: run.task || "（没有填写任务描述）" }),
          h("p", {
            class: "muted",
            text: `运行 ID：${run.id} · 更新于 ${run.updated_at || "—"}`,
          }),
          run.error ? h("div", { class: "error-box", text: run.error }) : null,
          h("div", { class: "approval-actions" }, goExecution),
        ],
      })
    );
  }
  if (all.length > runs.length) {
    list.append(
      h("p", {
        class: "muted",
        text: `共 ${all.length} 次运行，这里只列最近 ${runs.length} 次；全部记录见左侧「运行记录」。`,
      })
    );
  }
  return list;
}

function overviewNodes(payload) {
  const runs = payload.runs || [];
  const nodes = [
    h("div", { class: "section-label", text: `运行记录（${runs.length}）` }),
    runRows(payload),
  ];
  if (!runs.length) {
    nodes.push(
      cardBlock(
        "还没有运行",
        h("p", { class: "muted", text: "点上面的「＋ 新建任务」开始：架构段先出纲领，确认后执行。" })
      )
    );
  }
  return nodes;
}

function architectureNodes(payload) {
  const data = payload.architecture || {};
  if (!data.has_plan) {
    return [
      cardBlock(
        "尚未生成架构",
        h("p", { class: "muted", text: "在「执行」里新建任务，架构段会先产出纲领与验收标准。" })
      ),
    ];
  }
  const nodes = [
    // 折叠时标题行就是它的一句话摘要——收起来也不丢信息
    collapsibleCard("architecture:goal", "纲领目标", {
      subtitle: (data.goal || "").slice(0, 60),
      children: [
        h("p", { class: "plan-goal", text: data.goal || "（未写目标）" }),
        data.summary ? h("p", { text: data.summary }) : null,
        h("p", {
          class: "muted",
          text: `模型 ${data.metrics?.model || "—"} · ${(
            (data.metrics?.duration_ms || 0) / 1000
          ).toFixed(1)} 秒${
            typeof data.metrics?.total_tokens === "number"
              ? ` · ${data.metrics.total_tokens} tokens`
              : ""
          }`,
        }),
      ],
    }),
  ];
  if (data.principles?.length) {
    nodes.push(
      collapsibleCard("architecture:principles", "设计原则", {
        subtitle: `${data.principles.length} 条`,
        children: [
          h(
            "ul",
            { class: "list" },
            ...data.principles.map((item) => h("li", { text: item }))
          ),
        ],
      })
    );
  }
  if (data.components?.length) {
    nodes.push(
      collapsibleCard("architecture:components", "组件与职责", {
        subtitle: `${data.components.length} 个`,
        children: [
          h(
            "ul",
            { class: "list" },
            ...data.components.map((item) =>
              h("li", {
                text: `${item.name}：${item.responsibility}${
                  item.interfaces?.length ? `（接口：${item.interfaces.join("、")}）` : ""
                }`,
              })
            )
          )
        ],
      })
    );
  }
  if (data.risks?.length) {
    nodes.push(
      collapsibleCard("architecture:risks", "风险", {
        subtitle: `${data.risks.length} 条`,
        children: [
          h("ul", { class: "list" }, ...data.risks.map((item) => h("li", { text: item }))),
        ],
      })
    );
  }
  if (data.open_questions?.length) {
    nodes.push(
      collapsibleCard("architecture:questions", "待澄清", {
        subtitle: `${data.open_questions.length} 条`,
        children: [
          h(
            "ul",
            { class: "list" },
            ...data.open_questions.map((item) => h("li", { text: item }))
          ),
        ],
      })
    );
  }
  if (data.raw) {
    nodes.push(
      collapsibleCard("architecture:raw", "架构段原始输出", {
        subtitle: `${data.raw.length} 字`,
        children: [h("pre", { class: "stream", text: data.raw })],
      })
    );
  }
  return nodes;
}

function planNodes(payload) {
  const data = payload.plan || {};
  if (!data.has_plan) {
    return [
      cardBlock("尚无计划", h("p", { class: "muted", text: "计划来自架构段的纲领：在「执行」里新建任务即可生成。" })),
    ];
  }
  const nodes = [
    cardBlock(
      "计划",
      h("p", { class: "plan-goal", text: data.goal || "" }),
      h("p", { class: "muted", text: `共 ${data.steps?.length || 0} 步 · 修订第 ${data.plan_revision || 1} 版` })
    ),
  ];
  for (const step of data.steps || []) {
    nodes.push(
      collapsibleCard(`plan:step:${step.id}`, step.title || `第 ${step.id} 步`, {
        subtitle: `${STEP_STATUS_TEXT[step.status] || step.status} · 交付物 ${
          step.deliverables?.length || 0
        } 个 · 验收 ${step.acceptance?.length || 0} 条`,
        extraHead: [h("div", { class: "step-index", text: step.id })],
        cardClass: "step",
        dataset: { step: step.id, status: step.status },
        children: [
          h("p", { text: step.goal || "" }),
          step.deliverables?.length
            ? h("p", { class: "muted", text: `交付物：${step.deliverables.join("、")}` })
            : null,
          step.acceptance?.length
            ? h(
                "ul",
                { class: "list" },
                ...step.acceptance.map((item) => h("li", { text: item }))
              )
            : null,
          step.checks?.length
            ? h("p", {
                class: "muted",
                text: `客观检查：${step.checks.map((item) => item.type).join("、")}`,
              })
            : null,
        ],
      })
    );
  }
  return nodes;
}

function verificationNodes(payload) {
  const data = payload.verification || {};
  if (!data.has_run) {
    return [cardBlock("暂无验证结果", h("p", { class: "muted", text: "执行过步骤之后，这里会列出每一步的客观验收。" }))];
  }
  const totals = data.totals || {};
  const nodes = [
    cardBlock(
      "验证汇总",
      h("p", {
        text: `共 ${totals.total || 0} 条检查：通过 ${totals.passed || 0}，未通过 ${totals.failed || 0}${
          totals.unverified ? "（本步没有可自动判定的检查项）" : ""
        }`,
      }),
      data.unverified_steps?.length
        ? h("p", { class: "muted", text: `未验证的步骤：${data.unverified_steps.join("、")}` })
        : null
    ),
  ];
  for (const step of data.steps || []) {
    const items = (step.items || []).map((item) =>
      h("li", {
        class: item.ok ? "verify-ok" : "verify-fail",
        text: `${item.ok ? "✓" : "✗"} ${item.label || item.path}${item.detail ? ` — ${item.detail}` : ""}`,
      })
    );
    nodes.push(
      collapsibleCard(`verify:step:${step.id}`, `第 ${step.id} 步 · ${step.title || ""}`, {
        subtitle: step.summary
          ? `验收 ${step.summary.passed}/${step.summary.total}${
              step.summary.failed ? "（有未通过项）" : " 全部通过"
            }`
          : "未验证（没有可自动判定的检查项）",
        children: [
          items.length
            ? h("ul", { class: "verify-list" }, ...items)
            : h("p", { class: "muted", text: "没有可自动判定的检查项（未验证）" }),
        ],
      })
    );
  }
  return nodes;
}

function logsNodes(payload) {
  const data = payload.logs || {};
  if (!data.has_run) {
    return [cardBlock("暂无日志", h("p", { class: "muted", text: "这个项目还没有运行记录。" }))];
  }
  const nodes = [
    cardBlock(
      "运行指标",
      h("p", {
        class: "muted",
        text: `调用 ${data.metrics?.architect?.calls || 0} + ${data.metrics?.executor?.calls || 0} 次 · 耗时 ${(
          ((data.metrics?.architect?.duration_ms || 0) + (data.metrics?.executor?.duration_ms || 0)) / 1000
        ).toFixed(1)} 秒`,
      })
    ),
  ];
  // 日志条目同样折叠：折叠行是"阶段 · 角色 · 时间"，展开才看正文（与其它模块一致）
  const messages = (data.messages || []).map((item, index) =>
    collapsibleCard(`log:${index}`, `${item.phase} · ${item.role}`, {
      subtitle: `${item.model ? `${item.model} · ` : ""}${item.created_at}`,
      children: [h("pre", { class: "stream", text: item.content })],
    })
  );
  nodes.push(
    cardBlock(
      "事件与消息",
      ...(messages.length ? messages : [h("p", { class: "muted", text: "暂无消息。" })])
    )
  );
  if (data.commands?.length) {
    nodes.push(
      cardBlock(
        "验证命令",
        h(
          "ul",
          { class: "list" },
          ...data.commands.map((item) =>
            h("li", { text: `第 ${item.step_id} 步 ${item.skipped ? "（未执行）" : item.ok ? "✓" : "✗"} ${item.cmd}` })
          )
        )
      )
    );
  }
  return nodes;
}

function settingsNodes(payload) {
  const data = payload.settings || {};
  const project = payload.project || {};
  const nameInput = h("input", { type: "text", id: "project-name-edit", value: project.name || "" });
  const dirInput = h("input", {
    type: "text",
    id: "project-root-edit",
    value: project.workspace?.root_path || "",
    placeholder: "项目工作区根目录",
  });
  const descInput = h("input", {
    type: "text",
    id: "project-desc-edit",
    value: project.description || "",
    placeholder: "项目描述（可选）",
  });
  const save = h("button", { class: "btn primary small", type: "button", text: "保存项目设置" });
  save.addEventListener(
    "click",
    safe(async () => {
      save.disabled = true;
      try {
        await api.updateProject(project.project_id, {
          name: nameInput.value.trim(),
          description: descInput.value,
          root_path: dirInput.value.trim() || undefined,
        });
        await loadProjects();
        await loadModulePayload();
        showToast("项目设置已保存");
        render();
      } catch (error) {
        showToast(error.message);
      } finally {
        save.disabled = false;
      }
    })
  );
  const archive = h("button", { class: "btn ghost small", type: "button", text: "归档项目" });
  archive.addEventListener(
    "click",
    safe(async () => {
      const ok = await appConfirm({
        title: "归档项目",
        message: "归档后项目从列表里收起，运行记录与工作区都保留，可随时恢复。",
        confirmText: "归档",
      });
      if (!ok) return;
      await api.archiveProject(project.project_id);
      await loadProjects();
      await showProjects();
      showToast("项目已归档");
    })
  );
  return [
    cardBlock(
      "项目设置",
      h("label", { class: "inline-field" }, h("span", { text: "名称" }), nameInput),
      h("label", { class: "inline-field" }, h("span", { text: "工作区" }), dirInput),
      h("label", { class: "inline-field" }, h("span", { text: "描述" }), descInput),
      h("div", { class: "approval-actions" }, save, archive),
      h("p", { class: "muted", text: `项目 ID：${project.project_id}（稳定标识，不可修改）` })
    ),
    cardBlock(
      "模型路由（全局设置）",
      h("p", {
        text: `架构段 ${data.models?.architect?.model || "—"}${data.models?.architect?.host ? ` @ ${data.models.architect.host}` : ""}`,
      }),
      h("p", {
        text: `执行段 ${data.models?.editor?.model || "—"}${data.models?.editor?.host ? ` @ ${data.models.editor.host}` : ""}`,
      }),
      h("p", { class: "muted", text: "改模型 / 中转请用右上角标签或 ⚙ 设置：那是全局配置，不随项目变化。" })
    ),
  ];
}

/** 项目模块主区：概览 / 架构 / 计划 / 验证 / 日志 / 设置（执行模块走运行时间线）。 */
function renderModuleView() {
  if (!ProjectWorkspace.projectId) {
    lastModuleSignature = "";
    return renderProjectListView();
  }
  const payload = WorkspaceState.modulePayload;
  if (!payload) {
    // 错误态：说清原因 + 可重试，而不是一直显示"正在加载"
    if (WorkspaceState.moduleError) {
      const retry = h("button", { class: "btn primary small", type: "button", text: "重试" });
      retry.addEventListener(
        "click",
        safe(async () => {
          await loadModulePayload();
          render();
        })
      );
      const back = h("button", {
        class: "btn ghost small",
        type: "button",
        text: "回到项目列表",
      });
      back.addEventListener("click", safe(() => showProjects()));
      dom.timeline.replaceChildren(
        h(
          "div",
          { class: "card" },
          h("div", { class: "card-head" }, h("strong", { text: "模块加载失败" })),
          h(
            "div",
            { class: "card-body" },
            h("div", { class: "error-box", text: WorkspaceState.moduleError }),
            h("div", { class: "approval-actions" }, retry, back)
          )
        )
      );
      return;
    }
    lastModuleSignature = "";
    dom.timeline.replaceChildren(
      h("div", { class: "empty", text: "正在加载项目模块…" })
    );
    return;
  }
  const module = ProjectWorkspace.module;
  // 结构签名：载荷没变就不重建 DOM（否则每次 render 都会整块替换，触发重排风暴）
  const signature = JSON.stringify([
    module,
    payload.project?.project_id,
    payload.project?.name,
    payload.counts,
    payload.is_empty_state,
    payload.current_run?.id,
    payload.current_run?.status,
    (payload.runs || []).map((run) => [
      run.id,
      run.status,
      run.steps?.done,
      run.steps?.total,
    ]),
  ]);
  if (signature === lastModuleSignature && dom.timeline.childElementCount) return;
  lastModuleSignature = signature;
  let nodes = [moduleHeader(payload)];
  if (module === "overview") nodes = nodes.concat(overviewNodes(payload));
  else if (module === "architecture") nodes = nodes.concat(architectureNodes(payload));
  else if (module === "plan") nodes = nodes.concat(planNodes(payload));
  else if (module === "verification") nodes = nodes.concat(verificationNodes(payload));
  else if (module === "logs") nodes = nodes.concat(logsNodes(payload));
  else if (module === "settings") nodes = nodes.concat(settingsNodes(payload));
  dom.timeline.replaceChildren(...nodes);
}

function initWorkspace() {
  const storedSection = safeReadStorage(STORE_SECTION_KEY);
  ProjectWorkspace.module = PROJECT_SECTIONS.includes(storedSection)
    ? storedSection
    : PROJECT_SECTIONS[0];
  bindPrimaryNav();
  return (async () => {
    await loadProjects();
    await loadChats();
    // 没有 hash 时按"上次看过的项目"落地，实在没有就停在项目列表
    if (!location.hash) {
      const remembered = safeReadStorage(STORE_PROJECT_KEY) || "";
      const storedType = safeReadStorage(STORE_CONTEXT_KEY);
      if (storedType === CONTEXT_CHAT) {
        await enterChat(ProjectWorkspace.chatId, { navigate: false });
        return;
      }
      if (remembered && (WorkspaceState.projects || []).some((p) => p.project_id === remembered)) {
        await enterProject(remembered, ProjectWorkspace.module, { navigate: false });
        return;
      }
      await showProjects({ navigate: false });
      return;
    }
    await applyRoute();
  })().catch((error) => {
    reportClientError("workspace", error);
    showToast(error.message);
  });
}

//: 给后续步骤（项目能力边界 / 兼容验证）用的统一门面
window.OrchestratorContext = {
  CONTEXT_CHAT,
  CONTEXT_PROJECT,
  CONTEXT_TYPES,
  DEFAULT_CONTEXT_TYPE,
  PROJECT_SECTIONS,
  PROJECT_ONLY_ACTIONS,
  isProject() {
    return ProjectWorkspace.contextType === CONTEXT_PROJECT;
  },
  canRunAction(action) {
    return ProjectWorkspace.contextType === CONTEXT_PROJECT && PROJECT_ONLY_ACTIONS.has(action);
  },
  get contextType() {
    return ProjectWorkspace.contextType;
  },
  get projectId() {
    return ProjectWorkspace.projectId;
  },
  get module() {
    return ProjectWorkspace.module;
  },
  get chatId() {
    return ProjectWorkspace.chatId;
  },
  get projects() {
    return WorkspaceState.projects;
  },
  get chats() {
    return WorkspaceState.chats;
  },
  switchTo: switchWorkspace,
  selectSection: selectProjectSection,
  openProject,
  openChat: enterChat,
  createProject: createProjectFromForm,
  createChat: createChatSession,
  sendChatMessage,
  reload: async () => {
    await loadProjects();
    await loadChats();
    await loadModulePayload();
    render();
  },
  refresh: initWorkspace,
};

let workspaceInitialized = false;
function initWorkspaceOnce() {
  if (workspaceInitialized) return;
  workspaceInitialized = true;
  initWorkspace();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initWorkspaceOnce);
} else {
  initWorkspaceOnce();
}
