// WeWrite 工作台前端（原生 ES modules，无构建）
// JEE-1930 S8-b 返修：视觉对齐设计图（深色侧边栏三组导航 + 顶栏面包屑/h1/动作区 + 卡片化）。
// 仅改视觉层，后端 API 契约与行为不变；四态（空/加载/错误/无权限）齐全。

const app = document.getElementById("app");

// ============ 视觉返修：顶栏与侧边栏 chrome ============

// 页面标题/面包屑文案表（对齐设计图 PAGES）
const PAGE_CHROME = {
  home: { title: "工作台（运行列表）", crumb: "工作台" },
  detail: { title: "任务详情", crumb: "任务详情" },
  preview: { title: "产物预览", crumb: "产物预览" },
  hotspots: { title: "每日热榜", crumb: "每日热榜" },
  candidates: { title: "选题确认（候选对比）", crumb: "选题确认" },
  job: { title: "生成进行中", crumb: "生成进行中" },
  runJob: { title: "生成进行中", crumb: "生成进行中" },
  config: { title: "配置", crumb: "配置" },
  publish: { title: "发布确认", crumb: "发布确认" },
};

function updateChrome(route) {
  const chrome = PAGE_CHROME[route.page] || PAGE_CHROME.home;
  const pageTitle = document.getElementById("pageTitle");
  const crumbCur = document.getElementById("crumbCur");
  if (pageTitle) pageTitle.textContent = chrome.title;
  if (crumbCur) crumbCur.textContent = chrome.crumb;
}

// 顶栏动作区：按页面注入右上角主操作（视觉返修，不动行为）
function renderTopActions(page) {
  const actions = document.getElementById("topActions");
  if (!actions) return;
  actions.innerHTML = "";
  if (page === "home") {
    actions.appendChild(el("a", { class: "btn primary", href: "#/hotspots", text: "＋ 新建任务" }));
  } else if (page === "hotspots") {
    actions.appendChild(el("a", { class: "btn primary", href: "#/candidates", text: "生成候选 →" }));
  } else if (page === "publish") {
    actions.appendChild(el("a", { class: "btn", href: "#/runs", text: "返回任务详情" }));
  }
}

// 状态 chip（对齐设计图 statusChip：●已完成 / ●失败 / ●进行中）
function statusChip(s) {
  if (s === "completed") return el("span", { class: "chip ok" }, [el("span", { class: "dot completed" }), "已完成"]);
  if (s === "failed") return el("span", { class: "chip err" }, [el("span", { class: "dot failed" }), "失败"]);
  if (s === "active") return el("span", { class: "chip warn" }, [el("span", { class: "dot active" }), "进行中"]);
  return el("span", { class: "chip" }, [STATUS_TEXT[s] || s || "未知"]);
}

// 步骤时间线 stepper 气泡（对齐设计图步骤气泡）
function stepperSteps(steps) {
  const stepper = el("div", { class: "stepper" });
  const names = { topic: "选题", write: "写作", review: "审稿", visual: "配图" };
  const icons = { topic: "◎", write: "✎", review: "✓", visual: "🖼" };
  for (const key of STEP_ORDER) {
    const info = steps[key] || {};
    const st = info.status || "pending";
    let cls = "step";
    let glyph;
    if (st === "completed") { cls += " done"; glyph = "✓"; }
    else if (st === "failed") { cls += " fail"; glyph = "✕"; }
    else if (st === "in_progress") { cls += " cur"; glyph = icons[key]; }
    else { glyph = st === "skipped" ? "—" : icons[key]; }
    stepper.appendChild(el("div", { class: cls }, [
      el("div", { class: "bubble" }, [glyph]),
      el("div", { class: "lbl" }, [names[key] || key]),
    ]));
  }
  return stepper;
}

// 仅用 textContent 渲染用户数据，绝不 innerHTML 注入任意字符串（安全红线）。
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    if (child == null) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function emptyState(text) {
  return el("div", { class: "state-block" }, [text]);
}
function loadingState(text) {
  return el("div", { class: "state-block" }, [text || "正在加载…"]);
}
function errorState(text, retry) {
  const block = el("div", { class: "state-block error-block" }, [text || "加载失败"]);
  if (retry) {
    block.appendChild(el("br"));
    block.appendChild(el("button", { class: "btn", text: "重试", onclick: retry }));
  }
  return block;
}
function forbiddenState() {
  return el("div", { class: "state-block" }, ["本机工作台目录不可读，无法展示任务列表"]);
}

const STATUS_TEXT = {
  active: "进行中",
  failed: "失败",
  completed: "已完成",
  unknown: "未知",
};

const MODE_TEXT = { draft: "草稿模式", complete: "完整生成", publish: "发布模式" };

const ARTIFACT_LABELS = {
  brief: "任务书",
  article: "终稿",
  draft: "初稿",
  illustrated_article: "带图版",
  review_report: "审稿报告",
  images_manifest: "图片清单",
  sources: "来源",
  preview: "预览",
};

const STEP_LABELS = { topic: "选题", write: "写作", review: "审稿", visual: "视觉" };
const STEP_STATUS_TEXT = {
  pending: "待执行",
  in_progress: "进行中",
  completed: "已完成",
  failed: "失败",
  skipped: "已跳过",
};

// P6 生成进度常量与轮询句柄（必须声明在 render() 首次调用之前，否则冷启动
// 直达 #/jobs/{id} 会命中 TDZ，抛 Cannot access '_jobPollTimer' before initialization）。
const JOB_STATUS_TEXT = {
  queued: "排队中",
  running: "进行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "结果未知",
};
const STEP_ORDER = ["topic", "write", "review", "visual"];
const IMAGE_STATUS_TEXT = {
  pending: "排队中",
  running: "生成中",
  succeeded: "已完成",
  failed: "失败",
};
let _jobPollTimer = null;

function stopJobPolling() {
  if (_jobPollTimer) {
    clearInterval(_jobPollTimer);
    _jobPollTimer = null;
  }
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let code = "HTTP_" + res.status;
    let message = res.statusText;
    try {
      const data = await res.json();
      code = data.code || code;
      message = data.message || message;
    } catch (_) { /* 非 JSON 错误响应 */ }
    const err = new Error(message);
    err.code = code;
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function currentRoute() {
  const hash = location.hash || "#/";
  // 无 ID 入口：落到「最近一次运行」确定页，绝不静默回退工作台
  if (hash === "#/jobs") return { page: "job" };
  if (hash === "#/runs") return { page: "detail" };
  if (hash === "#/preview") return { page: "preview" };
  if (hash === "#/publish") return { page: "publish" };
  // #/jobs/{job_id}
  const jm = hash.match(/^#\/jobs\/([a-z0-9-]+)$/);
  if (jm) return { page: "job", jobId: jm[1] };
  // #/runs/{run_id}/job
  const rjm = hash.match(/^#\/runs\/([0-9]{8}-[0-9]{6}-[a-f0-9]{6})\/job$/);
  if (rjm) return { page: "runJob", runId: rjm[1] };
  // #/runs/{run_id}
  const m = hash.match(/^#\/runs\/([0-9]{8}-[0-9]{6}-[a-f0-9]{6})$/);
  if (m) return { page: "detail", runId: m[1] };
  // #/runs/{run_id}/preview
  const pm = hash.match(/^#\/runs\/([0-9]{8}-[0-9]{6}-[a-f0-9]{6})\/preview$/);
  if (pm) return { page: "preview", runId: pm[1] };
  // #/runs/{run_id}/publish
  const pbm = hash.match(/^#\/runs\/([0-9]{8}-[0-9]{6}-[a-f0-9]{6})\/publish$/);
  if (pbm) return { page: "publish", runId: pbm[1] };
  if (hash === "#/hotspots") return { page: "hotspots" };
  if (hash === "#/candidates") return { page: "candidates" };
  if (hash === "#/config") return { page: "config" };
  return { page: "home" };
}

async function render() {
  const route = currentRoute();
  // 离开 P6 时先停掉 job 轮询，否则 setInterval 会把整页反复画回 job 页（导航锁死）。
  if (route.page !== "job" && route.page !== "runJob") {
    stopJobPolling();
  }
  updateChrome(route);
  renderTopActions(route.page);
  document.querySelectorAll(".nav a").forEach((n) => {
    const nav = n.dataset.nav;
    const active =
      (nav === "home" && route.page === "home") ||
      (nav === "hotspots" && route.page === "hotspots") ||
      (nav === "candidates" && route.page === "candidates") ||
      (nav === "jobs" && (route.page === "job" || route.page === "runJob")) ||
      (nav === "detail" && route.page === "detail") ||
      (nav === "preview" && route.page === "preview") ||
      (nav === "publish" && route.page === "publish") ||
      (nav === "config" && route.page === "config");
    n.classList.toggle("active", active);
  });
  if (route.page === "home") {
    await renderHome();
  } else if (route.page === "detail") {
    await renderDetail(route.runId);
  } else if (route.page === "preview") {
    await renderPreview(route.runId);
  } else if (route.page === "hotspots") {
    await renderHotspots();
  } else if (route.page === "candidates") {
    await renderCandidates();
  } else if (route.page === "config") {
    await renderConfig();
  } else if (route.page === "publish") {
    await renderPublish(route.runId);
  } else if (route.page === "job") {
    await renderJob(route.jobId);
  } else if (route.page === "runJob") {
    await renderRunJob(route.runId);
  }
}

// ============ 无 ID 入口的最近运行解析 ============
// `#/runs`、`#/preview`、`#/publish`、`#/jobs` 等无 ID 入口必须有确定落点：
// 默认解析最近一次运行；无任何运行时渲染明确空态页，绝不静默回退工作台。
// 返回三态 { runId, denied, error }：
//   - denied=true：无权限（非回环 Host 403），需渲染无权限态；
//   - error 非空：请求失败（500/网络异常），需渲染可重试错误态；
//   - 否则成功：runId 非空为最近运行，runId 为空才是真空态。
async function resolveLatestRunState() {
  try {
    const data = await api("/api/v1/runs");
    const runs = (data.runs || []).filter((r) => r && r.run_id);
    // list_runs 按创建时间倒序，runs[0] 即最近一次运行
    return { runId: runs[0] ? runs[0].run_id : null, denied: false, error: null };
  } catch (err) {
    if (err && err.status === 403) {
      return { runId: null, denied: true, error: null };
    }
    return { runId: null, denied: false, error: err };
  }
}

async function renderDetailOrEmpty(runId) {
  if (runId) {
    await renderDetail(runId);
    return;
  }
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back-link", href: "#/", text: "← 返回任务列表" }));
  app.appendChild(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [el("h2", { text: "任务详情" })]),
    el("div", { class: "card-body" }, [emptyState("还没有任何任务，去「每日热榜」→「选题确认」发起第一篇")]),
  ]));
}

async function renderHome() {
  app.innerHTML = "";
  // 页面标题已由顶栏 h1 承载（「工作台（运行列表）」），内容区不再重复旧「任务列表」h1
  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-head" }, [
    el("h2", { text: "运行列表" }),
    el("span", { class: "hint" }, ["点击任意行进入任务详情"]),
  ]));
  const body = el("div", { class: "card-body" });
  card.appendChild(body);

  // 搜索框（本地过滤题名与 run_id）
  const search = el("input", {
    class: "search-input",
    type: "search",
    placeholder: "按题名或 run_id 搜索…",
  });
  const controls = el("div", { class: "preview-controls" }, [search]);
  body.appendChild(controls);

  const container = el("div");
  body.appendChild(container);
  app.appendChild(card);

  container.appendChild(loadingState("正在加载任务列表…"));

  let runs = [];
  try {
    const data = await api("/api/v1/runs");
    runs = data.runs || [];
  } catch (err) {
    container.innerHTML = "";
    if (err.status === 403) {
      container.appendChild(forbiddenState());
    } else {
      container.appendChild(errorState("加载失败，已保留上次列表，请点「刷新」重试", renderHome));
    }
    return;
  }

  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const filtered = runs.filter(
      (r) =>
        !q ||
        (r.title || "").toLowerCase().includes(q) ||
        (r.run_id || "").toLowerCase().includes(q)
    );
    container.innerHTML = "";
    if (runs.length === 0) {
      container.appendChild(emptyState("还没有任务，点「新建任务」从每日热榜开始"));
      return;
    }
    if (filtered.length === 0) {
      container.appendChild(emptyState("没有匹配的任务"));
      return;
    }
    container.appendChild(runsTable(filtered));
  };

  search.addEventListener("input", draw);
  draw();
}

