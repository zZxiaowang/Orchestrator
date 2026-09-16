/* Git 面板（由 web/app.js 拆出，顺序见 index.html 的 <script> 列表） */
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

