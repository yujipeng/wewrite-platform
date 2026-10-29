// 前端交互可机械判定 harness（JEE-1930 返修 · 4 条阻断 + 3 项拦截覆盖）。
//
// 用 Node `vm` 加载真实 app/web/app.js，提供最小 DOM 桩 + 可注入 fetch，执行真实逻辑：
//   1. 路由落点：currentRoute() 对 #/jobs、#/runs、#/preview、#/publish 返回确定页（非 home）；
//   2. 窄屏导航入口：index.html 存在 menuToggle/drawerMask，且 bindDrawer 把二者与侧栏串联
//      （点击开关→sidebar.open；真实派发一次 nav 链接 click→sidebar.open 被移除）；
//   3. 失败态类名：renderJobBody 生成 .stepper 下 .step.fail + .bubble + .lbl（与 CSS .step.fail 一致）；
//   4. 四态硬边界：无 ID 入口对 500/网络错误渲染可重试错误态、403 渲染无权限态、成功空列表渲染空态；
//   5. 发布确认页：授权成功后地址 ↔ 正文 ↔ h1 ↔ 高亮 四者一致。
//
// 输出 JSON 到 stdout；断言失败即 process.exit(1)。本文件由 pytest 调用，也可 `node` 直接跑。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const appJs = readFileSync(join(here, "..", "app", "web", "app.js"), "utf8");
const indexHtml = readFileSync(join(here, "..", "app", "web", "index.html"), "utf8");

class ClassList {
  constructor() {
    this._set = new Set();
  }
  _sync() {
    this._set = new Set(String(this._owner && this._owner.className || "").split(/\s+/).filter(Boolean));
  }
  toggle(name, force) {
    this._sync();
    const want = force === undefined ? !this._set.has(name) : !!force;
    if (want) this._set.add(name);
    else this._set.delete(name);
    if (this._owner) this._owner.className = [...this._set].join(" ");
    return want;
  }
  add(...names) {
    this._sync();
    names.forEach((n) => this._set.add(n));
    if (this._owner) this._owner.className = [...this._set].join(" ");
  }
  remove(...names) {
    this._sync();
    names.forEach((n) => this._set.delete(n));
    if (this._owner) this._owner.className = [...this._set].join(" ");
  }
  contains(name) {
    this._sync();
    return this._set.has(name);
  }
}

class FakeEl {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.textContent = "";
    this._innerHTML = "";
    this.disabled = false;
    this.value = "";
    this.hidden = false;
    this.id = "";
    this.classList = new ClassList();
    this.classList._owner = this;
    this._scrollCalls = 0;
    this._lastScroll = null;
  }
  scrollIntoView(opts) {
    // 记录调用（行为断言用），不真正滚动。
    this._scrollCalls += 1;
    this._lastScroll = opts;
  }
  set innerHTML(v) {
    this._innerHTML = String(v);
    this.children = [];
    this.textContent = "";
  }
  get innerHTML() {
    return this._innerHTML;
  }
  appendChild(c) {
    if (c == null) return c;
    if (typeof c === "string") {
      c = { nodeType: 3, textContent: c, children: [], classList: { toggle() {}, contains() { return false; } } };
    }
    if (c.nodeType === 3) {
      this.textContent += c.textContent;
      c.parentNode = this;
      this.children.push(c);
      return c;
    }
    c.parentNode = this;
    this.children.push(c);
    return c;
  }
  addEventListener(t, f) {
    (this.listeners[t] = this.listeners[t] || []).push(f);
  }
  removeEventListener() {}
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === "class") this.className = String(v);
    if (k === "id") this.id = String(v);
    if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
  }
  getAttribute(k) {
    if (k === "class") return this.className;
    if (k === "id") return this.id;
    return this.attributes[k];
  }
  querySelectorAll() {
    return [];
  }
  querySelector() {
    return null;
  }
  remove() {}
  cloneNode() {
    return new FakeEl(this.tagName);
  }
  dispatchEvent() {}
}

function findByClass(root, cls) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.classList && typeof n.classList.contains === "function" && n.classList.contains(cls)) out.push(n);
    (n.children || []).forEach(walk);
  };
  walk(root);
  return out;
}

function collectText(root) {
  const parts = [];
  const walk = (n) => {
    if (!n) return;
    if (typeof n.textContent === "string" && n.textContent && n.nodeType !== 3) parts.push(n.textContent);
    (n.children || []).forEach(walk);
  };
  walk(root);
  return parts.join("");
}