// 运行列表表格（对齐设计图 .table：run_id / 标题 / 状态 / 模式 / 产物 / 发布）
function runsTable(runs) {
  const table = el("table", { class: "table" });
  const thead = el("thead", {}, [
    el("tr", {}, [
      el("th", { text: "run_id" }),
      el("th", { text: "标题" }),
      el("th", { text: "状态" }),
      el("th", { text: "模式" }),
      el("th", { text: "产物" }),
      el("th", { text: "发布" }),
    ]),
  ]);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const r of runs) {
    tbody.appendChild(runTableRow(r));
  }
  table.appendChild(tbody);
  return table;
}

function runTableRow(r) {
  const status = r.status || "unknown";
  const titleCell = el("td", { class: "title-cell" }, [
    el("div", {}, [r.title || "未命名文章"]),
    el("div", { class: "sub" }, [(r.mode ? MODE_TEXT[r.mode] || r.mode + " · " : "") + (r.created ? "创建于 " + r.created : "")]),
  ]);

  const artBadges = el("td");
  const artWrap = el("div", { class: "artifact-badges" });
  for (const [name, label] of Object.entries(ARTIFACT_LABELS)) {
    const on = !!(r.artifacts && r.artifacts[name]);
    artWrap.appendChild(el("span", { class: "artifact-badge " + (on ? "on" : "off"), text: label }));
  }
  artBadges.appendChild(artWrap);

  const published = r.publish && r.publish.media_id;
  const publishCell = el("td", {}, [published ? el("span", { class: "chip ok" }, ["已发布"]) : el("span", { class: "chip" }, ["未发布"])]);

  const row = el("tr", { onclick: () => { location.hash = "#/runs/" + r.run_id; } }, [
    el("td", {}, [el("span", { class: "mono" }, [r.run_id])]),
    titleCell,
    el("td", { class: "status-cell" }, [statusChip(status)]),
    el("td", {}, [r.mode ? el("span", { class: "chip violet" }, [MODE_TEXT[r.mode] || r.mode]) : el("span", { class: "chip" }, ["—"])]),
    artBadges,
    publishCell,
  ]);
  return row;
}

async function renderDetail(runId) {
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back-link", href: "#/", text: "← 返回任务列表" }));

  // 无 ID 入口（#/runs）：解析最近一次运行；无权限/请求失败/无运行分别渲染对应状态页
  if (!runId) {
    const resolved = await resolveLatestRunState();
    if (resolved.denied) {
      app.innerHTML = "";
      app.appendChild(forbiddenState());
      return;
    }
    if (resolved.error) {
      app.innerHTML = "";
      app.appendChild(errorState("加载任务列表失败：" + (resolved.error.code || resolved.error.message || "网络错误"), () => renderDetail(null)));
      return;
    }
    if (!resolved.runId) {
      await renderDetailOrEmpty(null);
      return;
    }
    location.replace("#/runs/" + resolved.runId);
    return;
  }

  const container = el("div");
  app.appendChild(container);
  container.appendChild(loadingState("正在加载任务详情…"));

  let detail;
  try {
    detail = await api("/api/v1/runs/" + runId);
  } catch (err) {
    container.innerHTML = "";
    if (err.status === 403) {
      container.appendChild(forbiddenState());
    } else if (err.status === 404) {
      container.appendChild(errorState("任务不存在或已被移除", renderHome));
    } else {
      container.appendChild(errorState("加载失败：" + (err.code || err.message), () => renderDetail(runId)));
    }
    return;
  }

  container.innerHTML = "";
  container.appendChild(detailBody(detail));
}

function detailBody(d) {
  const status = d.status || "unknown";
  // 页面标题由顶栏 h1 承载（「任务详情」），此处用卡片头 h2 展示文章题名
  const title = el("div", { class: "card-head" }, [
    el("h2", { text: d.title || "未命名文章" }),
    el("span", { class: "hint" }, [d.run_id]),
  ]);

  const kv = el("dl", { class: "kv" });
  const rows = [
    ["run_id", d.run_id],
    ["状态", STATUS_TEXT[status] || status],
    ["模式", MODE_TEXT[d.mode] || d.mode],
    ["创建时间", d.created || "未提供"],
    ["更新时间", d.updated || "未提供"],
    ["发布权限", d.permissions && d.permissions.publish ? "已开启" : "未开启"],
  ];
  for (const [k, v] of rows) {
    kv.appendChild(el("dt", { text: k }));
    kv.appendChild(el("dd", { text: v == null || v === "" ? "未提供" : String(v) }));
  }

  // 状态与操作卡片（对齐设计图：chip + 主操作按钮）
  const statusCard = el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [el("h2", { text: "状态与操作" })]),
    el("div", { class: "card-body" }, [statusChip(status)]),
  ]);

  const previewBtn = el("a", {
    class: "btn",
    href: "#/runs/" + d.run_id + "/preview",
    text: "👁 预览产物",
  });

  // 生成进度入口（存在 job 时提供）
  const jobLink = el("a", {
    class: "btn",
    href: "#/runs/" + d.run_id + "/job",
    text: "生成进度",
  });

  const infoCard = el("div", { class: "card" }, [title, el("div", { class: "card-body" }, [kv])]);

  const sections = [infoCard, statusCard, el("div", { class: "preview-controls" }, [previewBtn, jobLink])];

  // P2-F5：失败/取消/未知任务的重试入口
  if (status === "failed" || status === "cancelled" || status === "unknown") {
    const retryBtn = el("button", {
      class: "btn",
      text: "重试",
      onclick: async () => {
        try {
          const jobs = await api("/api/v1/jobs?run_id=" + encodeURIComponent(d.run_id));
          const candidates = (jobs.jobs || []).filter((j) => ["failed", "cancelled", "unknown"].indexOf(j.status) >= 0);
          if (candidates.length === 0) {
            window.alert("未找到可重试的生成任务");
            return;
          }
          const job = candidates[0];
          const token = await csrfToken();
          const res = await fetch(`/api/v1/jobs/${job.job_id}/resume`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
            body: JSON.stringify({}),
          });
          if (!res.ok) {
            let code = "HTTP_" + res.status, message = res.statusText;
            try { const b = await res.json(); code = b.code || code; message = b.message || message; } catch (_) {}
            throw Object.assign(new Error(message), { code, status: res.status });
          }
          location.hash = "#/runs/" + d.run_id + "/job";
        } catch (err) {
          window.alert("重试失败：" + (err.code || err.message));
        }
      },
    });
    sections.push(el("div", { class: "preview-controls" }, [retryBtn]));
  }

  // 选题（卡片化：card-head + card-body，替代裸 section-title + dl.kv）
  const topic = d.topic || {};
  const topicKv = el("dl", { class: "kv" });
  const topicRows = [
    ["标题", topic.title],
    ["关键词", (topic.keywords || []).join("、")],
    ["来源", topic.source],
    ["切入点提示", topic.framework_hint],
  ];
  for (const [k, v] of topicRows) {
    topicKv.appendChild(el("dt", { text: k }));
    topicKv.appendChild(el("dd", { text: v == null || v === "" ? "未提供" : String(v) }));
  }
  sections.push(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [el("h2", { text: "选题" })]),
    el("div", { class: "card-body" }, [topicKv]),
  ]));

  // 配图设置（卡片化）
  const visual = d.visual || {};
  const visualKv = el("dl", { class: "kv" });
  for (const [k, v] of [
    ["视觉模式", visual.mode],
    ["最大图片数", visual.max_images],
    ["最大成本", visual.max_cost],
  ]) {
    visualKv.appendChild(el("dt", { text: k }));
    visualKv.appendChild(el("dd", { text: v == null || v === "" ? "未提供" : String(v) }));
  }
  sections.push(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [el("h2", { text: "配图设置" })]),
    el("div", { class: "card-body" }, [visualKv]),
  ]));

  // 步骤时间线（对齐设计图 stepper 气泡）
  const steps = d.steps || {};
  const stepCard = el("div", { class: "card" });
  stepCard.appendChild(el("div", { class: "card-head" }, [
    el("h2", { text: "步骤时间线" }),
    el("span", { class: "hint" }, ["steps 状态机"]),
  ]));
  const stepBody = el("div", { class: "card-body" });
  if (Object.keys(steps).length === 0) {
    stepBody.appendChild(emptyState("任务尚无步骤记录"));
  } else {
    stepBody.appendChild(stepperSteps(steps));
    // 保留失败步骤错误信息（对齐设计图 sub 文案）
    for (const [step, info] of Object.entries(steps)) {
      const s = info || {};
      if (s.error) {
        stepBody.appendChild(el("div", { class: "step-error" }, [(STEP_LABELS[step] || step) + " 错误：" + s.error]));
      }
    }
  }
  stepCard.appendChild(stepBody);
  sections.push(stepCard);

  // 审稿结论与 SEO（卡片化）
  const editorial = d.editorial || {};
  const seo = d.seo || {};
  const edKv = el("dl", { class: "kv" });
  const edRows = [
    ["审稿结论", editorial.decision ? (editorial.decision === "pass" ? "通过" : "不通过") : "未审稿"],
    ["可发布", editorial.publishable == null ? "未审稿" : (editorial.publishable ? "是" : "否")],
    ["SEO 标题", seo.title],
    ["摘要", seo.digest],
    ["标签", (seo.tags || []).join("、")],
    ["质量分", seo.quality_score],
  ];
  for (const [k, v] of edRows) {
    edKv.appendChild(el("dt", { text: k }));
    edKv.appendChild(el("dd", { text: v == null || v === "" ? "未提供" : String(v) }));
  }
  sections.push(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [el("h2", { text: "审稿结论与 SEO" })]),
    el("div", { class: "card-body" }, [edKv]),
  ]));

  // 产物清单（卡片化）
  const artBadges = el("div", { class: "artifact-badges" });
  for (const [name, label] of Object.entries(ARTIFACT_LABELS)) {
    const on = !!(d.artifacts && d.artifacts[name]);
    artBadges.appendChild(el("span", { class: "artifact-badge " + (on ? "on" : "off"), text: label + "：" + (on ? "已落盘" : "尚未生成") }));
  }
  sections.push(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [el("h2", { text: "产物清单" })]),
    el("div", { class: "card-body" }, [artBadges]),
  ]));

  // 发布概要（卡片化）
  const publish = d.publish || {};
  const pubBox = el("div", { class: "publish-box" }, [
    el("div", { text: "发布状态：" + (publish.status || "not_published") }),
    el("div", { text: "media_id：" + (publish.media_id || "无") }),
    publish.unknown_at ? el("div", { text: "结果未知时间：" + publish.unknown_at }) : null,
  ]);
  sections.push(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [el("h2", { text: "发布概要" })]),
    el("div", { class: "card-body" }, [pubBox]),
  ]));

  // 发布闸门（P7）——详情页调用方：操作成功后留在详情页
  sections.push(renderPublishGate(d.run_id, d, () => renderDetail(d.run_id)));

  // 失败步骤去向
  if (d.last_error) {
    sections.push(el("div", { class: "section-title", text: "失败去向" }));
    sections.push(el("div", { class: "error-block", text: "最后错误：" + d.last_error }));
  }

  return el("div", {}, sections);
}

