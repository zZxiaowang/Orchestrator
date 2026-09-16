/* 运行统计看板（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
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
    // 上下文构成：让用户看到"这一步的钱花在哪"（文件 / 树 / 历史 / 当前步…）
    const stats = item.context_stats || {};
    if (Object.keys(stats).length) {
      const order = [
        ["files", "文件"],
        ["tree", "树"],
        ["completed", "历史"],
        ["current", "当前步"],
        ["plan", "计划"],
        ["task", "任务"],
        ["brief", "简报"],
      ];
      const parts = order
        .filter(([key]) => stats[key] > 0)
        .map(([key, label]) => `${label} ${(stats[key] / 1000).toFixed(1)}k`);
      if (parts.length) cells.push(["上下文构成", parts.join(" / ")]);
    }
    const rounds = item.rounds || {};
    if (rounds.initial || rounds.fetch || rounds.repair) {
      cells.push([
        "调用轮次",
        `首轮 ${rounds.initial || 0} · 索取文件 ${rounds.fetch || 0} · 按报错修正 ${rounds.repair || 0}`,
      ]);
    }
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