function findByText(root, text) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.textContent === text) out.push(n);
    (n.children || []).forEach(walk);
  };
  walk(root);
  return out;
}

function findByTag(root, tag) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.tagName === tag.toUpperCase()) out.push(n);
    (n.children || []).forEach(walk);
  };
  walk(root);
  return out;
}

// 派发某按钮的 click（返回是否派发成功）；渲染动作是 fire-and-forget，配合宿主 macrotask 冲刷。
async function clickButton(root, text) {
  const btn = findByText(root, text).find((n) => n.tagName === "BUTTON");
  const handler = btn && btn.listeners && btn.listeners["click"] && btn.listeners["click"][0];
  if (handler) await Promise.resolve(handler());
  await new Promise((r) => setTimeout(r, 10));
  return !!handler;
}

// 派发含指定文案的按钮 click（精确匹配 textContent，返回按钮元素）。
function findButton(root, text) {
  return findByText(root, text).find((n) => n.tagName === "BUTTON");
}

// 派发 el 上第 index 个 click 处理器并冲刷宿主 macrotask。
async function dispatchClick(el, index = 0) {
  const handler = el && el.listeners && el.listeners["click"] && el.listeners["click"][index];
  if (handler) await Promise.resolve(handler());
  await new Promise((r) => setTimeout(r, 10));
  return !!handler;
}

// 可注入 fetch 的最小上下文。
// fetchImpl: (path, opts) => 返回对象 { ok, status, statusText, json } 或直接 throw。
// confirmImpl: 可选，覆盖 window.confirm（默认 false，测试需要确认时传 true）。
function makeContext({ hash, fetchImpl, confirmImpl }) {
  const els = {};
  const navAnchors = [
    ["home", "#/"],
    ["hotspots", "#/hotspots"],
    ["candidates", "#/candidates"],
    ["jobs", "#/jobs"],
    ["detail", "#/runs"],
    ["preview", "#/preview"],
    ["publish", "#/publish"],
    ["config", "#/config"],
  ].map(([nav, href]) => {
    const a = new FakeEl("a");
    a.dataset.nav = nav;
    a.setAttribute("href", href);
    return a;
  });

  // 侧栏元素：让 bindDrawer 的 sidebar.querySelectorAll(".nav a") 真实拿到 nav 链接，
  // 从而真实绑定 closeDrawer 到每个链接，抽屉测试才能真正派发点击。
  const sidebarEl = new FakeEl("aside");
  sidebarEl.querySelectorAll = (sel) => (sel === ".nav a" ? navAnchors : []);

  const getEl = (id) => {
    if (id === "sidebar") return sidebarEl;
    if (!els[id]) {
      els[id] = new FakeEl("div");
      els[id].id = id;
    }
    return els[id];
  };

  const location = { hash, replace() {} };
  const windowObj = {
    addEventListener() {},
    alert() {},
    confirm: confirmImpl || (() => false),
    prompt() { return null; },
  };
  const documentObj = {
    getElementById: getEl,
    createElement: (t) => new FakeEl(t),
    querySelectorAll: (sel) => {
      if (sel === ".nav a") return navAnchors;
      if (sel === ".sidebar") return [sidebarEl];
      return [];
    },
    querySelector: (sel) => {
      if (sel === ".sidebar") return sidebarEl;
      return null;
    },
    createTextNode: (t) => ({ nodeType: 3, textContent: String(t), children: [], classList: { toggle() {}, contains() { return false; } } }),
  };

  const sandbox = {
    document: documentObj,
    location,
    window: windowObj,
    fetch: fetchImpl || (async () => { throw new Error("no-network"); }),
    setInterval: () => 0,
    clearInterval: () => {},
    setTimeout: () => 0,
    clearTimeout: () => {},
    crypto: { randomUUID: () => "00000000-0000-4000-8000-000000000000" },
    navigator: { clipboard: { writeText: async () => {} } },
    console,
  };
  sandbox.globalThis = sandbox;
  return { sandbox, els, navAnchors, sidebarEl, location };
}

const checks = [];
function check(name, cond) {
  checks.push({ name, ok: !!cond });
}

// 跨 realm 安全地 await vm 内返回的 promise。
async function runAwait(ctx, code) {
  const p = vm.runInContext(code, ctx);
  await Promise.resolve(p);
}

function okJson(body) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}
function errJson(status, body = {}) {
  return { ok: false, status, statusText: "ERR", json: async () => body };
}