window.addEventListener("hashchange", render);
// 未保存变更提示：写作风格编辑器有改动时，刷新/离开给一次确认（验收第 11 条）。
window.addEventListener("beforeunload", (e) => {
  if (window.__styleDirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
render();

// ============ 窄屏抽屉导航 ============
// 窄屏隐藏侧栏后提供替代导航入口：顶栏 ☰ 打开抽屉，点遮罩或选中链接后收起。
function bindDrawer() {
  const sidebar = document.querySelector(".sidebar");
  const menuToggle = document.getElementById("menuToggle");
  const drawerMask = document.getElementById("drawerMask");
  if (!sidebar || !menuToggle || !drawerMask) return;
  const openDrawer = () => {
    sidebar.classList.add("open");
    drawerMask.hidden = false;
    menuToggle.setAttribute("aria-expanded", "true");
  };
  const closeDrawer = () => {
    sidebar.classList.remove("open");
    drawerMask.hidden = true;
    menuToggle.setAttribute("aria-expanded", "false");
  };
  menuToggle.addEventListener("click", () => {
    if (sidebar.classList.contains("open")) closeDrawer();
    else openDrawer();
  });
  drawerMask.addEventListener("click", closeDrawer);
  sidebar.querySelectorAll(".nav a").forEach((a) => a.addEventListener("click", closeDrawer));
}
bindDrawer();

// ============ M2：P3 产物预览 ============

function sourceLabel(source) {
  return { draft: "初稿 draft.md", article: "终稿 article.md", illustrated_article: "带图版 article-illustrated.md" }[source] || source;
}

async function renderPreviewEmpty() {
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back-link", href: "#/", text: "← 返回任务列表" }));
  app.appendChild(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [el("h2", { text: "产物预览" })]),
    el("div", { class: "card-body" }, [emptyState("还没有任何任务，暂无产物可预览")]),
  ]));
}

async function renderPreview(runId) {
  app.innerHTML = "";

  // 无 ID 入口（#/preview）：解析最近一次运行；无权限/请求失败/无运行分别渲染对应状态页
  if (!runId) {
    const resolved = await resolveLatestRunState();
    if (resolved.denied) {
      app.innerHTML = "";
      app.appendChild(forbiddenState());
      return;
    }
    if (resolved.error) {
      app.innerHTML = "";
      app.appendChild(errorState("加载任务列表失败：" + (resolved.error.code || resolved.error.message || "网络错误"), () => renderPreview(null)));
      return;
    }
    if (!resolved.runId) {
      await renderPreviewEmpty();
      return;
    }
    location.replace("#/runs/" + resolved.runId + "/preview");
    return;
  }

  app.appendChild(el("a", { class: "back-link", href: "#/runs/" + runId, text: "← 返回任务详情" }));
  /* 页面标题由顶栏 h1 承载 */

  // 常驻口径提示：每个来源/主题切换后提示仍在
  app.appendChild(el("div", { class: "notice" }, [
    "本地预览，图片上传后 URL 会被替换",
    el("br"),
    "本预览不承诺与微信草稿字节一致",
  ]));

  const controls = el("div", { class: "preview-controls" });
  const sourceSelect = el("select");
  for (const s of ["draft", "article", "illustrated_article"]) {
    sourceSelect.appendChild(el("option", { value: s, text: sourceLabel(s) }));
  }
  const themeSelect = el("select");
  themeSelect.appendChild(el("option", { value: "", text: "主题加载中…" }));
  const renderBtn = el("button", { class: "btn", text: "渲染预览" });
  const copyBtn = el("button", { class: "btn", text: "复制正文 HTML" });
  copyBtn.disabled = true;
  const meta = el("div", { class: "muted", text: "" });
  controls.appendChild(sourceSelect);
  controls.appendChild(themeSelect);
  controls.appendChild(renderBtn);
  controls.appendChild(copyBtn);
  app.appendChild(controls);
  app.appendChild(meta);

  const iframeHost = el("div");
  app.appendChild(iframeHost);

  // 主题列表走 GET /api/v1/themes（名称来自 wewrite 的 list_themes()）
  let themes = [];
  try {
    const data = await api("/api/v1/themes");
    themes = data.themes || [];
    themeSelect.innerHTML = "";
    for (const t of themes) {
      themeSelect.appendChild(el("option", { value: t.name, text: t.name + (t.description ? " — " + t.description : "") }));
    }
  } catch (err) {
    themeSelect.innerHTML = "";
    themeSelect.appendChild(el("option", { value: "", text: "主题加载失败" }));
  }

  let lastHtml = null;

  const renderPreviewIframe = async () => {
    iframeHost.innerHTML = "";
    iframeHost.appendChild(loadingState("正在生成预览…"));
    meta.textContent = "";
    copyBtn.disabled = true;
    lastHtml = null;
    try {
      const token = (await api("/api/v1/bootstrap")).csrf_token;
      const res = await fetch("/api/v1/preview", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": token,
        },
        body: JSON.stringify({ run_id: runId, source: sourceSelect.value, theme: themeSelect.value }),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status;
        let message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      const body = await res.json();
      // sandbox 且不含 allow-scripts：iframe 只渲染、不执行脚本
      const iframe = document.createElement("iframe");
      iframe.setAttribute("sandbox", "");
      iframe.className = "preview-frame";
      iframe.srcdoc = body.html;
      iframeHost.innerHTML = "";
      iframeHost.appendChild(iframe);
      meta.textContent = "标题：" + (body.title || "（无）") + "　摘要：" + (body.digest || "（无）") + "　已剥离可执行内容：" + body.filtered_count + " 处";
      copyBtn.disabled = false;
      lastHtml = body.paste_safe_html;
    } catch (err) {
      iframeHost.innerHTML = "";
      if (err.status === 403) iframeHost.appendChild(forbiddenState());
      else if (err.status === 404) iframeHost.appendChild(errorState("产物不存在或尚未生成", () => renderPreviewIframe()));
      else iframeHost.appendChild(errorState("预览失败：" + (err.code || err.message), renderPreviewIframe));
    }
  };

  renderBtn.addEventListener("click", renderPreviewIframe);
  copyBtn.addEventListener("click", async () => {
    if (!lastHtml) return;
    try {
      await navigator.clipboard.writeText(lastHtml);
      copyBtn.textContent = "已复制";
      setTimeout(() => { copyBtn.textContent = "复制正文 HTML"; }, 1500);
    } catch (_) {
      copyBtn.textContent = "复制失败";
    }
  });

  // 初始自动渲染一次
  if (themeSelect.value) await renderPreviewIframe();
}

// ============ M2：P4 每日热榜 ============

const SOURCE_TEXT = { weibo: "微博", baidu: "百度", toutiao: "今日头条" };

async function csrfToken() {
  return (await api("/api/v1/bootstrap")).csrf_token;
}

