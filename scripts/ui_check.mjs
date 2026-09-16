/**
 * 界面交互自检（Windows / Node 18+，需要本机 Chrome 或 Edge）。
 *
 * 用 Chrome DevTools 协议做**真实点击**（Input.dispatchMouseEvent），而不是 JS 里调
 * element.click()，因此能发现"被遮罩挡住点不到"这类问题。
 *
 *   node scripts/ui_check.mjs                 # 默认检查 http://127.0.0.1:8787
 *   node scripts/ui_check.mjs --url http://127.0.0.1:8787 --shot out.png
 *
 * 退出码：0 = 全部通过，1 = 有失败项。
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const BASE_URL = argOf("url", "http://127.0.0.1:8787");
const PORT = Number(argOf("port", "9222"));
const ALLOW_REMOTE = args.includes("--allow-remote");
const [VIEW_W, VIEW_H] = argOf("viewport", "1440x720")
  .split("x")
  .map((value) => Number(value) || 0);
const here = dirname(fileURLToPath(import.meta.url));
const SHOT = resolve(argOf("shot", resolve(here, "..", ".logs", "ui-light.png")));

const CHROME_CANDIDATES = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
];

function findBrowser() {
  for (const candidate of CHROME_CANDIDATES) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error("未找到 Chrome/Edge，可传入 --browser <path>");
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function waitForDevtools() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const targets = await response.json();
      const page = targets.find((item) => item.type === "page" && item.webSocketDebuggerUrl);
      if (page) return page;
    } catch {
      /* 还没起来 */
    }
    await sleep(250);
  }
  throw new Error("DevTools 端口未就绪");
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      const resolver = this.pending.get(message.id);
      if (!resolver) return;
      this.pending.delete(message.id);
      if (message.error) resolver.reject(new Error(JSON.stringify(message.error)));
      else resolver.resolve(message.result);
    });
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (result.exceptionDetails) {
      throw new Error(`页面脚本异常：${result.exceptionDetails.text}`);
    }
    return result.result.value;
  }

  /** 在元素中心发出真实鼠标点击，返回是否命中该元素（未被遮挡）。 */
  async clickSelector(selector, index = 0) {
    // 先滚动到可见区域（弹窗/面板内部滚动也算），贴近真人"先滚再点"
    await this.evaluate(`(() => {
      const el = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
      if (el) el.scrollIntoView({ block: "center", inline: "nearest" });
    })()`);
    await sleep(150);
    const box = await this.evaluate(`(() => {
      const el = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
      if (!el) return null;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return null;
      const x = r.left + r.width / 2;
      const y = r.top + r.height / 2;
      const top = document.elementFromPoint(x, y);
      return {
        x,
        y,
        hit: top === el || el.contains(top),
        // 没点中时把"实际点到谁"带回来：被遮罩/浮层挡住是最常见的失败原因
        topTag: top ? top.tagName : "",
        topId: top ? top.id : "",
        topClass: top ? String(top.className || "").slice(0, 40) : "",
        topPath: top
          ? (() => {
              const parts = [];
              let node = top;
              for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
                parts.push(
                  node.tagName + (node.id ? "#" + node.id : "") + "." +
                    String(node.className || "").split(" ")[0],
                );
              }
              return parts.join(" < ");
            })()
          : "",
        topText: top ? String(top.textContent || "").slice(0, 24) : "",
      };
    })()`);
    if (!box) return { clicked: false, reason: "元素不存在或不可见" };
    for (const type of ["mousePressed", "mouseReleased"]) {
      await this.send("Input.dispatchMouseEvent", {
        type,
        x: box.x,
        y: box.y,
        button: "left",
        clickCount: 1,
      });
    }
    await sleep(250);
    // top：没点中时"实际点到谁"，用于定位遮挡（遮罩、浮层、被别的元素盖住）
    return {
      clicked: true,
      hit: box.hit,
      top: `${box.topTag || ""}#${box.topId || ""}.${box.topClass || ""}`,
      topPath: box.topPath || "",
      topText: box.topText || "",
    };
  }

  /** 发送真实组合键（modifiers: 1=Alt 2=Ctrl 4=Meta 8=Shift）。 */
  async pressKey({ key, code, virtualKeyCode, modifiers = 0 }) {
    for (const type of ["keyDown", "keyUp"]) {
      await this.send("Input.dispatchKeyEvent", {
        type,
        key,
        code,
        windowsVirtualKeyCode: virtualKeyCode,
        nativeVirtualKeyCode: virtualKeyCode,
        modifiers,
      });
    }
    await sleep(150);
  }

  /** 在容器上发一次真实滚轮事件，返回滚动前后的 scrollTop。 */
  async wheelScroll(selector, delta = 300) {
    const info = await this.evaluate(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {
        x: r.left + r.width / 2,
        y: r.top + Math.min(120, Math.max(r.height / 2, 20)),
        clientH: el.clientHeight,
        scrollH: el.scrollHeight,
        before: el.scrollTop,
      };
    })()`);
    if (!info) return { found: false };
    const scrollable = info.scrollH > info.clientH + 4;
    if (scrollable) {
      await this.send("Input.dispatchMouseEvent", {
        type: "mouseWheel",
        x: info.x,
        y: info.y,
        deltaX: 0,
        deltaY: delta,
      });
      await sleep(400);
    }
    const after = await this.evaluate(
      `document.querySelector(${JSON.stringify(selector)}).scrollTop`,
    );
    return { found: true, scrollable, before: info.before, after, ...info };
  }
}

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok: Boolean(ok), detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
}

async function main() {
  // 安全闸：自检会写配置/装插件/提交任务，禁止打到接了真实中转的实例，
  // 否则会污染真实配置并消耗额度。确需如此请显式加 --allow-remote。
  if (!ALLOW_REMOTE) {
    try {
      const response = await fetch(`${BASE_URL}/api/v1/settings`);
      const payload = await response.json();
      const host = (payload.architect?.base_url || "").replace(/^https?:\/\//, "").split("/")[0];
      const isLocal = host.startsWith("127.0.0.1") || host.startsWith("localhost");
      if (!isLocal) {
        console.error(
          `拒绝运行：${BASE_URL} 指向的不是本地中转（当前 host=${host || "未配置"}）。\n` +
            "自检会写配置、装插件、提交任务，只应跑在演示实例上：\n" +
            "  1) python -m scripts.fake_relay\n" +
            "  2) $env:RELAY_BASE_URL='http://127.0.0.1:8799/v1'; $env:RELAY_API_KEY='sk-demo'; " +
            "$env:ORCHESTRATOR_IGNORE_SAVED_SETTINGS='1'; python -m uvicorn app.main:app --port 8788\n" +
            "  3) node scripts/ui_check.mjs --url http://127.0.0.1:8788",
        );
        return 2;
      }
    } catch (error) {
      console.error(`无法读取 ${BASE_URL} 的配置：${error.message}`);
      return 2;
    }
  }

  const browserPath = argOf("browser", findBrowser());
  const profile = resolve(process.env.TEMP || ".", `ui-check-${Date.now()}`);
  const child = spawn(
    browserPath,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${profile}`,
      "--window-size=1680,960",
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  try {
    const target = await waitForDevtools();
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((open) => socket.addEventListener("open", open, { once: true }));
    const cdp = new Cdp(socket);

    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: VIEW_W,
      height: VIEW_H,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await cdp.send("Page.navigate", { url: BASE_URL });
    await sleep(2500);

    // 1) 遮罩是否挡住了主要交互元素
    const overlay = await cdp.evaluate(`(() => {
      const ids = ["send-btn", "settings-btn", "new-run-btn", "task-input"];
      const vw = window.innerWidth, vh = window.innerHeight;
      return ids.map((id) => {
        const el = document.getElementById(id);
        if (!el) return { id, found: false };
        const r = el.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        const top = document.elementFromPoint(x, y);
        return {
          id, found: true,
          hit: top === el || el.contains(top),
          topClass: top ? top.className : null,
          inViewport: x >= 0 && y >= 0 && x < vw && y < vh,
          rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
          viewport: [vw, vh],
        };
      });
    })()`);
    for (const item of overlay) {
      const detail = item.hit
        ? ""
        : item.inViewport
          ? `被 ${item.topClass ?? "未知元素"} 遮挡`
          : `不在视口内（rect=${item.rect} viewport=${item.viewport}）`;
      check(`元素可点击：${item.id}`, item.found && item.hit, detail);
    }

    // 1a) 工作区导航：一级入口只有两个，项目内二级模块七个（且都能点）
    await cdp.evaluate(`location.hash = "#/projects"`);
    await sleep(800);
    const shell = await cdp.evaluate(`(() => ({
      primary: Array.from(document.querySelectorAll("#primary-nav .nav-entry")).map(
        (el) => el.querySelector("strong")?.textContent.trim() || "",
      ),
      modules: Array.from(document.querySelectorAll("#project-nav .project-nav-btn")).map(
        (el) => el.textContent.trim(),
      ),
      projects: document.querySelectorAll("#project-options .project-item").length,
      chatPaneHidden: document.getElementById("chat-pane").hidden,
      projectPaneShown: !document.getElementById("project-pane").hidden,
    }))()`);
    check(
      "一级入口只有「普通对话 / 项目」，项目内七个二级模块",
      JSON.stringify(shell.primary) === JSON.stringify(["普通对话", "项目"]) &&
        JSON.stringify(shell.modules) ===
          JSON.stringify(["概览", "架构", "计划", "执行", "验证", "日志", "设置"]) &&
        shell.projects >= 1 &&
        shell.chatPaneHidden &&
        shell.projectPaneShown,
      JSON.stringify(shell),
    );

    // 选中一个项目并进入「执行」模块：后面所有运行相关检查都在这个上下文里
    const picked = await cdp.evaluate(`(() => {
      document.getElementById("project-picker-btn").click();
      const first = document.querySelector("#project-options .project-item");
      if (first) first.click();
      return first ? first.textContent.slice(0, 40) : "";
    })()`);
    await sleep(900);
    await cdp.clickSelector('#project-nav [data-nav-section="execution"]');
    await sleep(900);
    const projectRoute = await cdp.evaluate(`location.hash`);
    check(
      "选项目后进入其「执行」模块（路由带 projectId）",
      Boolean(picked) && /#\/projects\/[^/]+\/execution$/.test(projectRoute),
      `项目=${picked} hash=${projectRoute}`,
    );

    // 1b) 项目模块不只是导航：架构 / 计划 / 验证 / 日志 / 设置各自渲染真实内容
    const probeModule = async (module, marker) => {
      await cdp.clickSelector(`#project-nav [data-nav-section="${module}"]`);
      await sleep(700);
      return cdp.evaluate(`(() => {
        const host = document.getElementById("timeline");
        return {
          hash: location.hash,
          marker: (host.textContent || "").includes(${JSON.stringify(marker)}),
          cards: host.querySelectorAll(".card").length,
        };
      })()`);
    };
    for (const [module, marker] of [
      ["architecture", "纲领目标"],
      ["plan", "交付物"],
      ["verification", "验证汇总"],
      ["logs", "事件与消息"],
      ["settings", "项目设置"],
    ]) {
      const state = await probeModule(module, marker);
      check(
        `项目「${module}」模块渲染真实内容`,
        state.hash.includes(`/${module}`) && state.marker && state.cards > 1,
        JSON.stringify(state),
      );
    }
    // 回到执行模块（后面的运行列表 / 时间线检查依赖它）
    await cdp.clickSelector('#project-nav [data-nav-section="execution"]');
    await sleep(700);

    // 1c) 普通对话：真实会话（新建 → 发消息 → 拿到回答 → 进列表），且不渲染项目控件
    await cdp.evaluate(`location.hash = "#/chat"`);
    await sleep(800);
    const chatShell = await cdp.evaluate(`(() => ({
      projectPaneHidden: document.getElementById("project-pane").hidden,
      metaHidden: document.getElementById("composer-meta").hidden,
      runActions: document.getElementById("run-actions").children.length,
    }))()`);
    check(
      "普通对话不渲染项目控件（没有落地目录 / 运行操作）",
      chatShell.projectPaneHidden && chatShell.metaHidden && chatShell.runActions === 0,
      JSON.stringify(chatShell),
    );

    await cdp.clickSelector("#new-chat-btn");
    await sleep(700);
    await cdp.evaluate(`document.getElementById("task-input").focus()`);
    await cdp.send("Input.insertText", { text: "你是哪个模型" });
    await cdp.pressKey({ key: "Enter", code: "Enter", virtualKeyCode: 13 });
    let chatAnswer = null;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      chatAnswer = await cdp.evaluate(`(() => ({
        hash: location.hash,
        assistant: document.querySelectorAll("#timeline .card.msg-assistant").length,
        question: document.querySelectorAll("#timeline .card.msg-user").length,
      }))()`);
      if (chatAnswer.assistant >= 1) break;
      await sleep(300);
    }
    check(
      "普通对话能发消息并拿到回答（会话落到后端）",
      chatAnswer.hash.startsWith("#/chat/") &&
        chatAnswer.assistant >= 1 &&
        chatAnswer.question >= 1,
      JSON.stringify(chatAnswer),
    );
    const chatListed = await cdp.evaluate(
      `document.querySelectorAll("#chat-list .chat-item").length`,
    );
    check("新对话出现在对话列表里", chatListed >= 1, `对话数=${chatListed}`);

    // 回到项目的执行模块
    await cdp.evaluate(`location.hash = ${JSON.stringify(projectRoute)}`);
    await sleep(900);

    const modalHidden = await cdp.evaluate(
      `(() => { const m = document.getElementById("settings-modal");
        return { hidden: m.hidden, display: getComputedStyle(m).display }; })()`,
    );
    check("设置弹窗默认隐藏", modalHidden.hidden && modalHidden.display === "none", JSON.stringify(modalHidden));

    // 2) 打开 / 关闭设置弹窗
    const open = await cdp.clickSelector("#settings-btn");
    const modalVisible = await cdp.evaluate(
      `(() => { const m = document.getElementById("settings-modal");
        return { hidden: m.hidden, display: getComputedStyle(m).display }; })()`,
    );
    check("点击⚙能打开设置", open.hit && !modalVisible.hidden && modalVisible.display !== "none", JSON.stringify(modalVisible));

    const close = await cdp.clickSelector("#settings-cancel");
    const modalClosed = await cdp.evaluate(`document.getElementById("settings-modal").hidden`);
    check("点击取消能关闭设置", close.hit && modalClosed === true);

    // 2b) 一键测试连接（会先保存当前表单内容；演示模式下只在本进程生效）
    await cdp.clickSelector("#settings-btn");
    const ping = await cdp.clickSelector("#settings-test");
    await sleep(1500);
    const pingResult = await cdp.evaluate(
      `document.getElementById("settings-test-result").textContent || ""`,
    );
    check("设置里可一键测试连接", ping.hit && pingResult.includes("✓"), pingResult.slice(0, 120));

    // 2c) 测试连接后 Key 输入框必须保留内容（曾出现"Key 自动消失"）
    const isLocal = await cdp.evaluate(
      `(() => { const url = state.settings?.relay_base_url || "";
         return url.includes("127.0.0.1") || url.includes("localhost"); })()`,
    );
    if (isLocal) {
      await cdp.evaluate(
        `document.getElementById("f-relay-key").value = "sk-uitest-123456"`,
      );
      await cdp.clickSelector("#settings-test");
      await sleep(1500);
      const afterPing = await cdp.evaluate(`(() => ({
        value: document.getElementById("f-relay-key").value,
        placeholder: document.getElementById("f-relay-key").placeholder,
        status: document.getElementById("settings-status").textContent,
      }))()`);
      check(
        "测试连接后 Key 输入框内容保留且显示掩码",
        afterPing.value === "sk-uitest-123456" && afterPing.placeholder.includes("已保存"),
        JSON.stringify(afterPing).slice(0, 160),
      );
    } else {
      console.log("SKIP  测试连接后 Key 保留（当前不是本地/演示端点，避免改动真实配置）");
    }

    // 2d) 多套配置：新建一套 → 填好 → 设为当前 → 侧栏出现切换器
    const optionsBefore = await cdp.evaluate(
      `document.querySelectorAll("#f-provider-select option").length`,
    );
    const made = await cdp.clickSelector("#provider-new");
    await sleep(900);
    const optionsAfter = await cdp.evaluate(
      `document.querySelectorAll("#f-provider-select option").length`,
    );
    check(
      "设置里可新建一套配置",
      made.hit && optionsAfter === optionsBefore + 1,
      `${optionsBefore} → ${optionsAfter} 套`,
    );

    await cdp.evaluate(`(() => {
      const set = (id, value) => { document.getElementById(id).value = value; };
      set("f-relay-base", "http://127.0.0.1:8799/v1");
      set("f-relay-key", "sk-provider-test-1234");
      set("f-architect-model", "gpt-5");
      set("f-editor-model", "deepseek-v4");
    })()`);
    const activated = await cdp.clickSelector("#provider-activate");
    await sleep(1200);
    const quick = await cdp.evaluate(`(() => {
      const select = document.getElementById("provider-quick");
      return {
        disabled: select.disabled,
        text: select.options[select.selectedIndex]?.textContent || "",
        options: select.options.length,
      };
    })()`);
    check(
      "侧栏可一键切换配置（当前项带 ● 标记）",
      activated.hit && !quick.disabled && quick.text.includes("●") && quick.options >= 2,
      JSON.stringify(quick),
    );

    // 2e) 分段模式：架构段与执行段各用不同配置
    const split = await cdp.evaluate(`(async () => {
      const toggle = document.getElementById("f-split-mode");
      toggle.checked = true;
      toggle.dispatchEvent(new Event("change", { bubbles: true }));
      const ids = [...document.querySelectorAll("#f-architect-provider option")]
        .map((option) => option.value)
        .filter(Boolean);
      document.getElementById("f-architect-provider").value = ids[0];
      document.getElementById("f-editor-provider").value = ids[ids.length - 1];
      document.getElementById("settings-save").click();
      await new Promise((done) => setTimeout(done, 1800));
      return {
        routes: state.settings?.routes || {},
        sidebar: document.getElementById("sidebar-route").textContent,
      };
    })()`);
    check(
      "分段模式：架构段/执行段可各用一套配置",
      Boolean(split.routes.architect?.provider_id) &&
        Boolean(split.routes.editor?.provider_id) &&
        split.sidebar.includes("分段"),
      JSON.stringify(split).slice(0, 200),
    );
    await cdp.clickSelector("#settings-cancel");

    // 3b) 左侧任务栏底部槽位（对应 dsh-desktop 的 sidebar.footer.action / sidebar.settings 契约）
    const slots = await cdp.evaluate(`(() => {
      const hit = (selector) => {
        const el = document.querySelector(selector);
        if (!el) return null;
        // 侧栏现在可滚动：先把目标滚进视口，再测"点得到点不到"
        el.scrollIntoView({ block: "center", inline: "nearest" });
        const r = el.getBoundingClientRect();
        const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return {
          present: true,
          hit: top === el || el.contains(top),
          topPath: top
            ? top.tagName + (top.id ? "#" + top.id : "") + "." + String(top.className || "").split(" ")[0]
            : "",
          topText: top ? String(top.textContent || "").slice(0, 20) : "",
        };
      };
      return {
        market: hit('[data-entry="market"]'),
        plugins: hit('[data-entry="plugins"]'),
        updates: hit('[data-entry="updates"]'),
        settings: hit('[data-seat="settings"]'),
        wide: document.getElementById("sidebar").dataset.wide,
      };
    })()`);
    for (const key of ["market", "plugins", "updates", "settings"]) {
      const info = slots[key];
      check(
        `侧栏槽位可点击：${key}`,
        Boolean(info && info.present && info.hit),
        JSON.stringify(info),
      );
    }
    check("左侧任务栏默认展开（data-wide=true）", slots.wide === "true", String(slots.wide));

    // 3c) 折叠成图标栏（rail）
    await cdp.clickSelector("#sidebar-toggle");
    await sleep(400);
    const rail = await cdp.evaluate(`(() => {
      const sidebar = document.getElementById("sidebar");
      const label = document.querySelector(".foot-label");
      const el = document.querySelector('[data-entry="market"]');
      const r = el ? el.getBoundingClientRect() : null;
      const top = r ? document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) : null;
      return {
        wide: sidebar.dataset.wide,
        appCollapsed: document.querySelector(".app").classList.contains("collapsed"),
        labelHidden: label ? getComputedStyle(label).display === "none" : null,
        marketHit: Boolean(el && (top === el || el.contains(top))),
        width: Math.round(sidebar.getBoundingClientRect().width),
      };
    })()`);
    check(
      "点击折叠按钮进入图标栏",
      rail.wide === "false" && rail.appCollapsed === true && rail.labelHidden === true,
      JSON.stringify(rail),
    );
    check("折叠后槽位依然可点击", rail.marketHit === true, JSON.stringify(rail));
    await cdp.clickSelector("#sidebar-toggle");
    await sleep(300);
    const restored = await cdp.evaluate(`document.getElementById("sidebar").dataset.wide`);
    check("再次点击恢复展开", restored === "true", String(restored));

    // 3d) 插件市场：打开 → 列出内置插件 → 安装 → 侧栏出现入口 → 禁用后消失 → 卸载
    const marketOpen = await cdp.clickSelector("#market-btn");
    await sleep(1500);
    const marketState = await cdp.evaluate(`(() => ({
      visible: !document.getElementById("market-modal").hidden,
      cards: document.querySelectorAll(".market-card").length,
      sources: document.querySelectorAll(".source-row").length,
    }))()`);
    check(
      "打开插件市场并列出内置插件",
      marketOpen.hit && marketState.visible && marketState.cards >= 3,
      JSON.stringify(marketState),
    );

    const installResult = await cdp.evaluate(`(async () => {
      const button = document.querySelector('.market-card[data-item="night-batch"] .btn.primary');
      if (!button) return { ok: false, reason: "找不到安装按钮" };
      button.click();
      await new Promise((done) => setTimeout(done, 2000));
      return {
        ok: true,
        hasEntry: Boolean(document.querySelector('[data-entry="night-batch"]')),
        footerEntries: document.querySelectorAll("#plugin-footer-actions [data-entry]").length,
        plugins: (state.plugins.plugins || []).map((plugin) => plugin.id),
      };
    })()`);
    check(
      "安装插件后侧栏底部出现它的入口",
      installResult.ok && installResult.hasEntry && installResult.footerEntries >= 1,
      JSON.stringify(installResult),
    );

    const disableResult = await cdp.evaluate(`(async () => {
      document.querySelector('#market-tabs .tab[data-mtab="installed"]').click();
      await new Promise((done) => setTimeout(done, 400));
      const card = document.querySelector('.market-card[data-plugin="night-batch"]');
      const button = card ? card.querySelector(".btn.ghost") : null;
      if (!button) return { ok: false, reason: "找不到禁用按钮" };
      button.click();
      await new Promise((done) => setTimeout(done, 1500));
      return {
        ok: true,
        footerEntries: document.querySelectorAll("#plugin-footer-actions [data-entry]").length,
      };
    })()`);
    check(
      "禁用插件后侧栏入口消失",
      disableResult.ok && disableResult.footerEntries === 0,
      JSON.stringify(disableResult),
    );

    const uninstallResult = await cdp.evaluate(`(async () => {
      const card = document.querySelector('.market-card[data-plugin="night-batch"]');
      const buttons = card ? [...card.querySelectorAll(".btn.ghost")] : [];
      const button = buttons.find((item) => item.textContent.includes("卸载"));
      if (!button) return { ok: false, reason: "找不到卸载按钮" };
      button.click();
      await new Promise((done) => setTimeout(done, 400));
      // 现在用的是页内确认框（不依赖 WebView 的原生 confirm），需要点"卸载"
      const confirm = document.getElementById("confirm-ok");
      if (!confirm) return { ok: false, reason: "没有出现确认框" };
      confirm.click();
      await new Promise((done) => setTimeout(done, 1500));
      return { ok: true, plugins: (state.plugins.plugins || []).map((plugin) => plugin.id) };
    })()`);
    check(
      "卸载插件后列表清空",
      uninstallResult.ok && uninstallResult.plugins.length === 0,
      JSON.stringify(uninstallResult),
    );
    await cdp.clickSelector("#market-close");

    // 3) 点击运行列表 → 打开运行
    // 先确保没有弹窗遮罩（弹窗未关时，后面的点击会被遮罩吃掉 → 误判"点了没反应"）
    await cdp.evaluate(`(() => {
      for (const id of ["settings-modal", "market-modal", "palette-modal", "confirm-modal"]) {
        const el = document.getElementById(id);
        if (el && !el.hidden) el.hidden = true;
      }
    })()`);
    await sleep(200);
    // 优先挑一条真的跑出过步骤的记录（列表里可能残留失败的历史运行）
    const targetIndex = await cdp.evaluate(`(() => {
      const items = [...document.querySelectorAll(".run-item")];
      const index = items.findIndex((item) => {
        const meta = item.querySelector(".run-meta")?.textContent || "";
        // 形如「已完成3/3 步」，前面还有状态文字，所以不做行首锚定
        const match = meta.match(/(\\d+)\\/(\\d+)\\s*步/);
        return match && Number(match[1]) > 0;
      });
      return index;
    })()`);
    if (targetIndex >= 0) {
      // 点标题（.run-name）而不是条目的几何中心：条目居中位置可能落在
      // ☆/✎/▣ 这些操作按钮上，它们 stopPropagation，点了不会打开运行详情。
      // 列表在运行中可能被刷新而重排，所以点一次没命中就重新定位再点一次。
      let clickRun = await cdp.clickSelector(".run-item .run-name", targetIndex);
      if (!clickRun.hit) {
        await sleep(400);
        clickRun = await cdp.clickSelector(".run-item .run-name", targetIndex);
      }
      // openRun 是异步的（拉运行详情 + 建 SSE），演示目录攒多了会明显变慢：
      // 轮询等详情真的打开，别用固定 sleep 去赌（曾经因此误判成"点了没反应"）。
      let afterRun = null;
      for (let attempt = 0; attempt < 40; attempt += 1) {
        afterRun = await cdp.evaluate(
          `(() => ({ title: document.getElementById("run-title").textContent,
                     steps: document.querySelectorAll(".step").length,
                     status: document.getElementById("status-pill").textContent }))()`,
        );
        if (afterRun.steps > 0 && !afterRun.status.includes("待开始")) break;
        await sleep(250);
      }
      check(
        "点击运行记录能打开详情",
        clickRun.hit && afterRun.steps > 0 && !afterRun.status.includes("待开始"),
        JSON.stringify({ ...afterRun, click: clickRun }),
      );
    } else {
      check("存在已完成步骤的运行记录", false, "列表为空，请先在演示模式下跑一次");
    }

    // 3a1) 设置弹窗：分区导航（不再是一条长滚动）
    await cdp.clickSelector("#settings-btn");
    await sleep(400);
    const sections = await cdp.evaluate(`(() => {
      const items = Array.from(document.querySelectorAll("#settings-nav .settings-nav-item"));
      const visible = Array.from(document.querySelectorAll(".settings-section"))
        .filter((el) => !el.hidden)
        .map((el) => el.dataset.section);
      return {
        nav: items.map((el) => el.textContent.trim()),
        active: document.querySelector("#settings-nav .settings-nav-item.active")?.dataset.section,
        visible,
      };
    })()`);
    check(
      "设置弹窗有分区导航，默认只显示「模型与路由」",
      sections.nav.length === 4 &&
        sections.active === "model" &&
        JSON.stringify(sections.visible) === JSON.stringify(["model"]),
      JSON.stringify(sections),
    );
    const switchToCommand = await cdp.clickSelector('#settings-nav [data-section="command"]');
    await sleep(300);
    const commandSection = await cdp.evaluate(`(() => {
      const visible = Array.from(document.querySelectorAll(".settings-section"))
        .filter((el) => !el.hidden)
        .map((el) => el.dataset.section);
      const box = document.getElementById("f-command-allowlist");
      const modelField = document.getElementById("f-provider-name");
      return {
        visible,
        allowlistShown: Boolean(box) && box.getBoundingClientRect().height > 0,
        modelShown: Boolean(modelField) && modelField.getBoundingClientRect().height > 0,
      };
    })()`);
    check(
      "切到「命令与安全」只显示该区字段",
      switchToCommand.hit &&
        JSON.stringify(commandSection.visible) === JSON.stringify(["command"]) &&
        commandSection.allowlistShown &&
        !commandSection.modelShown,
      JSON.stringify(commandSection),
    );
    await cdp.clickSelector("#settings-cancel");
    await sleep(250);

    // 3a2) 右侧明细面板默认收起（主区占满），按需滑出
    const panelClosed = await cdp.evaluate(`(() => {
      const panel = document.querySelector(".inspector").getBoundingClientRect();
      const main = document.querySelector(".main").getBoundingClientRect();
      return {
        state: document.body.dataset.panel,
        panelWidth: Math.round(panel.width),
        mainWidth: Math.round(main.width),
      };
    })()`);
    check(
      "明细面板默认收起、主区占满",
      panelClosed.state === "closed" && panelClosed.panelWidth < 5 && panelClosed.mainWidth > 800,
      JSON.stringify(panelClosed),
    );

    const stepLinkClicked = await cdp.clickSelector(".step-changes");
    await sleep(400);
    const panelOpened = await cdp.evaluate(`(() => {
      const panel = document.querySelector(".inspector").getBoundingClientRect();
      const main = document.querySelector(".main").getBoundingClientRect();
      return {
        state: document.body.dataset.panel,
        panelWidth: Math.round(panel.width),
        mainWidth: Math.round(main.width),
        tab: document.querySelector(".tab.active")?.dataset.tab || null,
      };
    })()`);
    check(
      "点「看变更」按需滑出明细面板",
      stepLinkClicked.hit &&
        panelOpened.state === "open" &&
        panelOpened.panelWidth > 300 &&
        panelOpened.tab === "changes",
      JSON.stringify(panelOpened),
    );

    // Esc 收起面板（没有弹窗时）
    await cdp.pressKey({ key: "Escape", code: "Escape", virtualKeyCode: 27 });
    await sleep(300);
    const afterEsc = await cdp.evaluate(`document.body.dataset.panel`);
    check("Esc 收起明细面板", afterEsc === "closed", `panel=${afterEsc}`);

    // 后续的标签页 / 滚轮检查需要面板是打开的
    await cdp.clickSelector("#inspector-toggle");
    await sleep(350);

    await cdp.clickSelector("#settings-btn");
    await sleep(400);
    // 白名单在「命令与安全」分区里
    await cdp.clickSelector('#settings-nav [data-section="command"]');
    await sleep(250);
    const allowlistFilled = await cdp.evaluate(`(() => {
      const el = document.getElementById("f-command-allowlist");
      if (!el) return null;
      el.value = "python -m pytest";
      return el.value;
    })()`);
    await cdp.clickSelector("#settings-save");
    await sleep(700);
    const savedAllowlist = await cdp.evaluate(`(async () => {
      const response = await fetch("/api/v1/settings");
      const payload = await response.json();
      return (payload.command_allowlist || []).join("|");
    })()`);
    check(
      "设置里可配置命令白名单并保存",
      allowlistFilled === "python -m pytest" && savedAllowlist.includes("python -m pytest"),
      JSON.stringify({ filled: allowlistFilled, saved: savedAllowlist }),
    );
    await cdp.clickSelector("#settings-cancel");
    await sleep(250);

    // 3a3) 自开发预设：一键把白名单配成本项目质量门
    await cdp.clickSelector("#settings-btn");
    await sleep(300);
    await cdp.clickSelector('#settings-nav [data-section="command"]');
    await sleep(250);
    const presetClicked = await cdp.clickSelector("#dev-preset-btn");
    await sleep(700);
    const presetState = await cdp.evaluate(`(() => {
      const box = document.getElementById("f-command-allowlist");
      return {
        lines: (box?.value || "").split("\\n").filter(Boolean).length,
        enabled: document.getElementById("f-allow-cmd")?.checked === true,
      };
    })()`);
    check(
      "一键配置自开发（质量门白名单）",
      presetClicked.hit && presetState.lines >= 3 && presetState.enabled,
      JSON.stringify(presetState),
    );
    // 还原：自检自己配上的白名单会污染演示实例（后面的运行会真的去跑 pytest 并失败）
    await cdp.evaluate(`(() => {
      document.getElementById("f-command-allowlist").value = "";
      document.getElementById("f-allow-cmd").checked = false;
      return true;
    })()`);
    await cdp.clickSelector("#settings-save");
    await sleep(600);
    await cdp.clickSelector("#settings-cancel");
    await sleep(250);

    const revertLabels = await cdp.evaluate(
      `Array.from(document.querySelectorAll(".step-actions button")).map((el) => el.textContent.trim())`,
    );
    check(
      "步骤卡片提供一键回滚",
      Array.isArray(revertLabels) && revertLabels.some((text) => text.includes("回滚")),
      JSON.stringify(revertLabels),
    );

    // 3f) 普通对话的长上下文自动拆分：开关 + 可选项都在「行为与上下文」里，且能保存
    await cdp.clickSelector("#settings-btn");
    await sleep(400);
    await cdp.clickSelector('#settings-nav [data-section="behavior"]');
    await sleep(300);
    const chatContextUi = await cdp.evaluate(`(() => {
      const master = document.getElementById("f-chat-context");
      const options = document.getElementById("chat-context-options");
      const inputs = options ? Array.from(options.querySelectorAll("input")) : [];
      const before = inputs.map((el) => el.disabled);
      master.checked = false;
      master.dispatchEvent(new Event("change", { bubbles: true }));
      const disabledWhenOff = inputs.every((el) => el.disabled);
      master.checked = true;
      master.dispatchEvent(new Event("change", { bubbles: true }));
      const enabledWhenOn = inputs.every((el) => !el.disabled);
      document.getElementById("f-chat-window-turns").value = "8";
      return {
        hasGroup: Boolean(master && options),
        fields: inputs.map((el) => el.id),
        before,
        disabledWhenOff,
        enabledWhenOn,
        status: (document.getElementById("chat-context-status").textContent || "").slice(0, 60),
      };
    })()`);
    await cdp.clickSelector("#settings-save");
    await sleep(600);
    const savedChatContext = await cdp.evaluate(`fetch("/api/v1/settings").then((r) => r.json())`);
    check(
      "设置里有「长上下文自动拆分」开关与可选项（关掉时选项禁用）",
      chatContextUi.hasGroup &&
        chatContextUi.fields.length >= 4 &&
        chatContextUi.disabledWhenOff &&
        chatContextUi.enabledWhenOn &&
        savedChatContext.chat_context_enabled === true &&
        savedChatContext.chat_window_turns === 8 &&
        typeof savedChatContext.chat_summary_max_chars === "number",
      JSON.stringify({ ...chatContextUi, saved: savedChatContext.chat_window_turns }),
    );
    // 还原默认值，别把演示实例的配置改坏
    await cdp.evaluate(`(() => {
      document.getElementById("f-chat-window-turns").value = "12";
      return true;
    })()`);
    await cdp.clickSelector("#settings-save");
    await sleep(500);
    await cdp.clickSelector("#settings-cancel");
    await sleep(250);

    // 3e) 运行操作条 + 首次使用引导
    const actions = await cdp.evaluate(`(() => {
      const host = document.getElementById("run-actions");
      return {
        exists: Boolean(host),
        labels: host ? Array.from(host.querySelectorAll("button")).map((el) => el.textContent.trim()) : [],
      };
    })()`);
    check(
      "顶栏有运行操作条（当前能做什么集中一处）",
      actions.exists && actions.labels.some((text) => text.includes("打包重启")),
      JSON.stringify(actions),
    );

    const emptyState = await cdp.evaluate(`(() => {
      // 临时进入空状态，看未配置时的引导（改完立即恢复）
      const saved = state.run;
      state.run = null;
      render();
      const text = document.querySelector(".empty")?.innerText || "";
      const hasButtons = document.querySelectorAll(".empty-actions button").length;
      state.run = saved;
      render();
      return { hasButtons, text: text.replace(/\\s+/g, " ").slice(0, 80) };
    })()`);
    check(
      "空状态给出下一步（配置引导 / 或功能介绍）",
      emptyState.text.length > 10,
      JSON.stringify(emptyState),
    );

    // 3d) Git 面板：主操作齐全，但不再有"按钮墙"
    await cdp.clickSelector("#git-btn");
    await sleep(1500);
    const gitPanel = await cdp.evaluate(`(() => {
      // Chrome 用 content-visibility 折叠 <details>，offsetParent 仍非空，
      // 所以要沿着祖先链判断"是否落在某个关闭的 details 里"。
      const visible = Array.from(document.querySelectorAll("#git-body button")).filter((el) => {
        for (let node = el.parentElement; node; node = node.parentElement) {
          if (node.tagName === "DETAILS" && !node.open &&
              !node.querySelector(":scope > summary")?.contains(el)) {
            return false;
          }
        }
        return el.getBoundingClientRect().height > 0;
      });
      const rows = document.querySelectorAll(".git-file").length;
      const boxes = document.querySelectorAll(".git-section").length;
      const closed = Array.from(document.querySelectorAll(".git-section")).filter((el) => !el.open).length;
      const summaryText = Array.from(document.querySelectorAll("#git-body summary")).map((el) => el.textContent.trim());
      return {
        visibleButtons: visible.length,
        labels: visible.map((el) => el.textContent.trim()).slice(0, 12),
        fileRows: rows,
        sections: boxes,
        collapsed: closed,
        summaries: summaryText,
      };
    })()`);
    check(
      "Git 面板不再有按钮墙（可见按钮 ≤ 12，低频块默认折叠）",
      gitPanel.visibleButtons <= 12 &&
        gitPanel.sections >= 3 &&
        gitPanel.collapsed === gitPanel.sections &&
        gitPanel.summaries.some((text) => text.includes("网络代理")) &&
        gitPanel.summaries.some((text) => text.includes("每日开机自动提交")),
      JSON.stringify(gitPanel),
    );
    const gitFileMenu = await cdp.evaluate(`(() => {
      const row = document.querySelector(".git-file");
      if (!row) return null;
      const details = row.querySelector(".git-file-menu");
      details.open = true;
      const items = Array.from(details.querySelectorAll("button")).map((el) => el.textContent.trim());
      details.open = false;
      return { items };
    })()`);
    check(
      "文件级操作收进「⋯」菜单（看差异 / 暂存 / 丢弃）",
      Boolean(gitFileMenu) &&
        ["看差异", "丢弃改动"].every((label) => gitFileMenu.items.includes(label)),
      JSON.stringify(gitFileMenu),
    );
    await cdp.clickSelector("#git-close");
    await sleep(300);

    // 3c2) 多轮续聊：跑完之后接着说下一步 → 追加步骤（不重跑旧的）
    const beforeContinue = await cdp.evaluate(
      `document.querySelectorAll(".card.step").length`,
    );
    await cdp.evaluate(`(() => {
      const el = document.getElementById("continue-input");
      if (el) el.value = "再补一份验收清单";
      return Boolean(el);
    })()`);
    const continueClicked = await cdp.clickSelector("#continue-btn");
    let continueState = null;
    for (let attempt = 0; attempt < 30; attempt += 1) {
      continueState = await cdp.evaluate(`({
        steps: document.querySelectorAll(".card.step").length,
        status: document.getElementById("status-pill").textContent.trim(),
      })`);
      if (continueState.steps > beforeContinue && continueState.status.includes("确认")) break;
      await sleep(300);
    }
    check(
      "跑完之后可以继续对话（追加步骤）",
      continueClicked.hit &&
        continueState &&
        continueState.steps === beforeContinue + 1 &&
        continueState.status.includes("确认"),
      JSON.stringify({ before: beforeContinue, ...continueState }),
    );

    // 3b) 滚轮必须真的能滚动（flex 子项被压扁会导致"内容裁掉且滚不动"）
    for (const [label, selector] of [
      ["时间线", ".timeline"],
      ["运行列表", ".run-list"],
      ["右侧检查器", ".inspector-body"],
    ]) {
      const result = await cdp.wheelScroll(selector);
      if (!result.found) {
        check(`${label}可用滚轮`, false, "元素不存在");
      } else if (!result.scrollable) {
        check(`${label}可用滚轮`, true, "内容未超出可视区，无需滚动");
      } else {
        check(
          `${label}可用滚轮`,
          result.after > result.before,
          `scrollTop ${result.before} → ${result.after}（内容 ${result.scrollH} / 可视 ${result.clientH}）`,
        );
      }
    }

    // 3c) Codex 式能力：Ctrl+K 命令面板
    await cdp.pressKey({ key: "k", code: "KeyK", virtualKeyCode: 75, modifiers: 2 });
    await sleep(500);
    const palette = await cdp.evaluate(`(() => ({
      open: !document.getElementById("palette-modal").hidden,
      items: document.querySelectorAll("#palette-list .palette-item").length,
    }))()`);
    check("Ctrl+K 打开命令面板并列出命令", palette.open && palette.items >= 5, JSON.stringify(palette));
    const paletteRan = await cdp.evaluate(`(async () => {
      const input = document.getElementById("palette-input");
      input.value = "快捷键";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await new Promise((done) => setTimeout(done, 300));
      const item = document.querySelector("#palette-list .palette-item");
      if (!item) return { ok: false };
      item.click();
      await new Promise((done) => setTimeout(done, 600));
      return { ok: true, closed: document.getElementById("palette-modal").hidden };
    })()`);
    check("命令面板可执行命令（搜索并运行）", paletteRan.ok && paletteRan.closed === true, JSON.stringify(paletteRan));

    // 3d) Codex 式任务管理：置顶 / 归档 / 搜索
    const manage = await cdp.evaluate(`(async () => {
      const run = state.runs[0];
      if (!run) return { ok: false, reason: "没有任务可操作" };
      const item = document.querySelector('.run-item[data-run="' + run.id + '"]');
      const buttons = item ? [...item.querySelectorAll(".run-actions button")] : [];
      if (buttons.length < 3) return { ok: false, reason: "任务项没有管理按钮" };
      buttons[0].click();
      await new Promise((done) => setTimeout(done, 1200));
      const pinned = (state.runs.find((entry) => entry.id === run.id) || {}).pinned;
      buttons[0] && (await new Promise((done) => setTimeout(done, 200)));
      const after = document.querySelector('.run-item[data-run="' + run.id + '"]');
      const again = after ? [...after.querySelectorAll(".run-actions button")] : [];
      if (again[0]) { again[0].click(); await new Promise((done) => setTimeout(done, 1200)); }
      const unpinned = (state.runs.find((entry) => entry.id === run.id) || {}).pinned;
      const search = document.getElementById("run-search");
      search.value = "不存在的关键词zzz";
      search.dispatchEvent(new Event("input", { bubbles: true }));
      await new Promise((done) => setTimeout(done, 800));
      const filtered = document.querySelectorAll("#run-list .run-item").length;
      search.value = "";
      search.dispatchEvent(new Event("input", { bubbles: true }));
      await new Promise((done) => setTimeout(done, 800));
      return { ok: true, pinned: pinned === true, unpinned: unpinned === false, filtered };
    })()`);
    check(
      "任务列表支持置顶与搜索过滤",
      manage.ok && manage.pinned && manage.unpinned && manage.filtered === 0,
      JSON.stringify(manage),
    );

    // 3e) 卡片复制按钮（Codex 式一键复制）
    const copyButtons = await cdp.evaluate(
      `document.querySelectorAll(".copy-btn").length`,
    );
    check("纲领/步骤卡片提供复制按钮", copyButtons > 0, `copy-btn=${copyButtons}`);

    // 3f) 右上角「架构 → 执行」：点哪个标签就只改哪一段
    const chipClicked = await cdp.clickSelector("#route-chips button.chip.architect");
    const architectView = await cdp.evaluate(`(() => {
      const modal = document.getElementById("route-modal");
      const dialog = document.querySelector("#route-modal .route-modal");
      const hidden = (sel) => getComputedStyle(document.querySelector(sel)).display === "none";
      return {
        opened: !modal.hidden,
        stage: dialog.dataset.stage,
        title: document.getElementById("route-title").textContent.trim(),
        editorHidden: hidden(".route-field-editor"),
        architectVisible: !hidden(".route-field-architect"),
      };
    })()`);
    check(
      "点「架构」标签只显示架构段设置",
      chipClicked.hit &&
        architectView.opened &&
        architectView.stage === "architect" &&
        architectView.editorHidden &&
        architectView.architectVisible,
      JSON.stringify(architectView),
    );
    // 弹窗里可以切到执行段（不需要关掉再点另一个标签）
    const switched = await cdp.clickSelector("#route-switch");
    const editorView = await cdp.evaluate(`(() => {
      const dialog = document.querySelector("#route-modal .route-modal");
      const hidden = (sel) => getComputedStyle(document.querySelector(sel)).display === "none";
      return {
        stage: dialog.dataset.stage,
        architectHidden: hidden(".route-field-architect"),
        editorVisible: !hidden(".route-field-editor"),
      };
    })()`);
    check(
      "弹窗内可切到执行段",
      switched.hit && editorView.stage === "editor" && editorView.architectHidden,
      JSON.stringify(editorView),
    );
    await cdp.evaluate(`(() => {
      const el = document.getElementById("route-editor-model");
      el.value = "deepseek-v4-uitest";
      return el.value;
    })()`);
    const routeSaved = await cdp.clickSelector("#route-save");
    await sleep(500);
    const chipsText = await cdp.evaluate(
      `document.getElementById("route-chips").innerText.replace(/\\s+/g, " ")`,
    );
    check(
      "右上角架构→执行标签可点击修改",
      routeSaved.hit && chipsText.includes("deepseek-v4-uitest"),
      JSON.stringify({ chips: chipsText }),
    );

    // 还原成"两段都跟随当前配置"，避免自检改坏演示配置
    await cdp.clickSelector("#route-edit");
    await cdp.clickSelector("#route-follow");
    await sleep(400);

    // 4) 右侧标签页切换
    for (const tab of ["changes", "files", "docs", "plan"]) {
      const clicked = await cdp.clickSelector(`.tab[data-tab="${tab}"]`);
      const active = await cdp.evaluate(
        `document.querySelector(".tab.active")?.dataset.tab || null`,
      );
      check(`切换到标签页：${tab}`, clicked.hit && active === tab, `active=${active}`);
    }

    // 5) 新建任务 → 空状态
    const newRun = await cdp.clickSelector("#new-run-btn");
    const cleared = await cdp.evaluate(`document.getElementById("run-title").textContent`);
    check("点击新任务回到空状态", newRun.hit && cleared.includes("新任务"), `标题=${cleared}`);

    // 6) 键盘可用性：输入、Ctrl+A 全选、覆盖输入、Enter 提交、Shift+Enter 换行
    await cdp.evaluate(`document.getElementById("task-input").focus()`);
    await cdp.send("Input.insertText", { text: "第一段文字" });
    const typed = await cdp.evaluate(`document.getElementById("task-input").value`);
    await cdp.pressKey({ key: "a", code: "KeyA", virtualKeyCode: 65, modifiers: 2 });
    await cdp.send("Input.insertText", { text: "浅色主题下的交互自检任务" });
    const afterSelectAll = await cdp.evaluate(`document.getElementById("task-input").value`);
    check(
      "输入框支持常规快捷键（Ctrl+A 全选后覆盖输入）",
      typed === "第一段文字" && afterSelectAll === "浅色主题下的交互自检任务",
      `${typed} → ${afterSelectAll}`,
    );

    // Shift+Enter 只换行、不提交（Enter 发送之后的配套；否则没法写多行需求）。
    // 换行是浏览器的默认行为，所以除了 keyDown/keyUp 还要补一条 char 事件，
    // 否则 CDP 只派发了按键、不会真的插入换行（第一次就是这么误判的）。
    const enter = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 };
    await cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", modifiers: 8, ...enter });
    await cdp.send("Input.dispatchKeyEvent", {
      type: "char",
      modifiers: 8,
      text: "\r",
      unmodifiedText: "\r",
      ...enter,
    });
    await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", modifiers: 8, ...enter });
    await sleep(200);
    const afterShiftEnter = await cdp.evaluate(`(() => ({
      value: document.getElementById("task-input").value,
      title: document.getElementById("run-title").textContent,
    }))()`);
    check(
      "Shift+Enter 只换行、不提交",
      afterShiftEnter.value.endsWith("\n") && afterShiftEnter.title.includes("新任务"),
      JSON.stringify({ value: afterShiftEnter.value, title: afterShiftEnter.title }),
    );

    const send = await cdp.pressKey({
      key: "Enter",
      code: "Enter",
      virtualKeyCode: 13,
    }).then(() => ({ hit: true }));
    await sleep(3000);
    const afterSend = await cdp.evaluate(
      `(() => ({ title: document.getElementById("run-title").textContent,
                 status: document.getElementById("status-pill").textContent,
                 steps: document.querySelectorAll(".step").length,
                 checkItems: document.querySelectorAll(".check-item").length }))()`,
    );
    check("Enter 能提交并产出纲领", send.hit && afterSend.checkItems > 0, JSON.stringify(afterSend));

    // 折叠只针对「已完成」：待执行的步骤必须保持展开（否则纲领一出来就被藏起来了）
    const pendingOpen = await cdp.evaluate(`(() => {
      const rows = Array.from(document.querySelectorAll(".card.step"));
      return {
        total: rows.length,
        collapsed: rows.filter((card) => card.querySelector(".card-body").hidden).length,
      };
    })()`);
    check(
      "待执行的步骤不会被折叠",
      pendingOpen.total > 0 && pendingOpen.collapsed === 0,
      JSON.stringify(pendingOpen),
    );

    const buildTag = await cdp.evaluate(`document.querySelector(".build-tag")?.textContent || ""`);
    check(
      "页面显示构建版本（便于确认是否为最新前端）",
      buildTag.includes("v0.") && !buildTag.includes("__"),
      buildTag,
    );

    // 7) 截图（浅色主题）
    const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
    mkdirSync(dirname(SHOT), { recursive: true });
    writeFileSync(SHOT, Buffer.from(shot.data, "base64"));
    console.log(`\n截图已保存：${SHOT}`);

    const bg = await cdp.evaluate(`getComputedStyle(document.body).backgroundColor`);
    check("页面为浅色背景", bg === "rgb(255, 255, 255)", bg);

    // 8) 运行统计看板：现在在明细面板的「统计」标签里（按需渲染）
    if ((await cdp.evaluate(`document.body.dataset.panel`)) !== "open") {
      await cdp.clickSelector("#inspector-toggle");
      await sleep(400);
    }
    await cdp.clickSelector('#inspector-tabs [data-tab="stats"]');
    await sleep(500);
    let dashboardReady = false;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      dashboardReady = await cdp.evaluate(`(() => {
        const el = document.getElementById("run-dashboard");
        return !!el && !el.hidden && el.querySelectorAll(".dash-card").length >= 6;
      })()`);
      if (dashboardReady) break;
      await sleep(500);
    }
    const overview = await cdp.evaluate(`(() => {
      const el = document.getElementById("run-dashboard");
      if (!el) return null;
      const box = el.getBoundingClientRect();
      return {
        visible: !el.hidden && box.width > 0 && box.height > 0,
        cards: el.querySelectorAll(".dash-card").length,
        text: (el.textContent || "").replace(/\\s+/g, " ").trim(),
      };
    })()`);
    check(
      "统计看板展示总耗时 / token / 上下文占用 / 重试次数",
      Boolean(
        dashboardReady &&
          overview &&
          overview.visible &&
          overview.cards >= 6 &&
          ["总耗时", "token", "上下文占用", "重试次数"].every((word) => overview.text.includes(word)),
      ),
      JSON.stringify(overview ? { visible: overview.visible, cards: overview.cards } : null),
    );

    const dashToggle = await cdp.clickSelector("#dash-toggle");
    await sleep(250);
    const dashDetail = await cdp.evaluate(`(() => {
      const panel = document.getElementById("run-dashboard");
      if (!panel) return null;
      const box = panel.querySelector(".dash-detail");
      const rows = panel.querySelectorAll(".dash-step-row");
      return {
        hidden: box ? box.hidden : null,
        rows: rows.length,
        cells: rows[0] ? rows[0].querySelectorAll(".dash-cell").length : 0,
      };
    })()`);
    check(
      "可展开到每一步：明细含耗时 / token / 上下文 / 用量来源 / 路由",
      Boolean(
        dashToggle.hit &&
          dashDetail &&
          dashDetail.hidden === false &&
          dashDetail.rows >= 1 &&
          dashDetail.cells >= 5,
      ),
      JSON.stringify(dashDetail),
    );

    // 每步的 token / 耗时 / 上下文构成只在**新记录**里才有：
    // 让刚提交的这条真的跑完（顺便验证「运行操作条」的确认按钮）
    const approved = await cdp.evaluate(`(() => {
      const btn = Array.from(document.querySelectorAll("#run-actions button"))
        .find((el) => el.textContent.includes("确认并开始执行"));
      if (!btn) return false;
      btn.click();
      return true;
    })()`);
    let finished = "";
    for (let attempt = 0; attempt < 60; attempt += 1) {
      finished = await cdp.evaluate(`document.getElementById("status-pill").textContent.trim()`);
      if (finished.includes("已完成")) break;
      await sleep(500);
    }
    if ((await cdp.evaluate(`document.body.dataset.panel`)) !== "open") {
      await cdp.clickSelector("#inspector-toggle");
      await sleep(300);
    }
    await cdp.clickSelector('#inspector-tabs [data-tab="stats"]');
    await sleep(500);
    await cdp.clickSelector("#dash-toggle");
    await sleep(400);

    const stepSpend = await cdp.evaluate(`(() => {
      const head = document.querySelector(".card.step .card-head .muted");
      return head ? head.textContent.trim() : "";
    })()`);
    check(
      "步骤卡片内联显示耗时与 token",
      approved && finished.includes("已完成") && stepSpend.includes("tokens") && stepSpend.includes("上下文"),
      JSON.stringify({ approved, finished, head: stepSpend.slice(0, 140) }),
    );

    // 执行长任务时，已完成步骤的明细应当默认收起（只留标题那一行摘要），点标题能展开回去
    const collapse = await cdp.evaluate(`(() => {
      const card = document.querySelector('.card.step[data-status="done"]');
      if (!card) return { missing: true };
      const head = card.querySelector(".card-head");
      const body = card.querySelector(".card-body");
      const arrow = () => card.querySelector(".step-caret")?.textContent.trim() || "";
      const state = () => ({
        hidden: Boolean(body.hidden),
        arrow: arrow(),
        aria: head.getAttribute("aria-expanded"),
        rowVisible: head.getBoundingClientRect().height > 0,
      });
      const collapsed = state();
      head.click();
      const expanded = state();
      head.click();
      const collapsedAgain = state();
      return { collapsed, expanded, collapsedAgain };
    })()`);
    check(
      "已完成步骤默认折叠，点标题行可展开 / 再收起",
      !collapse.missing &&
        collapse.collapsed.hidden === true &&
        collapse.collapsed.rowVisible === true &&
        collapse.collapsed.arrow === "▸" &&
        collapse.expanded.hidden === false &&
        collapse.expanded.arrow === "▾" &&
        collapse.expanded.aria === "true" &&
        collapse.collapsedAgain.hidden === true,
      JSON.stringify(collapse),
    );

    const composition = await cdp.evaluate(`(() => {
      const rows = Array.from(document.querySelectorAll(".dash-step-row"));
      const text = rows.map((row) => row.textContent).join(" | ");
      return {
        rows: rows.length,
        hasComposition: text.includes("上下文构成"),
        hasRounds: text.includes("调用轮次"),
        snippet: text.replace(/\\s+/g, " ").slice(0, 180),
      };
    })()`);
    check(
      "看板显示每步的上下文构成与调用轮次",
      composition.rows >= 1 && composition.hasComposition && composition.hasRounds,
      JSON.stringify(composition),
    );

    const dashText = await cdp.evaluate(
      `(() => { const el = document.getElementById("run-dashboard"); return el ? el.textContent : ""; })()`,
    );
    check(
      "运行状态与未知用量显示为中文（无 undefined / NaN 泄漏）",
      ["规划中", "待确认", "执行中", "已阻塞", "已完成", "失败", "已取消"].some((word) =>
        dashText.includes(word),
      ) && !/undefined|NaN/.test(dashText),
      dashText.replace(/\s+/g, " ").slice(0, 140),
    );

    // 9) 窄视口：输入区仍可见，统计面板可独立滚动
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1024,
      height: 640,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await sleep(350);
    const narrow = await cdp.evaluate(`(() => {
      const inView = (node) => {
        if (!node) return false;
        const box = node.getBoundingClientRect();
        return box.width > 0 && box.height > 0 && box.top >= -1 && box.bottom <= window.innerHeight + 1;
      };
      const panel = document.getElementById("run-dashboard");
      return {
        send: inView(document.getElementById("send-btn")),
        dir: inView(document.getElementById("target-dir")),
        panel: !!panel && !panel.hidden,
        overflow: panel ? getComputedStyle(panel).overflowY : "",
      };
    })()`);
    check(
      "窄视口下输入区保持可见，统计面板可独立滚动",
      narrow.send && narrow.dir && narrow.panel && narrow.overflow === "auto",
      JSON.stringify(narrow),
    );
    await cdp.send("Emulation.clearDeviceMetricsOverride");
    await sleep(200);

    // 10) 回归：设置弹窗隐藏后不得占据遮罩与点击区域
    const backdrop = await cdp.evaluate(`(() => {
      const modal = document.getElementById("settings-modal");
      if (!modal) return null;
      const box = modal.getBoundingClientRect();
      const hit = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
      return {
        hidden: modal.hidden,
        display: getComputedStyle(modal).display,
        area: Math.round(box.width * box.height),
        blockedByModal: !!hit && (hit === modal || modal.contains(hit)),
      };
    })()`);
    check(
      "设置弹窗隐藏时不占据遮罩与点击区域",
      Boolean(
        backdrop &&
          backdrop.hidden &&
          backdrop.display === "none" &&
          backdrop.area === 0 &&
          !backdrop.blockedByModal,
      ),
      JSON.stringify(backdrop),
    );
  } finally {
    child.kill();
  }

  const failed = results.filter((item) => !item.ok);
  console.log(`\n合计 ${results.length} 项，失败 ${failed.length} 项`);
  return failed.length === 0 ? 0 : 1;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error("自检失败：", error.message);
    process.exit(1);
  });