// ---------- 1) 路由落点 ----------
{
  const { sandbox, location } = makeContext({ hash: "#/" });
  const ctx = vm.createContext(sandbox);
  vm.runInContext(appJs, ctx, { filename: "app.js" });

  const cases = [
    ["#/jobs", "job"],
    ["#/runs", "detail"],
    ["#/preview", "preview"],
    ["#/publish", "publish"],
  ];
  for (const [hash, expected] of cases) {
    location.hash = hash;
    const route = vm.runInContext("currentRoute()", ctx);
    check(`route ${hash} -> ${expected}`, route && route.page === expected && route.page !== "home");
  }
}

// ---------- 2) 窄屏导航入口 + 抽屉串联（真实派发 nav 链接点击） ----------
{
  const { sandbox, els, navAnchors, sidebarEl, location } = makeContext({ hash: "#/runs" });
  const ctx = vm.createContext(sandbox);
  vm.runInContext(appJs, ctx, { filename: "app.js" });

  check("index.html 有 menuToggle 抽屉开关", /id="menuToggle"/.test(indexHtml));
  check("index.html 有 drawerMask 遮罩", /id="drawerMask"/.test(indexHtml));

  const menuToggle = els["menuToggle"];
  const drawerMask = els["drawerMask"];

  // bindDrawer 已在模块加载时执行：点击 menuToggle 打开抽屉
  menuToggle.listeners["click"] && menuToggle.listeners["click"][0]();
  check("点击 menuToggle 打开抽屉（sidebar.open）", sidebarEl.classList.contains("open"));
  drawerMask.listeners["click"] && drawerMask.listeners["click"][0]();
  check("点击遮罩关闭抽屉", !sidebarEl.classList.contains("open"));

  // 再次打开，然后真实派发一次 .nav a 的 click，断言 closeDrawer 被触发、open 被移除
  menuToggle.listeners["click"] && menuToggle.listeners["click"][0]();
  const opened = sidebarEl.classList.contains("open");
  const firstNavClick = navAnchors[0].listeners["click"];
  const dispatched = Array.isArray(firstNavClick) && firstNavClick.length > 0;
  if (dispatched) firstNavClick[0]();
  check("抽屉打开后真实派发 nav 链接点击", opened && dispatched);
  check("点击 nav 链接关闭抽屉（sidebar.open 移除）", !sidebarEl.classList.contains("open"));
}

// ---------- 3) 失败态类名 + stepper 气泡结构 ----------
{
  const { sandbox, els } = makeContext({ hash: "#/" });
  const ctx = vm.createContext(sandbox);
  vm.runInContext(appJs, ctx, { filename: "app.js" });

  const job = {
    job_id: "j-test0000000000",
    run_id: "20261026-080000-a1b2c3",
    status: "failed",
    step: "review",
    attempt: 1,
    progress: { kind: "step", completed: 2, total: 4 },
    images: [],
  };
  vm.runInContext(`renderJobBody(${JSON.stringify(job)}, "20261026-080000-a1b2c3")`, ctx);
  const app = els["app"];
  const steps = findByClass(app, "step");
  check("renderJobBody 输出 4 个 .step", steps.length === 4);
  const failStep = steps.find((s) => s.classList.contains("fail"));
  check("失败步骤使用 .fail 类（非 .failed）", !!failStep && !failStep.classList.contains("failed"));
  const bubble = failStep && failStep.children.find((c) => c.classList && c.classList.contains("bubble"));
  const lbl = failStep && failStep.children.find((c) => c.classList && c.classList.contains("lbl"));
  check("失败步骤含 .bubble 气泡", !!bubble);
  check("失败步骤含 .lbl 标签", !!lbl);
  check("失败步骤气泡文案为 ✕", !!bubble && bubble.textContent === "✕");
}