async function renderHotspots() {
  app.innerHTML = "";
  /* 页面标题由顶栏 h1 承载 */

  const actions = el("div", { class: "preview-controls" });
  const refreshBtn = el("button", { class: "btn", text: "立即刷新（抓取当下）" });
  const persistBtn = el("button", { class: "btn", text: "补抓并保存今日快照" });
  const hint = el("span", { class: "muted", text: "刷新只展示当下数据；保存会写入今日快照" });
  actions.appendChild(refreshBtn);
  actions.appendChild(persistBtn);
  actions.appendChild(hint);
  app.appendChild(actions);

  // P4-F3：历史快照浏览（日期选择器，仅最近 30 天）
  const dateRow = el("div", { class: "preview-controls" });
  const dateSelect = el("select");
  dateSelect.appendChild(el("option", { value: "", text: "加载历史日期…" }));
  const genCandidatesLink = el("a", { class: "btn", href: "#/candidates", text: "生成候选" });
  dateRow.appendChild(el("span", { class: "muted", text: "历史快照（30 天内）" }));
  dateRow.appendChild(dateSelect);
  dateRow.appendChild(genCandidatesLink);
  app.appendChild(dateRow);

  const container = el("div");
  app.appendChild(container);

  const loadDates = async () => {
    try {
      const body = await api("/api/v1/hotspot-snapshots");
      const dates = body.dates || [];
      dateSelect.innerHTML = "";
      dateSelect.appendChild(el("option", { value: "", text: "今日（默认）" }));
      for (const d of dates.slice().reverse()) {
        dateSelect.appendChild(el("option", { value: d, text: d }));
      }
      if (dates.length) dateSelect.value = dates[dates.length - 1];
    } catch (err) {
      dateSelect.innerHTML = "";
      dateSelect.appendChild(el("option", { value: "", text: "历史日期加载失败" }));
    }
  };
  const loadDate = async (date) => {
    container.innerHTML = "";
    container.appendChild(loadingState("正在读取 " + (date || "今日") + " 热榜…"));
    try {
      const body = date ? await api("/api/v1/hotspots/" + date) : await api("/api/v1/hotspots");
      if ((body.items || []).length === 0) { drawEmpty(); } else { drawList(body); }
    } catch (err) {
      container.innerHTML = "";
      if (err.status === 404) container.appendChild(emptyState("该日期没有快照"));
      else if (err.status === 403) container.appendChild(forbiddenState());
      else container.appendChild(errorState("加载失败：" + (err.code || err.message), () => loadDate(date)));
    }
  };
  dateSelect.addEventListener("change", () => loadDate(dateSelect.value));

  const drawEmpty = () => { container.innerHTML = ""; container.appendChild(emptyState("还没有热榜快照，点「补抓并保存今日快照」或「立即刷新」获取数据")); };
  const drawList = (body) => {
    container.innerHTML = "";
    const items = body.items || [];
    const meta = el("div", { class: "muted", text: "共 " + (body.count || 0) + " 条　来源：" + (body.sources || []).map((s) => SOURCE_TEXT[s] || s).join("、") + (body.sources_failed && body.sources_failed.length ? "　失败源：" + body.sources_failed.map((s) => SOURCE_TEXT[s] || s).join("、") : "") + (body.last_success_at ? "　上次成功：" + body.last_success_at : "") });
    container.appendChild(meta);
    if (items.length === 0) {
      container.appendChild(emptyState("暂无热榜条目"));
      return;
    }
    const list = el("ul", { class: "run-list" });
    for (const it of items) {
      const row = el("li", { class: "run-item" }, [
        el("div", { class: "run-main" }, [
          el("div", { class: "run-title", text: (it.title || "") }),
          el("div", { class: "run-meta", text: (it.source_label || it.source || "") + " · 热度 " + (it.hot ?? "—") + (it.hot_normalized != null ? " · 归一化 " + it.hot_normalized : "") }),
        ]),
      ]);
      if (it.url) {
        row.appendChild(el("a", { class: "btn", href: it.url, target: "_blank", rel: "noopener", text: "原文" }));
      }
      list.appendChild(row);
    }
    container.appendChild(list);
  };

  const loadSaved = async () => {
    container.innerHTML = "";
    container.appendChild(loadingState("正在读取热榜快照…"));
    try {
      const body = await api("/api/v1/hotspots");
      if ((body.items || []).length === 0) { drawEmpty(); } else { drawList(body); }
    } catch (err) {
      container.innerHTML = "";
      if (err.status === 403) container.appendChild(forbiddenState());
      else container.appendChild(errorState("加载失败：" + (err.code || err.message), loadSaved));
    }
  };

  const doRefresh = async () => {
    container.innerHTML = "";
    container.appendChild(loadingState("正在抓取当下热榜…"));
    try {
      const body = await api("/api/v1/hotspots?limit=20&refresh=1");
      if ((body.items || []).length === 0 && body.error) {
        container.innerHTML = "";
        container.appendChild(errorState("全源抓取失败：" + body.error, doRefresh));
      } else if ((body.items || []).length === 0) {
        drawEmpty();
      } else {
        drawList(body);
      }
    } catch (err) {
      container.innerHTML = "";
      if (err.status === 403) container.appendChild(forbiddenState());
      else container.appendChild(errorState("抓取失败：" + (err.code || err.message), doRefresh));
    }
  };

  const doPersist = async () => {
    container.innerHTML = "";
    container.appendChild(loadingState("正在抓取并保存今日快照…"));
    try {
      const token = await csrfToken();
      const res = await fetch("/api/v1/hotspots/refresh", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": token,
        },
        body: JSON.stringify({ limit: 20 }),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status; let message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      const body = await res.json();
      if ((body.items || []).length === 0 && body.error) {
        container.innerHTML = "";
        container.appendChild(errorState("全源抓取失败，未写入快照：" + body.error, doPersist));
      } else if ((body.items || []).length === 0) {
        drawEmpty();
      } else {
        drawList(body);
      }
    } catch (err) {
      container.innerHTML = "";
      if (err.status === 403) container.appendChild(forbiddenState());
      else container.appendChild(errorState("保存失败：" + (err.code || err.message), doPersist));
    }
  };

  refreshBtn.addEventListener("click", doRefresh);
  persistBtn.addEventListener("click", doPersist);

  await loadDates();
  await loadSaved();
}

// ============ M3：P5 选题确认（候选对比 + 持久化候选池 + 确认门） ============

const CANDIDATE_STATUS_TEXT = {
  proposed: "待确认",
  confirmed: "已确认",
  retracted: "已撤回",
  expired: "已过期",
};

const SET_STATUS_TEXT = {
  open: "进行中",
  confirmed: "已确认",
  superseded: "已取代",
  deleted: "已删除",
  expired: "已过期",
};

async function renderCandidates() {
  app.innerHTML = "";
  /* 页面标题由顶栏 h1 承载 */

  // 规则生成桩口径：分数来源标注（硬性要求）
  app.appendChild(el("div", { class: "notice" }, [
    "本页候选由规则生成器从当日热榜快照产出（非 AI 评估），相关度/价值分为规则打分，仅作对比参考。",
  ]));

  const actions = el("div", { class: "preview-controls" });
  const dateInput = el("input", { type: "date", value: "", style: "padding:6px 10px;border:1px solid var(--border);border-radius:6px" });
  const genBtn = el("button", { class: "btn", text: "生成候选" });
  const forceBtn = el("button", { class: "btn", text: "强制重开候选集" });
  const compareBtn = el("button", { class: "btn", text: "并排对比（最多 3 条）" });
  const hint = el("span", { class: "muted", text: "相同快照与风格会自动复用既有候选集" });
  actions.appendChild(dateInput);
  actions.appendChild(genBtn);
  actions.appendChild(forceBtn);
  actions.appendChild(compareBtn);
  actions.appendChild(hint);
  app.appendChild(actions);

  const container = el("div");
  app.appendChild(container);

  // P5-F2：勾选最多 3 条候选并排对比
  const selectedIds = new Set();

  // 默认日期取服务端返回的当日快照 date（避免浏览器本地时区与 Asia/Shanghai 不一致）
  async function refreshGenerateDisabled() {
    try {
      const snap = await api("/api/v1/hotspots");
      if (snap.date && !dateInput.value) dateInput.value = snap.date;
      const allFailed = !!(snap.error) || (snap.count || 0) === 0 || !(snap.items || []).length;
      const disabled = allFailed || !snap.date;
      genBtn.disabled = disabled;
      forceBtn.disabled = disabled;
      if (allFailed) {
        hint.textContent = "当日快照全源失败，无可用条目，请先补抓";
        hint.classList.add("error-block");
      } else if (!snap.date) {
        hint.textContent = "暂无热榜快照，请先到「每日热榜」补抓";
        hint.classList.add("error-block");
      } else {
        hint.textContent = "相同快照与风格会自动复用既有候选集";
        hint.classList.remove("error-block");
      }
    } catch (_) {
      genBtn.disabled = true;
      forceBtn.disabled = true;
      hint.textContent = "读取热榜快照失败，无法生成候选";
    }
  }

  const load = async () => {
    container.innerHTML = "";
    container.appendChild(loadingState("正在加载候选集…"));
    try {
      const body = await api("/api/v1/candidates");
      const sets = body.candidates || [];
      if (sets.length === 0) {
        container.appendChild(emptyState("还没有候选集，点「生成候选」从当日热榜快照生成"));
        return;
      }
      const latest = sets[0];
      drawSet(latest);
    } catch (err) {
      container.innerHTML = "";
      if (err.status === 403) container.appendChild(forbiddenState());
      else container.appendChild(errorState("加载失败：" + (err.code || err.message), load));
    }
  };

  const drawSet = (set) => {
    container.innerHTML = "";
    const setMeta = el("div", { class: "muted", text: "候选集 " + set.candidate_set_id + " · 状态 " + (SET_STATUS_TEXT[set.set_status] || set.set_status) + " · 创建 " + (set.created_at || "—") + " · 过期 " + (set.expires_at || "—") });
    container.appendChild(setMeta);

    const items = (set.items || []).slice().sort((a, b) => (b.scores.total - a.scores.total));
    const grid = el("div", { class: "candidate-grid" });
    for (const it of items) {
      grid.appendChild(candidateCard(set, it));
    }
    container.appendChild(grid);
  };

  const candidateCard = (set, it) => {
    const badges = el("div", { class: "artifact-badges" }, [
      el("span", { class: "badge " + badgeClass(it.status), text: CANDIDATE_STATUS_TEXT[it.status] || it.status }),
    ]);
    // P5-F2 勾选对比（最多 3 条）
    const compareCb = el("input", { type: "checkbox", title: "加入对比（最多 3 条）" });
    compareCb.checked = selectedIds.has(it.id);
    compareCb.addEventListener("change", () => {
      if (compareCb.checked) {
        if (selectedIds.size >= 3) {
          compareCb.checked = false;
          window.alert("最多并排对比 3 条候选");
          return;
        }
        selectedIds.add(it.id);
      } else {
        selectedIds.delete(it.id);
      }
    });
    const card = el("div", { class: "card candidate-card" }, [
      el("div", { class: "run-title", text: it.title }),
      el("div", { class: "muted", text: "角度：" + (it.angle || "—") }),
      el("div", { class: "muted", text: "框架：" + (it.framework || "—") + " · 来源：" + (it.source || "—") }),
      el("div", { class: "scores" }, [
        scoreLine("总分", it.scores.total),
        scoreLine("热度", it.scores.hot_normalized),
        scoreLine("相关度", it.scores.relevance),
        scoreLine("价值", it.scores.value),
      ]),
      el("div", { class: "muted", text: "评分来源：规则生成（非 AI 评估）" }),
      badges,
      el("label", { class: "muted", text: "" }, [compareCb, " 加入对比"]),
    ]);
    card.dataset.candidateId = it.id;

    const btnRow = el("div", { class: "preview-controls" });
    if (it.status === "proposed" && set.set_status === "open") {
      const confirmBtn = el("button", { class: "btn", text: "确认选题", onclick: () => openConfirm(set, it) });
      btnRow.appendChild(confirmBtn);
    }
    if (it.status === "confirmed") {
      const retractBtn = el("button", { class: "btn", text: "撤回", onclick: () => doRetract(set, it) });
      btnRow.appendChild(retractBtn);
    }
    card.appendChild(btnRow);
    return card;
  };

  const scoreLine = (label, v) => el("div", { class: "muted", text: label + "：" + (v == null ? "—" : v) });

  const badgeClass = (status) => {
    if (status === "confirmed") return "badge-completed";
    if (status === "retracted") return "badge-failed";
    if (status === "expired") return "badge-failed";
    return "badge-active";
  };

  const openConfirm = (set, it) => {
    const modal = el("div", { class: "modal-backdrop" }, [
      el("div", { class: "modal card" }, [
        el("h3", { text: "确认选题" }),
        el("div", { class: "muted", text: "拟定标题：" + it.title }),
        el("label", { text: "图片上限（0–10）" }),
        el("input", { type: "number", min: "0", max: "10", value: "4", id: "max-images" }),
        el("label", { text: "预算（元）" }),
        el("input", { type: "number", min: "0", step: "0.01", value: "10", id: "max-cost" }),
        el("div", { class: "muted", text: "mode=draft · visual_mode=full · allow_publish=false（固定，发布前移 MVP 另走 P7 预检）" }),
        el("div", { class: "preview-controls" }, [
          el("button", { class: "btn", text: "确认生成", onclick: () => doConfirm(set, it, modal) }),
          el("button", { class: "btn", text: "取消", onclick: () => { modal.remove(); } }),
        ]),
        el("div", { class: "muted", id: "confirm-result", text: "" }),
      ]),
    ]);
    app.appendChild(modal);
  };

  const doConfirm = async (set, it, modal) => {
    const maxImages = parseInt(document.getElementById("max-images").value, 10) || 0;
    const maxCost = parseFloat(document.getElementById("max-cost").value) || 0;
    const key = "web-" + (crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(16).slice(2));
    const resultEl = document.getElementById("confirm-result");
    try {
      const token = await csrfToken();
      const res = await fetch(`/api/v1/candidates/${set.candidate_set_id}/confirm`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": token,
          "Idempotency-Key": key,
        },
        body: JSON.stringify({
          candidate_id: it.id,
          content_hash: it.content_hash,
          max_images: maxImages,
          max_image_cost: maxCost,
          mode: "draft",
          visual_mode: "full",
        }),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status; let message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      const body = await res.json();
      resultEl.textContent = "已确认：run_id=" + body.run_id + " job_id=" + body.job_id;
      setTimeout(() => load(), 600);
    } catch (err) {
      resultEl.textContent = "确认失败：" + (err.code || err.message);
      resultEl.classList.add("error-block");
    }
  };

  const doRetract = async (set, it) => {
    if (!window.confirm("确认撤回该选题？run 会保留，仅标记为已撤回。")) return;
    try {
      const token = await csrfToken();
      const res = await fetch(`/api/v1/candidates/${set.candidate_set_id}/retract`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ candidate_id: it.id }),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status; let message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      await load();
    } catch (err) {
      window.alert("撤回失败：" + (err.code || err.message));
    }
  };

  const doGenerate = async (force) => {
    container.innerHTML = "";
    container.appendChild(loadingState("正在生成候选…"));
    try {
      const token = await csrfToken();
      const res = await fetch("/api/v1/candidates", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ date: dateInput.value, style_revision: "sha256:web-default", force }),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status; let message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      const body = await res.json();
      selectedIds.clear();
      drawSet(body);
    } catch (err) {
      container.innerHTML = "";
      container.appendChild(errorState("生成失败：" + (err.code || err.message), () => doGenerate(force)));
    }
  };

  const doCompare = () => {
    if (selectedIds.size === 0) {
      window.alert("请先勾选 1–3 条候选进行对比");
      return;
    }
    if (selectedIds.size > 3) {
      window.alert("最多并排对比 3 条候选");
      return;
    }
    // 从当前候选集中取勾选项
    let picked = [];
    const cards = container.querySelectorAll(".candidate-card");
    for (const card of cards) {
      if (selectedIds.has(card.dataset.candidateId)) picked.push(card);
    }
    if (picked.length === 0) {
      window.alert("未找到已勾选的候选，请重新勾选");
      return;
    }
    const modal = el("div", { class: "modal-backdrop" }, [
      el("div", { class: "modal card", style: "width:min(920px,95vw)" }, [
        el("h3", { text: "候选对比（" + picked.length + " 条）" }),
        el("div", { class: "compare-grid" }, picked.map((card) => {
          const clone = card.cloneNode(true);
          // 对比视图里去掉按钮行与勾选框，避免误操作
          clone.querySelectorAll("button, input").forEach((n) => n.remove());
          return clone;
        })),
        el("button", { class: "btn", text: "关闭", onclick: () => { modal.remove(); } }),
      ]),
    ]);
    app.appendChild(modal);
  };

  genBtn.addEventListener("click", () => doGenerate(false));
  forceBtn.addEventListener("click", () => doGenerate(true));
  compareBtn.addEventListener("click", doCompare);

  await refreshGenerateDisabled();
  await load();
}

