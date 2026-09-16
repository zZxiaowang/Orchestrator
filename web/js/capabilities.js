/* 能力中心（skill / MCP / 插件）（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
/* ── 能力中心（P0：统一入口 + 注册表视图；P1 补 skill 安装、P2 补 MCP 工具）── */

function closeCapabilities() {
  const modal = document.getElementById("capabilities-modal");
  if (modal) modal.hidden = true;
}

async function openCapabilities() {
  const modal = document.getElementById("capabilities-modal");
  if (!modal) return;
  modal.hidden = false;
  const token = ++state.tokens.capabilities;
  const body = document.getElementById("capabilities-body");
  body.replaceChildren(h("p", { class: "muted", text: "正在加载能力…" }));
  try {
    const payload = await api.capabilities();
    if (token !== state.tokens.capabilities) return;
    state.capabilities = payload.capabilities || [];
    renderCapabilities(payload);
  } catch (error) {
    if (token !== state.tokens.capabilities) return;
    body.replaceChildren(h("div", { class: "error-box", text: `能力列表加载失败：${error.message}` }));
  }
}

function renderCapabilities(payload) {
  const body = document.getElementById("capabilities-body");
  const items = payload.capabilities || [];
  const kinds = payload.kinds || [];
  const counts = payload.counts || {};
  const summary = document.getElementById("capabilities-summary");
  if (summary) {
    summary.textContent = kinds
      .map((info) => `${info.label} ${counts[info.id] || 0}`)
      .join(" · ");
  }

  const nodes = [
    h("p", {
      class: "hint",
      text: "skill / MCP / 插件共用一套安装、启用与审计。skill 是指令包（按需注入执行段上下文）；MCP 是工具服务器（模型可请求调用，应用层执行后回灌）。",
    }),
  ];

  const kindTabs = h("div", { class: "tabs tabs-inline", id: "capability-kinds" });
  for (const info of kinds) {
    const tab = h("button", {
      class: "tab",
      type: "button",
      dataset: { kind: info.id },
      title: info.hint || "",
      text: `${info.label}（${counts[info.id] || 0}）`,
    });
    kindTabs.append(tab);
  }
  nodes.push(kindTabs);

  const listHost = h("div", { class: "capability-list" });
  const renderList = (kind) => {
    for (const tab of kindTabs.querySelectorAll(".tab")) {
      tab.classList.toggle("active", kind ? tab.dataset.kind === kind : false);
    }
    const filtered = kind ? items.filter((item) => item.kind === kind) : items;
    // skill 页顶部给安装入口（本地目录 / GitHub / zip 地址）
    if (kind === "skill") {
      listHost.replaceChildren(skillInstallForm());
      if (filtered.length) listHost.append(...filtered.map(capabilityNode));
      return;
    }
    // MCP 页顶部给"从预设添加服务器"的入口
    if (kind === "mcp") {
      listHost.replaceChildren(mcpAddForm());
      if (filtered.length) listHost.append(...filtered.map(capabilityNode));
      return;
    }
    // 插件页：市场入口收进能力中心（侧栏不再单独占两个按钮）
    if (kind === "plugin") {
      // 注意别把按钮变量叫 openMarket：那会遮蔽同名函数，点一下直接 TypeError
      const marketBtn = h("button", {
        class: "btn primary small",
        type: "button",
        text: "打开插件市场（legacy）",
      });
      marketBtn.addEventListener(
        "click",
        safe(() => {
          closeCapabilities();
          openMarket("market");
        })
      );
      listHost.replaceChildren(
        h(
          "div",
          { class: "card" },
          h("div", { class: "card-head" }, h("strong", { text: "插件市场（历史形态）" })),
          h(
            "div",
            { class: "card-body" },
            h("p", {
              class: "hint",
              text: "插件是旧形态：只登记界面入口与能力声明，不下载、不执行第三方代码。新能力请优先用 skill 或 MCP；这里保留兼容。",
            }),
            h("div", { class: "approval-actions" }, marketBtn)
          )
        )
      );
      if (filtered.length) listHost.append(...filtered.map(capabilityNode));
      return;
    }
    if (!filtered.length) {
      const hint = kinds.find((info) => info.id === kind);
      listHost.replaceChildren(
        h("p", {
          class: "muted",
          text: kind
            ? `还没有 ${hint ? hint.label : kind} 能力：${hint ? hint.hint : ""}`
            : "还没有任何能力。可以到「插件市场」装插件（会以 plugin 形态出现在这里）。",
        })
      );
      return;
    }
    listHost.replaceChildren(...filtered.map(capabilityNode));
  };

  const capabilityNode = (capability) => {
    const actions = h("div", { class: "approval-actions" });
    const toggle = h("button", {
      class: "btn ghost small",
      type: "button",
      text: capability.enabled ? "停用" : "启用",
    });
    toggle.addEventListener(
      "click",
      safe(async () => {
        if (capability.enabled) await api.disableCapability(capability.id);
        else await api.enableCapability(capability.id);
        await openCapabilities();
      })
    );
    actions.append(toggle);

    if (capability.kind === "skill") {
      // 预览 SKILL.md 正文：装进来的是一份副本，这里看到的就是注入执行段的内容
      const peek = h("button", { class: "btn ghost small", type: "button", text: "查看 SKILL.md" });
      const preview = h("pre", { class: "stream", hidden: true });
      peek.addEventListener(
        "click",
        safe(async () => {
          if (preview.hidden) {
            const payload = await api.capabilityBody(capability.id);
            preview.textContent = payload.body || "（没有正文）";
            preview.hidden = false;
            peek.textContent = "收起 SKILL.md";
          } else {
            preview.hidden = true;
            peek.textContent = "查看 SKILL.md";
          }
        })
      );
      actions.append(peek);
      actions.append(preview);
      // 作用域：全局，或只在某个项目里生效
      const scopeSelect = h("select", { class: "capability-scope" });
      for (const [value, label] of [
        ["global", "全局启用"],
        ["project", "只在某个项目启用"],
      ]) {
        scopeSelect.append(h("option", { value, text: label, selected: capability.scope === value }));
      }
      scopeSelect.addEventListener(
        "change",
        safe(async () => {
          try {
            await api.setCapabilityScope(capability.id, {
              scope: scopeSelect.value,
              project_id: scopeSelect.value === "project" ? ProjectWorkspace.projectId || "" : "",
            });
            showToast(scopeSelect.value === "project" ? "已限定到当前项目" : "已改为全局启用");
          } catch (error) {
            showToast(error.message);
          }
          await openCapabilities();
        })
      );
      actions.append(scopeSelect);
    } else if (capability.kind === "mcp") {
      // MCP：先确认信任（会跑代码），再列工具 / 调用
      if (!capability.meta?.trusted) {
        const trust = h("button", {
          class: "btn primary small",
          type: "button",
          text: "确认信任",
        });
        trust.addEventListener(
          "click",
          safe(async () => {
            const ok = await appConfirm({
              title: "确认信任这个 MCP 服务器",
              message:
                "它会以本机权限运行（可读写文件、访问网络）。只有你信任它的来源时才确认；\n" +
                "确认后，模型在执行步骤时可以请求调用它的工具。",
              confirmText: "确认信任",
            });
            if (!ok) return;
            await api.trustCapability(capability.id);
            await openCapabilities();
          })
        );
        actions.append(trust);
      }
      const listTools = h("button", { class: "btn ghost small", type: "button", text: "列出工具" });
      listTools.addEventListener(
        "click",
        safe(async () => {
          listTools.disabled = true;
          try {
            const payload = await api.mcpTools(capability.id, true);
            if (payload.error) showToast(`拉取工具失败：${payload.error}`);
            else showToast(`已列出 ${(payload.tools || []).length} 个工具`);
            await openCapabilities();
          } catch (error) {
            showToast(error.message);
          } finally {
            listTools.disabled = false;
          }
        })
      );
      actions.append(listTools);
      actions.append(mcpCallPanel(capability));
    }

    if (capability.kind === "plugin") {
      actions.append(h("span", { class: "muted-small", text: "插件请到「插件市场」卸载" }));
    } else {
      const remove = h("button", { class: "btn ghost small", type: "button", text: "卸载" });
      remove.addEventListener(
        "click",
        safe(async () => {
          const ok = await appConfirm({
            title: "卸载能力",
            message: `确定卸载「${capability.name || capability.id}」？`,
            confirmText: "卸载",
            danger: true,
          });
          if (!ok) return;
          await api.uninstallCapability(capability.id);
          await openCapabilities();
        })
      );
      actions.append(remove);
    }

    return collapsibleCard(`capability:${capability.id}`, capability.name || capability.id, {
          subtitle: [
            capability.kind,
            capability.enabled ? "已启用" : "已停用",
            capability.scope === "project" ? `项目 ${capability.project_id || "（未指定）"}` : "",
            capability.version ? `v${capability.version}` : "",
            capability.permissions?.length ? capability.permissions.join("/") : "",
          ]
            .filter(Boolean)
            .join(" · "),
          children: [
            capability.description
              ? h("p", { text: capability.description })
              : h("p", { class: "muted", text: "（没有填写说明）" }),
            h("p", { class: "muted", text: `ID：${capability.id} · 范围：${capability.scope}` }),
            capability.source?.location
              ? h("p", { class: "muted", text: `来源：${capability.source.kind} · ${capability.source.location}` })
              : null,
            capability.meta?.scripts?.length
              ? h("p", {
                  class: "muted",
                  text: `自带脚本（默认不执行，要跑需开启命令白名单）：${capability.meta.scripts.join("、")}`,
                })
              : null,
            capability.kind === "mcp" ? h("p", { class: "muted", text: mcpSummary(capability) }) : null,
            capability.kind === "mcp" && (capability.meta?.tools || []).length
              ? h(
                  "ul",
                  { class: "list" },
                  ...(capability.meta.tools || [])
                    .slice(0, 20)
                    .map((tool) =>
                      h("li", { text: `${tool.name}：${tool.description || "（没有说明）"}` })
                    )
                )
              : null,
            actions,
          ],
        });
  };

  const mcpSummary = (capability) => {
    const meta = capability.meta || {};
    const bits = [
      meta.transport === "http" ? `HTTP ${meta.url || ""}` : `stdio ${meta.command || ""}`,
      meta.trusted ? "已确认信任" : "未确认信任（模型调用会被拒绝）",
      (meta.tools || []).length
        ? `工具 ${meta.tools.length} 个${meta.tools_fetched_at ? `（${meta.tools_fetched_at.slice(0, 19)} 拉取）` : ""}`
        : "还没拉取工具清单",
    ];
    return bits.join(" · ");
  };

  const mcpCallPanel = (capability) => {
    const tools = capability.meta?.tools || [];
    const button = h("button", { class: "btn ghost small", type: "button", text: "手动调用" });
    const select = h("select", { class: "capability-scope" });
    for (const tool of tools) select.append(h("option", { value: tool.name, text: tool.name }));
    const args = h("input", { placeholder: '参数 JSON，例如 {"text":"hi"}' });
    const result = h("pre", { class: "stream", hidden: true });
    const run = h("button", { class: "btn primary small", type: "button", text: "执行" });
    const box = h(
      "div",
      { class: "mcp-call-row", hidden: true },
      select,
      args,
      run,
      result
    );
    button.addEventListener(
      "click",
      safe(() => {
        box.hidden = !box.hidden;
        if (!tools.length) showToast("先点「列出工具」，拿到工具清单再调用。");
      })
    );
    run.addEventListener(
      "click",
      safe(async () => {
        run.disabled = true;
        try {
          let parsed = {};
          const raw = args.value.trim();
          if (raw) parsed = JSON.parse(raw);
          const payload = await api.mcpCall(capability.id, select.value, parsed);
          result.hidden = false;
          result.textContent = payload.result.ok
            ? payload.result.content || "（没有返回内容）"
            : `失败：${payload.result.error}`;
        } catch (error) {
          showToast(error.message);
        } finally {
          run.disabled = false;
        }
      })
    );
    return h("span", {}, button, box);
  };

  const mcpAddForm = () => {
    const presetSelect = h("select", { id: "mcp-preset" });
    presetSelect.append(h("option", { value: "", text: "自定义（自己填命令 / 地址）" }));
    const serverSelect = () => state.mcpPresets || [];
    const fill = () => {
      presetSelect.replaceChildren(h("option", { value: "", text: "自定义（自己填命令 / 地址）" }));
      for (const preset of serverSelect()) {
        presetSelect.append(h("option", { value: preset.id, text: preset.name }));
      }
    };
    fill();
    (async () => {
      try {
        const payload = await api.mcpPresets();
        state.mcpPresets = payload.presets || [];
        fill();
      } catch (error) {
        /* 预设拿不到也能自定义添加 */
      }
    })();
    const name = h("input", { id: "mcp-name", placeholder: "名字（留空用预设名）" });
    const transport = h("select", { id: "mcp-transport" });
    transport.append(
      h("option", { value: "stdio", text: "stdio（本地进程）" }),
      h("option", { value: "http", text: "http（远端 / 本地服务）" })
    );
    const command = h("input", { id: "mcp-command", placeholder: "command，例如 npx 或 python" });
    const argsInput = h("input", { id: "mcp-args", placeholder: "参数，空格分隔（可留空）" });
    const url = h("input", { id: "mcp-url", placeholder: "HTTP 地址，例如 https://host/mcp" });
    const add = h("button", { class: "btn primary small", type: "button", text: "添加服务器" });
    add.addEventListener(
      "click",
      safe(async () => {
        add.disabled = true;
        try {
          const isHttp = transport.value === "http";
          const payload = await api.addMcpServer({
            preset_id: presetSelect.value,
            name: name.value.trim(),
            transport: transport.value,
            command: isHttp ? "" : command.value.trim(),
            args: isHttp ? [] : String(argsInput.value || "").split(/\s+/).filter(Boolean),
            url: isHttp ? url.value.trim() : "",
          });
          showToast(`已添加「${payload.capability.name}」（默认未启用、未确认信任）`);
          await openCapabilities();
        } catch (error) {
          showToast(error.message);
        } finally {
          add.disabled = false;
        }
      })
    );
    return h(
      "div",
      { class: "card" },
      h("div", { class: "card-head" }, h("strong", { text: "添加 MCP 服务器" })),
      h(
        "div",
        { class: "card-body" },
        h("p", {
          class: "hint",
          text: "会跑代码的能力默认不启用、未确认信任：添加后先「列出工具」，确认信任后才允许（含模型自主）调用。",
        }),
        h("div", { class: "skill-install-row" }, presetSelect, name),
        h("div", { class: "skill-install-row" }, transport, command, argsInput, url, add)
      )
    );
  };

  const skillInstallForm = () => {
    const source = h("select", { id: "skill-source" });
    for (const [value, label] of [
      ["local", "本地目录"],
      ["github", "GitHub（owner/repo#ref/子目录）"],
      ["zip", "zip 地址"],
    ]) {
      source.append(h("option", { value, text: label }));
    }
    const location = h("input", {
      id: "skill-location",
      placeholder: "D:\\skills\\my-skill 或 owner/repo#main/skills/my-skill 或 https://…/skill.zip",
    });
    const install = h("button", { class: "btn primary small", type: "button", text: "安装技能" });
    install.addEventListener(
      "click",
      safe(async () => {
        const value = location.value.trim();
        if (!value) {
          showToast("先填技能来源。");
          return;
        }
        install.disabled = true;
        try {
          const payload = await api.installSkill({ source: source.value, location: value });
          showToast(`已安装技能「${payload.capability.name}」`);
          await openCapabilities();
        } catch (error) {
          showToast(error.message);
        } finally {
          install.disabled = false;
        }
      })
    );
    return h(
      "div",
      { class: "card" },
      h("div", { class: "card-head" }, h("strong", { text: "安装技能（SKILL.md）" })),
      h(
        "div",
        { class: "card-body" },
        h(
          "p",
          {
            class: "hint",
            text: "兼容市面常见格式：目录里有 SKILL.md（frontmatter 写 name / description）即可；带 scripts/ 的会被登记但默认不执行。",
          }
        ),
        h("div", { class: "skill-install-row" }, source, location, install)
      )
    );
  };
  kindTabs.addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) renderList(tab.dataset.kind);
  });
  nodes.push(listHost);

  // 审计：谁在什么时候装过 / 用过什么，面板里能看到最后几条
  (async () => {
    try {
      const audit = await api.capabilityAudit(10);
      const events = audit.events || [];
      if (!events.length) return;
      body.append(
        collapsibleCard("capability:audit", "最近的能力操作", {
          subtitle: `${events.length} 条`,
          children: [
            h(
              "ul",
              { class: "list" },
              ...events
                .slice()
                .reverse()
                .map((event) =>
                  h("li", {
                    text: `${event.at || ""} · ${event.event || ""} · ${event.id || ""}`,
                  })
                )
            ),
          ],
        })
      );
    } catch (error) {
      /* 审计读不到不影响主列表 */
    }
  })();

  body.replaceChildren(...nodes);
  renderList("");
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
    "Enter　发送任务（Shift+Enter 换行）",
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