// ---------- 4) 四态硬边界：无 ID 入口错误态/无权限态/空态可区分 ----------
async function testFourStates() {
  // 4.1 任务详情（#/runs）
  {
    const { sandbox, els } = makeContext({
      hash: "#/runs",
      fetchImpl: async () => errJson(500),
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderDetail(null)");
    const app = els["app"];
    check("renderDetail(null) 500 → 错误态(含重试)", findByClass(app, "error-block").length > 0 && findByText(app, "重试").length > 0);
  }
  {
    const { sandbox, els } = makeContext({
      hash: "#/runs",
      fetchImpl: async () => { throw new Error("network down"); },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderDetail(null)");
    const app = els["app"];
    check("renderDetail(null) 网络错误 → 错误态(含重试)", findByClass(app, "error-block").length > 0 && findByText(app, "重试").length > 0);
  }
  {
    const { sandbox, els } = makeContext({
      hash: "#/runs",
      fetchImpl: async () => errJson(403, { code: "CSRF_INVALID", message: "denied" }),
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderDetail(null)");
    const app = els["app"];
    const text = collectText(app);
    check("renderDetail(null) 403 → 无权限态", text.includes("本机工作台目录不可读"));
  }
  {
    const { sandbox, els } = makeContext({
      hash: "#/runs",
      fetchImpl: async () => okJson({ runs: [] }),
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderDetail(null)");
    const app = els["app"];
    const text = collectText(app);
    check("renderDetail(null) 空列表 → 空态(非错误态)", findByClass(app, "error-block").length === 0 && text.includes("还没有"));
  }

  // 4.2 产物预览 / 发布确认 / 生成进行中（#/jobs）的 500 错误态
  {
    const { sandbox, els } = makeContext({ hash: "#/preview", fetchImpl: async () => errJson(500) });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderPreview(null)");
    check("renderPreview(null) 500 → 错误态(含重试)", findByClass(els["app"], "error-block").length > 0 && findByText(els["app"], "重试").length > 0);
  }
  {
    const { sandbox, els } = makeContext({ hash: "#/publish", fetchImpl: async () => errJson(500) });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderPublish(null)");
    check("renderPublish(null) 500 → 错误态(含重试)", findByClass(els["app"], "error-block").length > 0 && findByText(els["app"], "重试").length > 0);
  }
  {
    // 生成任务列表非 403（jobs 接口 500）：必须错误态，不能落到 renderJobEmpty
    const { sandbox, els } = makeContext({
      hash: "#/jobs",
      fetchImpl: async (path) => {
        if (path === "/api/v1/runs") return okJson({ runs: [{ run_id: "20261026-080000-a1b2c3" }] });
        return errJson(500);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderJob(null)");
    const app = els["app"];
    const text = collectText(app);
    check("renderJob(null) jobs 500 → 错误态(含重试,非空态)", findByClass(app, "error-block").length > 0 && findByText(app, "重试").length > 0 && !text.includes("还没有生成任务"));
  }
  {
    // 生成任务列表 403：无权限态
    const { sandbox, els } = makeContext({
      hash: "#/jobs",
      fetchImpl: async (path) => {
        if (path === "/api/v1/runs") return okJson({ runs: [{ run_id: "20261026-080000-a1b2c3" }] });
        return errJson(403);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderJob(null)");
    const text = collectText(els["app"]);
    check("renderJob(null) jobs 403 → 无权限态", text.includes("本机工作台目录不可读"));
  }
}

// ---------- 5) 发布确认页：授权成功后地址 ↔ 正文 ↔ h1 ↔ 高亮一致 ----------
async function testPublishConsistency() {
  const runId = "20261026-080000-a1b2c3";
  const detail = {
    run_id: runId,
    title: "测试发布标题",
    status: "completed",
    mode: "complete",
    permissions: { publish: false },
    publish: { status: "not_published", media_id: null },
    editorial: { decision: "pass", publishable: true },
  };
  const { sandbox, els, navAnchors, location } = makeContext({
    hash: `#/runs/${runId}/publish`,
    fetchImpl: async (path, opts) => {
      if (path === "/api/v1/bootstrap") return okJson({ csrf_token: "t" });
      if (path === `/api/v1/runs/${runId}`) return okJson(detail);
      if (path === `/api/v1/runs/${runId}/publish/preflight`) return okJson({ ok: false, checks: [], revision: "r1" });
      if (path === `/api/v1/runs/${runId}/publish/permission`) return okJson({ ok: true });
      throw new Error("unexpected path " + path);
    },
  });
  const ctx = vm.createContext(sandbox);
  vm.runInContext(appJs, ctx, { filename: "app.js" });

  // 完整走 render()：更新 h1、顶栏动作、侧栏高亮，再渲染发布页
  await runAwait(ctx, "render()");
  const pageTitle = els["pageTitle"];
  check("发布页初始 h1 为「发布确认」", pageTitle && pageTitle.textContent === "发布确认");

  const publishNav = navAnchors[6];
  check("发布页初始 publish 导航高亮", publishNav.classList.contains("active"));

  // 点击「开启发布授权」，成功后应原地 renderPublish(runId)（不跳详情）。
  // handler 内部 fire-and-forget 调 renderPublish（未 await），用宿主 macrotask 冲刷其异步链。
  const appBefore = els["app"];
  const authorizeBtn = findByText(appBefore, "开启发布授权")[0];
  const handler = authorizeBtn && authorizeBtn.listeners["click"] && authorizeBtn.listeners["click"][0];
  if (handler) await Promise.resolve(handler());
  await new Promise((r) => setTimeout(r, 10));
  const appAfter = els["app"];
  const afterText = collectText(appAfter);
  check("授权成功后正文仍是发布页(含发布闸门)", afterText.includes("发布闸门（P7）") && !afterText.includes("选题"));
  check("授权成功后地址不变(#/runs/{id}/publish)", location.hash === `#/runs/${runId}/publish`);
  check("授权成功后 h1 仍是「发布确认」", pageTitle.textContent === "发布确认");
  check("授权成功后 publish 导航仍高亮", publishNav.classList.contains("active"));
}


// ---------- 6) 任务详情页：授权成功后应留在详情页（本轮复验点） ----------
async function testDetailPublishConsistency() {
  const runId = "20261026-080000-a1b2c3";
  const detail = {
    run_id: runId,
    title: "测试发布标题",
    status: "completed",
    mode: "complete",
    permissions: { publish: false },
    publish: { status: "not_published", media_id: null },
    editorial: { decision: "pass", publishable: true },
  };
  const { sandbox, els, navAnchors, location } = makeContext({
    hash: `#/runs/${runId}`,
    fetchImpl: async (path, opts) => {
      if (path === "/api/v1/bootstrap") return okJson({ csrf_token: "t" });
      if (path === `/api/v1/runs/${runId}`) return okJson(detail);
      if (path === `/api/v1/runs/${runId}/publish/preflight`) return okJson({ ok: false, checks: [], revision: "r1" });
      if (path === `/api/v1/runs/${runId}/publish/permission`) return okJson({ ok: true });
      throw new Error("unexpected path " + path);
    },
  });
  const ctx = vm.createContext(sandbox);
  vm.runInContext(appJs, ctx, { filename: "app.js" });

  await runAwait(ctx, "render()");
  const pageTitle = els["pageTitle"];
  check("详情页初始 h1 为「任务详情」", pageTitle && pageTitle.textContent === "任务详情");
  const detailNav = navAnchors[4];
  check("详情页初始 detail 导航高亮", detailNav.classList.contains("active"));

  const appBefore = els["app"];
  const authorizeBtn = findByText(appBefore, "开启发布授权")[0];
  check("详情页存在发布闸门授权按钮", !!authorizeBtn);
  const handler = authorizeBtn && authorizeBtn.listeners["click"] && authorizeBtn.listeners["click"][0];
  if (handler) await Promise.resolve(handler());
  await new Promise((r) => setTimeout(r, 10));

  const afterText = collectText(els["app"]);
  check("详情页授权成功后正文仍是详情页(含选题)", afterText.includes("选题"));
  check("详情页授权成功后正文未变成发布确认页", !afterText.includes("发布闸门（P7）") || afterText.includes("选题"));
  check("详情页授权成功后地址不变(#/runs/{id})", location.hash === `#/runs/${runId}`);
  check("详情页授权成功后 h1 仍是「任务详情」", pageTitle.textContent === "任务详情");
  check("详情页授权成功后 detail 导航仍高亮", detailNav.classList.contains("active"));
}

// ============ 7) P3 预览：来源下拉标注可用性 + 默认选可用来源 + 404 按 reason 分流 ============
async function testPreviewSourceAvailability() {
  const runId = "20261026-080000-a1b2c3";
  const artifacts = { article: true, draft: false, illustrated_article: false };

  // 场景 A：只有 article，无 draft → 默认选中 article，draft/带图版标「（未生成）」且 disabled
  {
    const { sandbox, els } = makeContext({
      hash: `#/runs/${runId}/preview`,
      fetchImpl: async (path, opts) => {
        if (path === "/api/v1/bootstrap") return okJson({ csrf_token: "t" });
        if (path === `/api/v1/runs/${runId}`) return okJson({ run_id: runId, artifacts });
        if (path === "/api/v1/themes") return okJson({ themes: [{ name: "professional-clean", description: "" }] });
        if (path === "/api/v1/preview") {
          // 渲染成功（正常路径不 404）
          return okJson({ html: "<h1>预览</h1>", paste_safe_html: "", title: "T", digest: "D", source_sha256: "x".repeat(64), filtered_count: 0 });
        }
        throw new Error("unexpected path " + path);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderPreview(" + JSON.stringify(runId) + ")");
    const appEl = els["app"];
    const selects = findByTag(appEl, "select");
    const sourceSel = selects[0];
    const options = sourceSel.children.filter((c) => c.tagName === "OPTION");
    check("预览：source 下拉选项含（未生成）标注", options.some((o) => String(o.textContent).includes("（未生成）")));
    const draftOpt = options.find((o) => o.getAttribute("value") === "draft");
    const articleOpt = options.find((o) => o.getAttribute("value") === "article");
    check("预览：draft 不可选(disabled)", draftOpt && draftOpt.disabled === true);
    check("预览：默认选中 article（第一个可用来源）", sourceSel.value === "article" && articleOpt && articleOpt.disabled === false);
    // 正常路径渲染成功：iframe 出现且无 404 提示（点渲染按钮触发，初始自动渲染受 theme 加载时序影响）
    await clickButton(appEl, "渲染预览");
    const text = collectText(appEl);
    check("预览：正常路径无「产物不存在或尚未生成」", !text.includes("产物不存在或尚未生成"));
    const iframes = findByTag(appEl, "iframe");
    check("预览：正常路径渲染出 iframe", iframes.length > 0);
  }

  // 场景 B：无任何产物 → 渲染按钮禁用 + 引导文案
  {
    const { sandbox, els } = makeContext({
      hash: `#/runs/${runId}/preview`,
      fetchImpl: async (path) => {
        if (path === "/api/v1/bootstrap") return okJson({ csrf_token: "t" });
        if (path === `/api/v1/runs/${runId}`) return okJson({ run_id: runId, artifacts: { article: false, draft: false, illustrated_article: false } });
        if (path === "/api/v1/themes") return okJson({ themes: [{ name: "professional-clean", description: "" }] });
        throw new Error("unexpected path " + path);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderPreview(" + JSON.stringify(runId) + ")");
    const appEl = els["app"];
    const text = collectText(appEl);
    check("预览：无产物时按钮禁用", findByText(appEl, "渲染预览").some((b) => b.disabled === true));
    check("预览：无产物时显示引导文案", text.includes("该任务还没有可预览的正文"));
  }

  // 场景 C：404 artifact_missing → 提示可切换来源（不再是「产物不存在或尚未生成」）
  {
    const { sandbox, els } = makeContext({
      hash: `#/runs/${runId}/preview`,
      fetchImpl: async (path, opts) => {
        if (path === "/api/v1/bootstrap") return okJson({ csrf_token: "t" });
        if (path === `/api/v1/runs/${runId}`) return okJson({ run_id: runId, artifacts });
        if (path === "/api/v1/themes") return okJson({ themes: [{ name: "professional-clean", description: "" }] });
        if (path === "/api/v1/preview") {
          return errJson(404, { code: "NOT_FOUND", message: "资源不存在", details: { reason: "artifact_missing", artifact: "draft", run_id: runId } });
        }
        throw new Error("unexpected path " + path);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderPreview(" + JSON.stringify(runId) + ")");
    const appEl = els["app"];
    // 点渲染按钮触发 404（初始自动渲染受 theme 加载时序影响，故显式派发）
    await clickButton(appEl, "渲染预览");
    const text = collectText(appEl);
    check("预览：404 artifact_missing 不再显示旧文案", !text.includes("产物不存在或尚未生成"));
    check("预览：404 artifact_missing 显示可切换来源提示", text.includes("还没有生成") && text.includes("当前实际存在的来源"));
  }

  // 场景 D：404 theme_not_found → 主题提示
  {
    const { sandbox, els } = makeContext({
      hash: `#/runs/${runId}/preview`,
      fetchImpl: async (path, opts) => {
        if (path === "/api/v1/bootstrap") return okJson({ csrf_token: "t" });
        if (path === `/api/v1/runs/${runId}`) return okJson({ run_id: runId, artifacts });
        if (path === "/api/v1/themes") return okJson({ themes: [{ name: "professional-clean", description: "" }] });
        if (path === "/api/v1/preview") {
          return errJson(404, { code: "NOT_FOUND", message: "资源不存在", details: { reason: "theme_not_found", theme: "no-such" } });
        }
        throw new Error("unexpected path " + path);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderPreview(" + JSON.stringify(runId) + ")");
    const appEl = els["app"];
    await clickButton(appEl, "渲染预览");
    const text = collectText(appEl);
    check("预览：404 theme_not_found 显示主题提示", text.includes("不存在或已不可用"));
  }
}

// ============ 8) 写作模型未配置：可点击引导入口 + 错误文案不被 code 遮蔽（JEE-1955） ============
async function testWriterNotConfiguredInteraction() {
  // 8.1 未配置状态：按钮可点击，点击后真实触发 scrollIntoView + config-highlight
  {
    const { sandbox, els } = makeContext({
      hash: "#/config",
      fetchImpl: async (path) => {
        if (path === "/api/v1/config") return okJson({
          theme: "professional-clean",
          image: { provider: "", model: "", base_url: "", api_key_configured: false },
          wechat: { appid: "", author: "编辑", appid_configured: false, secret_configured: false },
          writer: { provider: "deepseek", model: "deepseek-chat", base_url: "https://api.deepseek.com", temperature: 1.0, max_tokens: 4000, api_key_configured: false, writer_configured: false },
          permissions: { publish: false },
          env_managed: [],
        });
        if (path === "/api/v1/style") return okJson({ raw: "name: ''\n", revision: "r1" });
        if (path === "/api/v1/image-providers") return okJson({ providers: [] });
        if (path === "/api/v1/writer-providers") return okJson({ providers: [] });
        if (path === "/api/v1/themes") return okJson({ themes: [] });
        throw new Error("unexpected path " + path);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderConfig()");

    const appEl = els["app"];
    const btn = findButton(appEl, "去配置写作模型");
    check("未配置时按钮可点击（文案「去配置写作模型」）", !!btn && btn.disabled !== true);

    // 找到「写作模型（文章生成）」区块对应的 section（按钮点击前先记录 scrollIntoView 是否已装）
    const writerSection = findWriterSection(appEl);
    check("存在「写作模型（文章生成）」区块", !!writerSection);

    await dispatchClick(btn);
    check("点击未配置按钮后真实调用 scrollIntoView", !!writerSection && writerSection._scrollCalls > 0);
    check("点击未配置按钮后区块加 config-highlight", !!writerSection && writerSection.classList.contains("config-highlight"));
  }

  // 8.2 WRITER_NOT_CONFIGURED 响应：渲染中文 message，不出现裸 code
  {
    let generateCalled = 0;
    const { sandbox, els } = makeContext({
      hash: "#/config",
      fetchImpl: async (path, opts) => {
        if (path === "/api/v1/config") return okJson({
          theme: "professional-clean",
          image: { provider: "", model: "", base_url: "", api_key_configured: false },
          wechat: { appid: "", author: "编辑", appid_configured: false, secret_configured: false },
          writer: { provider: "deepseek", model: "deepseek-chat", base_url: "https://api.deepseek.com", temperature: 1.0, max_tokens: 4000, api_key_configured: true, writer_configured: true },
          permissions: { publish: false },
          env_managed: [],
        });
        if (path === "/api/v1/style") return okJson({ raw: "name: ''\n", revision: "r1" });
        if (path === "/api/v1/image-providers") return okJson({ providers: [] });
        if (path === "/api/v1/writer-providers") return okJson({ providers: [] });
        if (path === "/api/v1/themes") return okJson({ themes: [] });
        if (path === "/api/v1/bootstrap") return okJson({ csrf_token: "t" });
        if (path === "/api/v1/style/generate") {
          generateCalled += 1;
          return errJson(503, { code: "WRITER_NOT_CONFIGURED", message: "写作模型未配置：请在上方「写作模型」区填写 API Key 并保存" });
        }
        throw new Error("unexpected path " + path);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "renderConfig()");

    const appEl = els["app"];
    // 已配置时按钮文案为「生成」，填一项引导信息后点击触发请求
    const genBtn = findButton(appEl, "生成");
    check("已配置时按钮文案为「生成」", !!genBtn && genBtn.disabled !== true);

    const industryInput = findByClass(appEl, "style-custom-input")[0];
    industryInput.value = "科技";
    await dispatchClick(genBtn);

    check("点击「生成」确实发起了 style/generate 请求", generateCalled === 1);

    const genText = collectText(appEl);
    check("WRITER_NOT_CONFIGURED 渲染中文可操作提示", genText.includes("写作模型未配置") && genText.includes("API Key"));
    check("WRITER_NOT_CONFIGURED 渲染不含裸 code", !genText.includes("WRITER_NOT_CONFIGURED"));
  }
}

// ============ 9) JEE-1963：重新生成按钮 → 真实 POST /regenerate 端点 ============
async function testRegenerateInteraction() {
  const runId = "20261026-080000-a1b2c3";
  const jobId = "j-test000001";

  // 场景 A：有 length_warning 的成功任务 → 按钮出现，点击真实 POST 到 /regenerate
  {
    let regenerateCalls = 0;
    const detail = {
      run_id: runId,
      title: "标题",
      status: "completed",
      mode: "complete",
      permissions: { publish: false },
      publish: { status: "not_published", media_id: null },
      editorial: { decision: "pass", publishable: true },
      length_warning: { actual: 300, target_min: 1600, target_max: 2400, target: 2000 },
    };
    const { sandbox, els } = makeContext({
      hash: `#/runs/${runId}`,
      confirmImpl: () => true,
      fetchImpl: async (path, opts) => {
        if (path === "/api/v1/bootstrap") return okJson({ csrf_token: "t" });
        if (path === `/api/v1/runs/${runId}`) return okJson(detail);
        if (path === `/api/v1/jobs?run_id=${runId}`) return okJson({ jobs: [{ job_id: jobId, run_id: runId, status: "succeeded" }] });
        if (path === `/api/v1/jobs/${jobId}/regenerate`) {
          regenerateCalls += 1;
          return okJson({ job_id: "j-new", run_id: runId, status: "queued" });
        }
        throw new Error("unexpected path " + path);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "render()");

    const appEl = els["app"];
    const regenBtn = findButton(appEl, "重新生成");
    check("有 length_warning 时详情页出现「重新生成」按钮", !!regenBtn);

    await dispatchClick(regenBtn);
    check("点击「重新生成」真实 POST /api/v1/jobs/{job_id}/regenerate", regenerateCalls === 1);
  }

  // 场景 B：无 length_warning 的成功任务 → 按钮不出现（反向用例）
  {
    const detail = {
      run_id: runId,
      title: "标题",
      status: "completed",
      mode: "complete",
      permissions: { publish: false },
      publish: { status: "not_published", media_id: null },
      editorial: { decision: "pass", publishable: true },
    };
    const { sandbox, els } = makeContext({
      hash: `#/runs/${runId}`,
      fetchImpl: async (path) => {
        if (path === "/api/v1/bootstrap") return okJson({ csrf_token: "t" });
        if (path === `/api/v1/runs/${runId}`) return okJson(detail);
        if (path === `/api/v1/jobs?run_id=${runId}`) return okJson({ jobs: [{ job_id: jobId, run_id: runId, status: "succeeded" }] });
        throw new Error("unexpected path " + path);
      },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "app.js" });
    await runAwait(ctx, "render()");

    const appEl = els["app"];
    check("无 length_warning 的成功任务不出现「重新生成」按钮", !findButton(appEl, "重新生成"));
  }
}

// 找到「写作模型（文章生成）」区块对应的 section 元素。
// highlightWriterSection 作用于 app.js 里的 writerSection 局部 div，该 div 内是 writerRow(dl.kv)，
// 其 kv 表单包含「API 密钥」dt。因此从「API 密钥」dt 沿 parentNode 向上三级到 writerSection。
function findWriterSection(root) {
  const apiKeyDt = findByText(root, "API 密钥").find((n) => n.tagName === "DT");
  if (!apiKeyDt || !apiKeyDt.parentNode || !apiKeyDt.parentNode.parentNode) return null;
  const writerRow = apiKeyDt.parentNode.parentNode; // dt -> dl(kv) -> writerRow(div)
  return writerRow.parentNode; // writerSection(div)
}

// ============ 主入口 ============
(async () => {
  await testFourStates();
  await testPublishConsistency();
  await testDetailPublishConsistency();
  await testPreviewSourceAvailability();
  await testWriterNotConfiguredInteraction();
  await testRegenerateInteraction();

  const failed = checks.filter((c) => !c.ok);
  const summary = {
    total: checks.length,
    passed: checks.length - failed.length,
    failed: failed.map((c) => c.name),
    checks,
  };
  console.log(JSON.stringify(summary, null, 2));
  if (failed.length > 0) process.exit(1);
})();