// ============ M4：P8 配置 + P7 发布闸门（页面） ============

async function renderConfig() {
  app.innerHTML = "";
  /* 页面标题由顶栏 h1 承载 */

  const container = el("div");
  app.appendChild(container);
  container.appendChild(loadingState("正在加载配置…"));

  let cfg, style;
  try {
    cfg = await api("/api/v1/config");
    style = await api("/api/v1/style");
  } catch (err) {
    container.innerHTML = "";
    container.appendChild(errorState("加载配置失败：" + (err.code || err.message), renderConfig));
    return;
  }

  container.innerHTML = "";

  // 图片供应商元数据（GET /api/v1/image-providers，动态读 wewrite PROVIDERS）
  let providerList = [];
  try {
    providerList = (await api("/api/v1/image-providers")).providers || [];
  } catch (_) {
    providerList = [];
  }
  // 主题清单（GET /api/v1/themes）
  let themeList = [];
  try {
    themeList = (await api("/api/v1/themes")).themes || [];
  } catch (_) {
    themeList = [];
  }

  // 公众号
  container.appendChild(el("div", { class: "section-title", text: "公众号（secret 只写不读）" }));
  const appidInput = el("input", { type: "text", value: cfg.wechat.appid || "", placeholder: "AppID" });
  const authorInput = el("input", { type: "text", value: cfg.wechat.author || "", placeholder: "作者" });
  const secretInput = el("input", { type: "password", value: "", placeholder: cfg.wechat.secret_configured ? "已配置（留空保持）" : "AppSecret" });
  container.appendChild(kvForm([["AppID", appidInput], ["作者", authorInput], ["AppSecret", secretInput]]));

  // 图片供应商
  container.appendChild(el("div", { class: "section-title", text: "图片供应商与 fallback 链（provider 下拉选择，model/base_url 联动填充）" }));
  const envManaged = new Set((cfg.env_managed || []).map((m) => m.field));
  const imageSection = el("div");

  // provider 下拉：内置 provider + 「自定义…」
  const providerSelect = el("select");
  providerSelect.appendChild(el("option", { value: "__custom__", text: "自定义…" }));
  for (const p of providerList) {
    providerSelect.appendChild(el("option", { value: p.name, text: p.name }));
  }
  const providerExtraFields = {}; // name -> extra_fields 提示
  const providerDefaults = {}; // name -> {model, base_url}
  for (const p of providerList) {
    providerDefaults[p.name] = { model: p.default_model || "", base_url: p.default_base_url || "" };
    providerExtraFields[p.name] = p.extra_fields || [];
  }
  const currentProvider = cfg.image.provider || "";
  if (currentProvider && !(currentProvider in providerDefaults)) {
    providerSelect.appendChild(el("option", { value: currentProvider, text: currentProvider + "（当前值）" }));
  }
  providerSelect.value = currentProvider || "__custom__";

  const providerModelInput = el("input", { type: "text", value: cfg.image.model || "", placeholder: "model" });
  const providerBaseUrlInput = el("input", { type: "text", value: cfg.image.base_url || "", placeholder: "base_url" });
  const apiKeyInput = el("input", {
    type: "password",
    value: "",
    placeholder: cfg.image.api_key_configured ? "已配置（留空保持）" : "api_key",
  });
  // 自定义 provider 名：选「自定义…」时出现，可自由填写不在内置清单里的名字
  const customProviderInput = el("input", {
    type: "text",
    value: "",
    placeholder: "自定义 provider 名称",
    style: "display:none;padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:14px;width:100%",
  });

  const extraHint = el("div", { class: "muted", text: "" });
  const updateExtraHint = () => {
    const fields = providerExtraFields[providerSelect.value] || [];
    if (fields.length) {
      extraHint.textContent = "该 provider 还需要额外参数：" + fields.join("、") + "（当前配置页暂不支持填写，仅提示）";
    } else {
      extraHint.textContent = "";
    }
  };
  const refreshCustomInput = () => {
    const isCustom = providerSelect.value === "__custom__";
    customProviderInput.style.display = isCustom ? "" : "none";
  };

  // 选中内置 provider 时联动填充默认 model/base_url；自定义/当前值不覆盖
  const applyProviderDefaults = () => {
    const val = providerSelect.value;
    const def = providerDefaults[val];
    if (def) {
      providerModelInput.value = def.model;
      providerBaseUrlInput.value = def.base_url;
    } else if (val === "__custom__") {
      providerModelInput.value = "";
      providerBaseUrlInput.value = "";
    }
    updateExtraHint();
    refreshCustomInput();
  };
  providerSelect.addEventListener("change", applyProviderDefaults);

  const providerRow = el("div");
  providerRow.appendChild(kvForm([["provider", providerSelect], ["model", providerModelInput], ["base_url", providerBaseUrlInput], ["api_key", apiKeyInput]]));
  providerRow.appendChild(customProviderInput);
  providerRow.appendChild(extraHint);
  imageSection.appendChild(providerRow);
  container.appendChild(imageSection);

  // 写作模型（文章生成）—— 与图片供应商同一套交互（provider 联动/自定义/api_key 只写不读/env 优先）
  container.appendChild(el("div", { class: "section-title", text: "写作模型（文章生成）" }));
  // 未配置 api_key 时一眼看到后果（WRITER_NOT_CONFIGURED）
  if (!cfg.writer.api_key_configured) {
    container.appendChild(el("div", { class: "notice", text: "未配置写作模型，文章生成会失败（WRITER_NOT_CONFIGURED）" }));
  }

  let writerProviderList = [];
  try {
    writerProviderList = (await api("/api/v1/writer-providers")).providers || [];
  } catch (_) {
    writerProviderList = [];
  }

  const writerSection = el("div");
  const writerProviderSelect = el("select");
  writerProviderSelect.appendChild(el("option", { value: "__custom__", text: "自定义…" }));
  const writerDefaults = {}; // name -> {model, base_url}
  for (const p of writerProviderList) {
    writerProviderSelect.appendChild(el("option", { value: p.name, text: p.name }));
    writerDefaults[p.name] = { model: p.default_model || "", base_url: p.default_base_url || "" };
  }
  const writerCurrentProvider = cfg.writer.provider || "";
  if (writerCurrentProvider && !(writerCurrentProvider in writerDefaults)) {
    writerProviderSelect.appendChild(el("option", { value: writerCurrentProvider, text: writerCurrentProvider + "（当前值）" }));
  }
  writerProviderSelect.value = writerCurrentProvider || "__custom__";

  const writerModelInput = el("input", { type: "text", value: cfg.writer.model || "", placeholder: "模型名称" });
  const writerBaseUrlInput = el("input", { type: "text", value: cfg.writer.base_url || "", placeholder: "接口地址" });
  const writerApiKeyInput = el("input", {
    type: "password",
    value: "",
    placeholder: cfg.writer.api_key_configured ? "已配置（留空保持）" : "API 密钥",
  });
  const writerTemperatureInput = el("input", {
    type: "number", step: "0.1", min: "0", max: "2",
    value: cfg.writer.temperature == null ? "1.0" : String(cfg.writer.temperature),
    placeholder: "温度（0–2）",
  });
  const writerMaxTokensInput = el("input", {
    type: "number", step: "1", min: "1",
    value: cfg.writer.max_tokens == null ? "4000" : String(cfg.writer.max_tokens),
    placeholder: "最大 token 数",
  });
  const writerCustomInput = el("input", {
    type: "text",
    value: "",
    placeholder: "自定义写作模型 provider 名称",
    style: "display:none;padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:14px;width:100%",
  });

  // 选中内置 provider 联动填充默认 model/base_url；自定义清空供自由填写
  const applyWriterDefaults = () => {
    const val = writerProviderSelect.value;
    const def = writerDefaults[val];
    if (def) {
      writerModelInput.value = def.model;
      writerBaseUrlInput.value = def.base_url;
    } else if (val === "__custom__") {
      writerModelInput.value = "";
      writerBaseUrlInput.value = "";
    }
    writerCustomInput.style.display = val === "__custom__" ? "" : "none";
  };
  writerProviderSelect.addEventListener("change", applyWriterDefaults);

  const writerRow = el("div");
  writerRow.appendChild(kvForm([
    ["模型提供商", writerProviderSelect],
    ["模型名称", writerModelInput],
    ["接口地址", writerBaseUrlInput],
    ["API 密钥", writerApiKeyInput],
    ["温度（0–2）", writerTemperatureInput],
    ["最大 token 数", writerMaxTokensInput],
  ]));
  writerRow.appendChild(writerCustomInput);
  writerSection.appendChild(writerRow);
  container.appendChild(writerSection);

  // 主题
  container.appendChild(el("div", { class: "section-title", text: "排版主题" }));
  const themeSelect = el("select");
  const currentTheme = cfg.theme || "";
  if (currentTheme && !themeList.some((t) => t.name === currentTheme)) {
    themeSelect.appendChild(el("option", { value: currentTheme, text: currentTheme + "（当前值）" }));
  }
  for (const t of themeList) {
    themeSelect.appendChild(el("option", { value: t.name, text: t.name + (t.description ? " — " + t.description : "") }));
  }
  themeSelect.value = currentTheme || (themeList[0] && themeList[0].name) || "";
  container.appendChild(kvForm([["theme", themeSelect]]));

  // 时区展示
  container.appendChild(el("div", { class: "muted", text: "时区：Asia/Shanghai（UTC+8）" }));

  // env_managed 提示与控件禁用（env 接管优先）
  if (cfg.env_managed && cfg.env_managed.length) {
    const managed = cfg.env_managed.map((m) => m.field + " ← " + m.env).join("、");
    container.appendChild(el("div", { class: "notice", text: "以下字段由环境变量接管，写入将返回 409 ENV_MANAGED：" + managed }));
  }
  if (envManaged.has("image.provider")) {
    providerSelect.disabled = true;
    providerSelect.title = "该字段由环境变量接管，禁止修改";
  }
  if (envManaged.has("image.model")) providerModelInput.disabled = true;
  if (envManaged.has("image.base_url")) providerBaseUrlInput.disabled = true;
  if (envManaged.has("theme")) themeSelect.disabled = true;
  if (envManaged.has("image.api_key")) apiKeyInput.disabled = true;

  // 写作模型 env 接管禁用（与图片供应商一致）
  if (envManaged.has("writer.provider")) {
    writerProviderSelect.disabled = true;
    writerProviderSelect.title = "该字段由环境变量接管，禁止修改";
  }
  if (envManaged.has("writer.model")) writerModelInput.disabled = true;
  if (envManaged.has("writer.base_url")) writerBaseUrlInput.disabled = true;
  if (envManaged.has("writer.api_key")) writerApiKeyInput.disabled = true;
  if (envManaged.has("writer.temperature")) writerTemperatureInput.disabled = true;
  if (envManaged.has("writer.max_tokens")) writerMaxTokensInput.disabled = true;

  const saveBtn = el("button", { class: "btn", text: "保存配置" });
  const result = el("div", { class: "muted", text: "" });
  container.appendChild(el("div", { class: "preview-controls" }, [saveBtn, result]));

  saveBtn.addEventListener("click", async () => {
    const isCustom = providerSelect.value === "__custom__";
    // 图片供应商：env 接管的字段不发（避免破坏 409 ENV_MANAGED 语义）。
    // 内置 provider 的 model/base_url 以输入框当前值为准提交（含空串，如
    // azure_openai 默认 base_url 为空串时不得被 `|| undefined` 吞掉）；自定义
    // provider 的 model/base_url 空值保持原值（与改造前一致）。
    const image = {};
    if (!envManaged.has("image.provider")) {
      if (isCustom) {
        const customVal = customProviderInput.value.trim();
        if (customVal) image.provider = customVal;
      } else {
        image.provider = providerSelect.value;
      }
    }
    if (!envManaged.has("image.model")) {
      image.model = isCustom ? (providerModelInput.value || undefined) : providerModelInput.value;
    }
    if (!envManaged.has("image.base_url")) {
      image.base_url = isCustom ? (providerBaseUrlInput.value || undefined) : providerBaseUrlInput.value;
    }
    const wechat = { appid: appidInput.value || undefined, author: authorInput.value || undefined };
    // 写作模型：env 接管的字段不发；内置 provider 的 model/base_url 以输入框当前值
    // 为准提交，自定义 provider 的空值保持原值（与图片供应商一致）。
    const writerIsCustom = writerProviderSelect.value === "__custom__";
    const writer = {};
    if (!envManaged.has("writer.provider")) {
      if (writerIsCustom) {
        const customVal = writerCustomInput.value.trim();
        if (customVal) writer.provider = customVal;
      } else if (writerProviderSelect.value) {
        writer.provider = writerProviderSelect.value;
      }
    }
    if (!envManaged.has("writer.model")) {
      writer.model = writerIsCustom ? (writerModelInput.value || undefined) : writerModelInput.value;
    }
    if (!envManaged.has("writer.base_url")) {
      writer.base_url = writerIsCustom ? (writerBaseUrlInput.value || undefined) : writerBaseUrlInput.value;
    }
    // 提交前校验：temperature 为 0–2 的有限数字，max_tokens 为正整数。
    // 不合法就地提示并中止，不发起请求；禁止 parseInt 静默截断小数。
    let tempValue;
    if (!envManaged.has("writer.temperature") && writerTemperatureInput.value !== "") {
      tempValue = Number(writerTemperatureInput.value);
      if (!Number.isFinite(tempValue) || tempValue < 0 || tempValue > 2) {
        result.textContent = "保存失败：温度（0–2）必须是 0 到 2 之间的数字";
        result.classList.add("error-block");
        return;
      }
      writer.temperature = tempValue;
    }
    if (!envManaged.has("writer.max_tokens") && writerMaxTokensInput.value !== "") {
      const rawTokens = writerMaxTokensInput.value.trim();
      const tokenNum = Number(rawTokens);
      if (!Number.isFinite(tokenNum) || tokenNum <= 0 || !Number.isInteger(tokenNum)) {
        result.textContent = "保存失败：最大 token 数必须为正整数";
        result.classList.add("error-block");
        return;
      }
      writer.max_tokens = tokenNum;
    }
    const body = {
      theme: themeSelect.value || undefined,
      image,
      wechat,
      writer,
    };
    if (apiKeyInput.value) image.api_key = apiKeyInput.value;
    if (secretInput.value) wechat.secret = secretInput.value;
    if (!envManaged.has("writer.api_key") && writerApiKeyInput.value) writer.api_key = writerApiKeyInput.value;
    try {
      const token = await csrfToken();
      const res = await fetch("/api/v1/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status, message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      result.textContent = "已保存";
      result.classList.remove("error-block");
      setTimeout(renderConfig, 400);
    } catch (err) {
      result.textContent = "保存失败：" + (err.code || err.message);
      result.classList.add("error-block");
    }
  });

  // 初始化联动提示（若当前值命中内置 provider，不覆盖已存值，仅提示）
  updateExtraHint();

  // ---------- P8-F3 写作风格（整文件 YAML 编辑 + AI 引导，JEE-1947） ----------
  container.appendChild(el("div", { class: "section-title", text: "写作风格（style.yaml）" }));
  container.appendChild(el("div", { class: "muted", text: "直接编辑下方 style.yaml 全文，保存后下次写作自动生效；写作主题与上方「排版主题」相互独立。" }));

  // 写作模型是否已配置（env WEWRITE_WRITER_API_KEY 是否存在）
  const writerConfigured = !!(cfg.writer && cfg.writer.writer_configured);

  const yamlEditor = el("textarea", {
    class: "style-yaml-editor",
    spellcheck: "false",
    placeholder: "name: ...\nindustry: ...",
  });
  yamlEditor.value = (style && style.raw) ? style.raw : "";
  container.appendChild(yamlEditor);

  // 记录已保存的原文，用于未保存变更提示
  let savedRaw = yamlEditor.value;

  const validateBox = el("div", { class: "style-validate-box", style: "display:none;" });
  container.appendChild(validateBox);

  function renderValidateResult(data) {
    validateBox.style.display = "";
    validateBox.innerHTML = "";
    if (data.valid) {
      validateBox.classList.remove("error-block");
      validateBox.classList.add("muted");
      if (data.warnings && data.warnings.length) {
        validateBox.appendChild(el("div", { text: "校验通过；忽略的未知字段：" + data.warnings.join("、") }));
      } else {
        validateBox.appendChild(el("div", { text: "校验通过" }));
      }
    } else {
      validateBox.classList.add("error-block");
      validateBox.classList.remove("muted");
      for (const e of data.errors || []) {
        const lineText = e.line ? "第 " + e.line + " 行：" : "";
        validateBox.appendChild(el("div", { text: lineText + (e.field ? e.field + "：" : "") + (e.message || "") }));
      }
    }
  }

  function renderWarnings(data) {
    // 保存路径的 warnings 可见化（不静默丢弃）
    if (data && data.warnings && data.warnings.length) {
      styleResult.textContent = "已保存；忽略的未知字段：" + data.warnings.join("、");
      styleResult.classList.add("muted");
    }
  }

  // 四个操作：加载当前配置 / 智能生成 / 校验 / 保存
  const styleLoadBtn = el("button", { class: "btn", text: "加载当前配置" });
  const styleGenBtn = el("button", { class: "btn", text: "智能生成" });
  const styleValidateBtn = el("button", { class: "btn", text: "校验" });
  const styleSaveBtn = el("button", { class: "btn", text: "保存" });
  const styleResult = el("div", { class: "muted", text: "" });

  // AI 引导区（默认折叠）
  const aiBox = el("div", { class: "ai-generate-box", style: "display:none;" });
  const aiIndustry = el("input", { class: "style-custom-input", type: "text", placeholder: "做什么行业/领域" });
  const aiAudience = el("input", { class: "style-custom-input", type: "text", placeholder: "写给谁看" });
  const aiTone = el("input", { class: "style-custom-input", type: "text", placeholder: "希望什么调性（一句话）" });
  aiBox.appendChild(kvForm([["行业/领域", aiIndustry], ["目标读者", aiAudience], ["调性", aiTone]]));
  const aiGoBtn = el("button", { class: "btn", text: "生成" });
  const aiResult = el("div", { class: "ai-generate-hint", text: "" });
  if (!writerConfigured) {
    aiGoBtn.disabled = true;
    aiResult.textContent = "需先配置写作模型（见「写作模型」区，WEWRITE_WRITER_API_KEY）";
  }
  aiBox.appendChild(el("div", { class: "preview-controls" }, [aiGoBtn, aiResult]));
  container.appendChild(aiBox);

  container.appendChild(el("div", { class: "preview-controls" }, [styleLoadBtn, styleGenBtn, styleValidateBtn, styleSaveBtn, styleResult]));

  // 未保存变更提示：改动后刷新/离开给一次确认
  function markDirty(dirty) {
    window.__styleDirty = dirty;
  }
  yamlEditor.addEventListener("input", () => { markDirty(yamlEditor.value !== savedRaw); });

  // 加载当前配置
  styleLoadBtn.addEventListener("click", async () => {
    try {
      const s = await api("/api/v1/style");
      yamlEditor.value = s.raw || "";
      savedRaw = yamlEditor.value;
      markDirty(false);
      styleResult.textContent = "已加载";
      styleResult.classList.remove("error-block");
    } catch (err) {
      styleResult.textContent = "加载失败：" + (err.code || err.message);
      styleResult.classList.add("error-block");
    }
  });

  // 智能生成：展开引导区
  styleGenBtn.addEventListener("click", () => {
    aiBox.style.display = aiBox.style.display === "none" ? "" : "none";
  });

  aiGoBtn.addEventListener("click", async () => {
    const industry = aiIndustry.value.trim();
    const audience = aiAudience.value.trim();
    const toneHint = aiTone.value.trim();
    if (!industry && !audience && !toneHint) {
      aiResult.textContent = "请至少填写一项引导信息（行业/受众/调性）";
      aiResult.classList.add("error-block");
      return;
    }
    aiGoBtn.disabled = true;
    aiResult.classList.remove("error-block");
    aiResult.textContent = "正在生成…";
    try {
      const token = await csrfToken();
      const res = await fetch("/api/v1/style/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ industry, audience, tone_hint: toneHint }),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status, message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      const data = await res.json();
      yamlEditor.value = data.yaml || "";
      markDirty(yamlEditor.value !== savedRaw);
      aiResult.textContent = "已生成并填入编辑器，请预览修改后点「保存」落盘（未自动保存）";
    } catch (err) {
      aiResult.textContent = "生成失败：" + (err.code || err.message);
      aiResult.classList.add("error-block");
    } finally {
      aiGoBtn.disabled = !writerConfigured;
    }
  });

  // 校验
  styleValidateBtn.addEventListener("click", async () => {
    styleResult.textContent = "";
    try {
      const token = await csrfToken();
      const res = await fetch("/api/v1/style/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ yaml: yamlEditor.value }),
      });
      let data = null;
      try { data = await res.json(); } catch (_) {}
      if (!res.ok) {
        const code = data && data.code ? data.code : "HTTP_" + res.status;
        const message = data && data.message ? data.message : res.statusText;
        // 422 解析错误：展示行号 + 中文原因
        renderValidateResult({ valid: false, errors: [{ field: data && data.details && data.details.field, message: (data && data.details && data.details.line ? "第 " + data.details.line + " 行：" : "") + message }] });
        styleResult.textContent = "校验失败：" + (code || message);
        styleResult.classList.add("error-block");
        return;
      }
      renderValidateResult(data);
      styleResult.textContent = data.valid ? "校验通过" : "校验未通过";
      styleResult.classList.remove("error-block");
      if (!data.valid) styleResult.classList.add("error-block");
    } catch (err) {
      styleResult.textContent = "校验失败：" + (err.code || err.message);
      styleResult.classList.add("error-block");
    }
  });

  // 保存（提交 yaml 全文）
  styleSaveBtn.addEventListener("click", async () => {
    styleResult.textContent = "";
    try {
      const token = await csrfToken();
      const res = await fetch("/api/v1/style", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ yaml: yamlEditor.value }),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status, message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      const saved = await res.json();
      savedRaw = yamlEditor.value;
      markDirty(false);
      styleResult.textContent = "已保存";
      styleResult.classList.remove("error-block");
      styleResult.classList.remove("muted");
      renderWarnings(saved);
      setTimeout(renderConfig, 400);
    } catch (err) {
      styleResult.textContent = "保存失败：" + (err.code || err.message);
      styleResult.classList.add("error-block");
    }
  });
}

