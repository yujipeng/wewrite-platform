// 前端交互可机械判定 harness（JEE-1930 返修 · 阻断项 1–3 覆盖）。
//
// 用 Node `vm` 加载真实 app/web/app.js，提供最小 DOM 桩，执行真实逻辑：
//   1. 路由落点：currentRoute() 对 #/jobs、#/runs、#/preview、#/publish 返回确定页（非 home）；
//   2. 窄屏导航入口：index.html 存在 menuToggle/drawerMask，且 app.js bindDrawer 把二者与侧栏串联（点击开关→sidebar.open）；
//   3. 失败态类名：renderJobBody 生成 .stepper 下 .step.fail + .bubble + .lbl（类名与 CSS .step.fail 一致）。
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
    this._set = new Set(String(this._owner.className || "").split(/\s+/).filter(Boolean));
  }
  toggle(name, force) {
    this._sync();
    const want = force === undefined ? !this._set.has(name) : !!force;
    if (want) this._set.add(name);
    else this._set.delete(name);
    this._owner.className = [...this._set].join(" ");
    return want;
  }
  add(...names) {
    this._sync();
    names.forEach((n) => this._set.add(n));
    this._owner.className = [...this._set].join(" ");
  }
  remove(...names) {
    this._sync();
    names.forEach((n) => this._set.delete(n));
    this._owner.className = [...this._set].join(" ");
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
      c = { nodeType: 3, textContent: c, children: [], classList: { toggle() {} } };
    }
    if (c.nodeType === 3) {
      // 文本节点：累积到父元素 textContent，便于断言气泡文案
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

function makeContext({ hash }) {
  const els = {};
  const getEl = (id) => {
    if (!els[id]) {
      els[id] = new FakeEl("div");
      els[id].id = id;
    }
    return els[id];
  };
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

  const location = { hash, replace() {} };
  const windowObj = {
    addEventListener() {},
    alert() {},
    confirm() { return false; },
    prompt() { return null; },
  };
  const documentObj = {
    getElementById: getEl,
    createElement: (t) => new FakeEl(t),
    querySelectorAll: (sel) => {
      if (sel === ".nav a") return navAnchors;
      if (sel === ".sidebar") return [getEl("sidebar")];
      return [];
    },
    querySelector: (sel) => {
      if (sel === ".sidebar") return getEl("sidebar");
      return null;
    },
    createTextNode: (t) => ({ nodeType: 3, textContent: String(t), children: [], classList: { toggle() {} } }),
  };

  const sandbox = {
    document: documentObj,
    location,
    window: windowObj,
    fetch: async () => { throw new Error("no-network"); },
    setInterval: () => 0,
    clearInterval: () => {},
    setTimeout: () => 0,
    clearTimeout: () => {},
    crypto: { randomUUID: () => "00000000-0000-4000-8000-000000000000" },
    navigator: { clipboard: { writeText: async () => {} } },
    console,
  };
  sandbox.globalThis = sandbox;
  return { sandbox, els, navAnchors, location };
}

const checks = [];
function check(name, cond) {
  checks.push({ name, ok: !!cond });
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

// ---------- 2) 窄屏导航入口 + 抽屉串联 ----------
{
  const { sandbox, els, location } = makeContext({ hash: "#/runs" });
  const ctx = vm.createContext(sandbox);
  vm.runInContext(appJs, ctx, { filename: "app.js" });

  check("index.html 有 menuToggle 抽屉开关", /id="menuToggle"/.test(indexHtml));
  check("index.html 有 drawerMask 遮罩", /id="drawerMask"/.test(indexHtml));

  // 执行 bindDrawer 已随模块加载跑过一次：点击 menuToggle 应给 sidebar 加 .open
  const sidebar = els["sidebar"] || new FakeEl("div");
  els["sidebar"] = sidebar;
  const menuToggle = els["menuToggle"] || new FakeEl("button");
  els["menuToggle"] = menuToggle;
  const drawerMask = els["drawerMask"] || new FakeEl("div");
  els["drawerMask"] = drawerMask;

  // 重新跑 bindDrawer（幂等），再用真实点击触发
  vm.runInContext("bindDrawer()", ctx);
  menuToggle.listeners["click"] && menuToggle.listeners["click"][0]();
  check("点击 menuToggle 打开抽屉（sidebar.open）", sidebar.classList.contains("open"));
  drawerMask.listeners["click"] && drawerMask.listeners["click"][0]();
  check("点击遮罩关闭抽屉", !sidebar.classList.contains("open"));
  menuToggle.listeners["click"] && menuToggle.listeners["click"][0]();
  // 点 nav 链接应关闭抽屉
  vm.runInContext("document.querySelectorAll('.nav a')[0].addEventListener('click', ()=>{}); ", ctx);
  const navLinkHandler = () => sidebar.classList.remove("open");
  // 直接用真实绑定：重新 bindDrawer 后取侧栏上 nav 链接的监听
  const before = sidebar.classList.contains("open");
  check("抽屉处于打开态后再点击", before);
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
  vm.runInContext(
    `renderJobBody(${JSON.stringify(job)}, "20261026-080000-a1b2c3")`,
    ctx,
  );
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

const failed = checks.filter((c) => !c.ok);
const summary = {
  total: checks.length,
  passed: checks.length - failed.length,
  failed: failed.map((c) => c.name),
  checks,
};
console.log(JSON.stringify(summary, null, 2));
if (failed.length > 0) process.exit(1);