function kvForm(rows) {
  const dl = el("dl", { class: "kv" });
  for (const [label, input] of rows) {
    dl.appendChild(el("dt", { text: label }));
    // 仅对真实表单控件套内联样式；包装容器（select+自定义、标签输入、字数档位）由 CSS class 负责
    if (input.tagName === "INPUT" || input.tagName === "SELECT" || input.tagName === "TEXTAREA") {
      input.style.cssText = "padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:14px;width:100%";
    }
    dl.appendChild(el("dd", {}, [input]));
  }
  return dl;
}

// 发布确认页面（#/publish 无 ID 入口或 #/runs/{run_id}/publish 有 ID 入口）
async function renderPublish(runId) {
  app.innerHTML = "";

  // 无 ID 入口：解析最近一次运行；无权限/请求失败/无运行分别渲染对应状态页
  if (!runId) {
    const resolved = await resolveLatestRunState();
    if (resolved.denied) {
      app.innerHTML = "";
      app.appendChild(forbiddenState());
      return;
    }
    if (resolved.error) {
      app.innerHTML = "";
      app.appendChild(errorState("加载任务列表失败：" + (resolved.error.code || resolved.error.message || "网络错误"), () => renderPublish(null)));
      return;
    }
    if (!resolved.runId) {
      app.appendChild(el("a", { class: "back-link", href: "#/", text: "← 返回任务列表" }));
      app.appendChild(el("div", { class: "card" }, [
        el("div", { class: "card-head" }, [el("h2", { text: "发布确认" })]),
        el("div", { class: "card-body" }, [emptyState("还没有任何任务，暂无发布确认内容")]),
      ]));
      return;
    }
    location.replace("#/runs/" + resolved.runId + "/publish");
    return;
  }

  app.appendChild(el("a", { class: "back-link", href: "#/runs/" + runId, text: "← 返回任务详情" }));

  const container = el("div");
  app.appendChild(container);
  container.appendChild(loadingState("正在加载发布确认…"));

  let detail;
  try {
    detail = await api("/api/v1/runs/" + runId);
  } catch (err) {
    container.innerHTML = "";
    if (err.status === 403) container.appendChild(forbiddenState());
    else if (err.status === 404) container.appendChild(errorState("任务不存在或已被移除", () => renderHome()));
    else container.appendChild(errorState("加载失败：" + (err.code || err.message), () => renderPublish(runId)));
    return;
  }

  container.innerHTML = "";

  // 题名卡片
  container.appendChild(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [
      el("h2", { text: detail.title || "未命名文章" }),
      el("span", { class: "hint" }, [detail.run_id]),
    ]),
  ]));

  // 发布闸门（含发布预检 / 权限闸 / 提交动作）——发布页调用方：操作成功后留在发布页
  container.appendChild(renderPublishGate(runId, detail, () => renderPublish(runId)));
}

function renderPublishGate(runId, detail, onDone) {
  // onDone 由调用方决定成功后刷新哪个页面：详情页传 () => renderDetail(runId)、
  // 发布页传 () => renderPublish(runId)；缺省回退 renderPublish(runId) 保证向后兼容。
  const refresh = typeof onDone === "function" ? onDone : () => renderPublish(runId);
  const section = el("div", { class: "card" });
  section.appendChild(el("div", { class: "card-head" }, [el("h2", { text: "发布闸门（P7）" })]));
  const body = el("div", { class: "card-body" });
  section.appendChild(body);

  const perm = !!(detail.permissions && detail.permissions.publish);
  const statusLine = el("div", { class: "muted", text: "发布权限：" + (perm ? "已开启" : "未开启") });
  body.appendChild(statusLine);

  const authorizeBtn = el("button", { class: "btn", text: perm ? "撤销发布授权" : "开启发布授权" });
  const preflightBox = el("div", { class: "muted", text: "尚未预检" });
  const submitBox = el("div");
  // P7-F4：手动「核对草稿箱」（仅只读 get_draft，不自动轮询、不重发）
  const reconcileBtn = el("button", { class: "btn", text: "核对草稿箱" });
  const reconcileBox = el("div", { class: "muted" });
  body.appendChild(el("div", { class: "preview-controls" }, [authorizeBtn, reconcileBtn]));
  body.appendChild(preflightBox);
  body.appendChild(submitBox);
  body.appendChild(reconcileBox);

  reconcileBtn.addEventListener("click", async () => {
    const mediaId = window.prompt("请输入要核对的 media_id（留空则用任务已记录的 media_id）", (detail.publish && detail.publish.media_id) || "");
    if (mediaId === null) return; // 用户取消
    try {
      const token = await csrfToken();
      const body = mediaId.trim() ? { media_id: mediaId.trim() } : {};
      const res = await fetch(`/api/v1/runs/${runId}/publish/reconcile`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status, message = res.statusText;
        try { const b = await res.json(); code = b.code || code; message = b.message || message; } catch (_) {}
        throw Object.assign(new Error(message), { code, status: res.status });
      }
      const rb = await res.json();
      if (rb.status === "manual_review") {
        reconcileBox.textContent = rb.note;
      } else {
        reconcileBox.textContent = "核对结果：" + (rb.consistent ? "文本一致" : "文本不一致") + "（" + rb.note + "）";
      }
    } catch (err) {
      reconcileBox.textContent = "核对失败：" + (err.code || err.message);
      reconcileBox.classList.add("error-block");
    }
  });

  authorizeBtn.addEventListener("click", async () => {
    try {
      const token = await csrfToken();
      const res = await fetch(`/api/v1/runs/${runId}/publish/permission`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ allow: !perm }),
      });
      if (!res.ok) throw new Error((await res.json()).message || res.statusText);
      // 授权/撤销成功后由调用方 onDone 决定刷新哪个页面，使地址、顶栏 h1、
      // 侧栏高亮、正文四者指向同一页面（详情页留详情页，发布页留发布页）。
      refresh();
    } catch (err) {
      window.alert("授权失败：" + (err.code || err.message));
    }
  });

  const runPreflight = async () => {
    try {
      const body = await api(`/api/v1/runs/${runId}/publish/preflight`);
      const items = (body.checks || []).map((c) => el("div", { class: "muted", text: (c.ok ? "✅" : "❌") + " " + c.name + (c.message ? "：" + c.message : "") }));
      preflightBox.innerHTML = "";
      preflightBox.appendChild(el("div", { text: "预检：" + (body.ok ? "通过" : "未通过") }));
      for (const it of items) preflightBox.appendChild(it);
      preflightBox.dataset.revision = body.revision;
      preflightBox.dataset.ok = body.ok ? "1" : "0";
      return body;
    } catch (err) {
      preflightBox.textContent = "预检失败：" + (err.code || err.message);
      return null;
    }
  };

  runPreflight();

  const publishBtn = el("button", { class: "btn", text: "获取一次性凭据并写入草稿箱" });
  publishBtn.disabled = true;
  submitBox.appendChild(publishBtn);

  publishBtn.addEventListener("click", async () => {
    if (!window.confirm("我确认写入公众号草稿箱")) return;
    const title = window.prompt("请再次输入文章标题（逐字匹配）", detail.title || "");
    if (!title) return;
    try {
      const revision = preflightBox.dataset.revision;
      const token = await csrfToken();
      // 1) 取一次性 token
      const tr = await fetch(`/api/v1/runs/${runId}/publish/token`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ revision, checked: true, title }),
      });
      if (!tr.ok) {
        let code = "HTTP_" + tr.status, message = tr.statusText;
        try { const d = await tr.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        throw Object.assign(new Error(message), { code, status: tr.status });
      }
      const tbody = await tr.json();
      // 2) 提交
      const pr = await fetch(`/api/v1/runs/${runId}/publish`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ token: tbody.token, title, checked: true }),
      });
      if (!pr.ok) {
        let code = "HTTP_" + pr.status, message = pr.statusText;
        try { const d = await pr.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        throw Object.assign(new Error(message), { code, status: pr.status });
      }
      const pbody = await pr.json();
      if (pbody.status === "succeeded") {
        window.alert("已写入草稿箱，media_id：" + pbody.media_id + "，请到公众号后台处理");
        // 提交成功后由调用方 onDone 决定刷新哪个页面，四者一致（不跳去别的页面）。
        refresh();
      } else {
        window.alert("发布结果未知（unknown），禁止重复提交，请到公众号后台草稿箱核对");
      }
    } catch (err) {
      window.alert("发布失败：" + (err.code || err.message));
    }
  });

  // 预检通过且已授权才启用提交按钮
  const poll = setInterval(() => {
    publishBtn.disabled = !(preflightBox.dataset.ok === "1" && perm);
  }, 500);
  setTimeout(() => clearInterval(poll), 10000);

  return section;
}

// ============ M4：P6 生成进行中（job 进度 / 逐图状态 / 取消 / 重试） ============

// 由 run_id 找到最近一个 job 再渲染（P6 页面入口 #/runs/{run_id}/job）
async function renderRunJob(runId) {
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back-link", href: "#/runs/" + runId, text: "← 返回任务详情" }));
  /* 页面标题由顶栏 h1 承载 */

  const container = el("div");
  app.appendChild(container);
  container.appendChild(loadingState("正在定位生成任务…"));

  let jobs = [];
  try {
    const body = await api("/api/v1/jobs?run_id=" + encodeURIComponent(runId));
    jobs = body.jobs || [];
  } catch (err) {
    container.innerHTML = "";
    if (err.status === 403) container.appendChild(forbiddenState());
    else container.appendChild(errorState("加载生成任务失败：" + (err.code || err.message), () => renderRunJob(runId)));
    return;
  }

  if (jobs.length === 0) {
    container.innerHTML = "";
    container.appendChild(emptyState("该任务还没有生成记录，去「选题确认」发起生成"));
    return;
  }
  // 取最近一个 job（列表按 job_id 排序，取末个）
  const job = jobs[jobs.length - 1];
  renderJobBody(job, runId);
}

async function renderJob(jobId) {
  stopJobPolling();
  app.innerHTML = "";
  /* 页面标题由顶栏 h1 承载 */

  // 无 ID 入口（#/jobs）：解析最近一次运行，再定位其最近一个 job；无权限/请求失败/无 job 分别渲染对应状态
  if (!jobId) {
    const resolved = await resolveLatestRunState();
    if (resolved.denied) {
      app.innerHTML = "";
      app.appendChild(forbiddenState());
      return;
    }
    if (resolved.error) {
      app.innerHTML = "";
      app.appendChild(errorState("加载任务列表失败：" + (resolved.error.code || resolved.error.message || "网络错误"), () => renderJob(null)));
      return;
    }
    if (resolved.runId) {
      try {
        const body = await api("/api/v1/jobs?run_id=" + encodeURIComponent(resolved.runId));
        const jobs = body.jobs || [];
        if (jobs.length > 0) {
          const job = jobs[jobs.length - 1];
          location.replace("#/jobs/" + job.job_id);
          return;
        }
      } catch (err) {
        if (err.status === 403) {
          app.innerHTML = "";
          app.appendChild(forbiddenState());
          return;
        }
        // 非 403：请求失败，渲染可重试错误态，绝不落到 renderJobEmpty()
        app.innerHTML = "";
        app.appendChild(errorState("加载生成任务列表失败：" + (err.code || err.message || "网络错误"), () => renderJob(null)));
        return;
      }
    }
    renderJobEmpty();
    return;
  }

  const container = el("div");
  app.appendChild(container);
  container.appendChild(loadingState("正在加载生成任务…"));

  let job;
  try {
    job = await api("/api/v1/jobs/" + jobId);
  } catch (err) {
    container.innerHTML = "";
    if (err.status === 403) container.appendChild(forbiddenState());
    else if (err.status === 404) container.appendChild(errorState("生成任务不存在或已被移除", () => renderHome()));
    else container.appendChild(errorState("加载失败：" + (err.code || err.message), () => renderJob(jobId)));
    return;
  }

  container.innerHTML = "";
  renderJobBody(job, job.run_id);
}

function renderJobEmpty() {
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back-link", href: "#/", text: "← 返回任务列表" }));
  app.appendChild(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [el("h2", { text: "生成进行中" })]),
    el("div", { class: "card-body" }, [emptyState("还没有生成任务，去「选题确认」发起生成")]),
  ]));
}

function renderJobBody(job, runId) {
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back-link", href: "#/runs/" + runId, text: "← 返回任务详情" }));
  /* 页面标题由顶栏 h1 承载 */

  const status = job.status || "unknown";
  const meta = el("div", { class: "muted", text: "job_id：" + job.job_id + " · 状态：" + (JOB_STATUS_TEXT[status] || status) + " · 尝试次数：" + (job.attempt || 1) + " · 更新时间：" + (job.updated_at || "—") });
  app.appendChild(meta);

  // stepper（topic/write/review/visual）：bubble/lbl 气泡结构，失败态统一用 `fail` 类
  const stepper = el("div", { class: "stepper" });
  const currentStep = job.step || "topic";
  const stepDone = status === "succeeded";
  const stepIcons = { topic: "◎", write: "✎", review: "✓", visual: "🖼" };
  for (const s of STEP_ORDER) {
    let cls = "step";
    let glyph = stepIcons[s];
    if (stepDone) { cls += " done"; glyph = "✓"; }
    else if (STEP_ORDER.indexOf(s) < STEP_ORDER.indexOf(currentStep)) { cls += " done"; glyph = "✓"; }
    else if (s === currentStep && status === "running") cls += " cur";
    else if (s === currentStep && status === "failed") { cls += " fail"; glyph = "✕"; }
    else if (s === currentStep && status === "cancelled") { cls += " fail"; glyph = "✕"; }
    stepper.appendChild(el("div", { class: cls }, [
      el("div", { class: "bubble" }, [glyph]),
      el("div", { class: "lbl" }, [STEP_LABELS[s]]),
    ]));
  }
  app.appendChild(stepper);

  // 进度（不伪造百分比，批处理中语义）
  const progress = job.progress || {};
  const progressText = progress.kind === "step"
    ? "步骤 " + (progress.completed || 0) + "/" + (progress.total || 4)
    : "批处理中";
  app.appendChild(el("div", { class: "muted", text: "进度：" + progressText }));

  // 失败去向
  if (job.last_error) {
    app.appendChild(el("div", { class: "error-block", text: "最后错误：" + (job.last_error.code || "") + " " + (job.last_error.message || "") }));
  }

  // 逐图状态与缩略图
  const images = job.images || [];
  if (images.length) {
    app.appendChild(el("div", { class: "section-title", text: "配图状态" }));
    const grid = el("div", { class: "image-grid" });
    for (const img of images) {
      const st = img.status || "pending";
      const cell = el("div", { class: "card image-cell" }, [
        el("div", { class: "muted", text: "图 " + (img.index + 1) + "：" + (IMAGE_STATUS_TEXT[st] || st) }),
      ]);
      if (st === "succeeded") {
        const thumb = el("img", {
          class: "image-thumb",
          src: "/api/v1/runs/" + encodeURIComponent(runId) + "/images/" + (img.index || 0) + ".png",
          alt: "图 " + (img.index + 1),
          loading: "lazy",
        });
        thumb.addEventListener("error", () => { thumb.style.display = "none"; });
        cell.appendChild(thumb);
      } else if (st === "failed") {
        cell.appendChild(el("div", { class: "step-error", text: img.error ? ("错误：" + img.error) : "生成失败" }));
        if (!img.retried) {
          const retryBtn = el("button", { class: "btn", text: "重试失败图", onclick: async () => {
            try {
              const token = await csrfToken();
              const res = await fetch(`/api/v1/jobs/${job.job_id}/images/retry`, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
                body: JSON.stringify({ indices: [img.index] }),
              });
              if (!res.ok) {
                let code = "HTTP_" + res.status, message = res.statusText;
                try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
                throw Object.assign(new Error(message), { code, status: res.status });
              }
              refreshJobView();
            } catch (err) {
              window.alert("重试失败图失败：" + (err.code || err.message));
            }
          } });
          cell.appendChild(retryBtn);
        } else {
          cell.appendChild(el("div", { class: "muted", text: "已重试一次" }));
        }
      }
      grid.appendChild(cell);
    }
    app.appendChild(grid);
  }

  // 取消（仅运行中/排队中）
  if (status === "running" || status === "queued") {
    const cancelBtn = el("button", { class: "btn", text: "取消生成", onclick: async () => {
      if (!window.confirm("取消请求会记录，并在安全边界停止后续步骤；在途外部调用可能继续产生费用。确认取消？")) return;
      try {
        const token = await csrfToken();
        const res = await fetch(`/api/v1/jobs/${job.job_id}/cancel`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
          body: JSON.stringify({}),
        });
        if (!res.ok) {
          let code = "HTTP_" + res.status, message = res.statusText;
          try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
          throw Object.assign(new Error(message), { code, status: res.status });
        }
        window.alert("取消请求已记录，等待安全边界");
        refreshJobView();
      } catch (err) {
        window.alert("取消失败：" + (err.code || err.message));
      }
    } });
    app.appendChild(el("div", { class: "preview-controls" }, [cancelBtn]));
  }

  // 失败/取消/未知可重试
  if (status === "failed" || status === "cancelled" || status === "unknown") {
    const resumeBtn = el("button", { class: "btn", text: "重试生成", onclick: async () => {
      try {
        const token = await csrfToken();
        const res = await fetch(`/api/v1/jobs/${job.job_id}/resume`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
          body: JSON.stringify({}),
        });
        if (!res.ok) {
          let code = "HTTP_" + res.status, message = res.statusText;
          try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
          throw Object.assign(new Error(message), { code, status: res.status });
        }
        refreshJobView();
      } catch (err) {
        window.alert("重试失败：" + (err.code || err.message));
      }
    } });
    app.appendChild(el("div", { class: "preview-controls" }, [resumeBtn]));
  }

  // 完成/失败后去向
  const nav = el("div", { class: "preview-controls" });
  nav.appendChild(el("a", { class: "btn", href: "#/runs/" + runId, text: "查看任务详情" }));
  if (status === "succeeded") {
    nav.appendChild(el("a", { class: "btn", href: "#/runs/" + runId, text: "前往发布确认" }));
  }
  app.appendChild(nav);

  // 立即刷新视图（按钮动作后调用）
  const refreshJobView = () => renderJob(job.job_id);

  // 非终态启动轮询。回调里先校验当前路由仍是 job 页，否则停表——防止
  // render() 已被 hashchange 调去画别的页面时，本 timer 又把整页盖回 job 页。
  if (["succeeded", "failed", "cancelled", "unknown"].indexOf(status) < 0) {
    stopJobPolling();
    _jobPollTimer = setInterval(() => {
      const r = currentRoute();
      if (r.page !== "job" && r.page !== "runJob") {
        stopJobPolling();
        return;
      }
      renderJob(job.job_id);
    }, 1500);
  }
}
