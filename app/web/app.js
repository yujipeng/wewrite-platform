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
  } else if (route.page === "job") {
    await renderJob(route.jobId);
  } else if (route.page === "runJob") {
    await renderRunJob(route.runId);
  }
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

  // 选题
  const topic = d.topic || {};
  sections.push(el("div", { class: "section-title", text: "选题" }));
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
  sections.push(topicKv);

  // 配图限额
  const visual = d.visual || {};
  sections.push(el("div", { class: "section-title", text: "配图设置" }));
  const visualKv = el("dl", { class: "kv" });
  for (const [k, v] of [
    ["视觉模式", visual.mode],
    ["最大图片数", visual.max_images],
    ["最大成本", visual.max_cost],
  ]) {
    visualKv.appendChild(el("dt", { text: k }));
    visualKv.appendChild(el("dd", { text: v == null || v === "" ? "未提供" : String(v) }));
  }
  sections.push(visualKv);

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

  // 审稿结论与 SEO
  sections.push(el("div", { class: "section-title", text: "审稿结论与 SEO" }));
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
  sections.push(edKv);

  // 产物清单
  sections.push(el("div", { class: "section-title", text: "产物清单" }));
  const artBadges = el("div", { class: "artifact-badges" });
  for (const [name, label] of Object.entries(ARTIFACT_LABELS)) {
    const on = !!(d.artifacts && d.artifacts[name]);
    artBadges.appendChild(el("span", { class: "artifact-badge " + (on ? "on" : "off"), text: label + "：" + (on ? "已落盘" : "尚未生成") }));
  }
  sections.push(artBadges);

  // 发布概要
  const publish = d.publish || {};
  sections.push(el("div", { class: "section-title", text: "发布概要" }));
  const pubBox = el("div", { class: "publish-box" }, [
    el("div", { text: "发布状态：" + (publish.status || "not_published") }),
    el("div", { text: "media_id：" + (publish.media_id || "无") }),
    publish.unknown_at ? el("div", { text: "结果未知时间：" + publish.unknown_at }) : null,
  ]);
  sections.push(pubBox);

  // 发布闸门（P7）
  sections.push(renderPublishGate(d.run_id, d));

  // 失败步骤去向
  if (d.last_error) {
    sections.push(el("div", { class: "section-title", text: "失败去向" }));
    sections.push(el("div", { class: "error-block", text: "最后错误：" + d.last_error }));
  }

  return el("div", {}, sections);
}

window.addEventListener("hashchange", render);
render();

// ============ M2：P3 产物预览 ============

function sourceLabel(source) {
  return { draft: "初稿 draft.md", article: "终稿 article.md", illustrated_article: "带图版 article-illustrated.md" }[source] || source;
}

async function renderPreview(runId) {
  app.innerHTML = "";
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

  // 公众号
  container.appendChild(el("div", { class: "section-title", text: "公众号（secret 只写不读）" }));
  const appidInput = el("input", { type: "text", value: cfg.wechat.appid || "", placeholder: "AppID" });
  const authorInput = el("input", { type: "text", value: cfg.wechat.author || "", placeholder: "作者" });
  const secretInput = el("input", { type: "password", value: "", placeholder: cfg.wechat.secret_configured ? "已配置（留空保持）" : "AppSecret" });
  container.appendChild(kvForm([["AppID", appidInput], ["作者", authorInput], ["AppSecret", secretInput]]));

  // 图片供应商
  container.appendChild(el("div", { class: "section-title", text: "图片供应商与 fallback 链（provider 只读展示，model/base_url 可编辑）" }));
  const providerInput = el("input", { type: "text", value: cfg.image.provider || "", placeholder: "provider（env 接管时只读）", disabled: true });
  const modelInput = el("input", { type: "text", value: cfg.image.model || "", placeholder: "model" });
  const baseUrlInput = el("input", { type: "text", value: cfg.image.base_url || "", placeholder: "base_url" });
  container.appendChild(kvForm([["provider", providerInput], ["model", modelInput], ["base_url", baseUrlInput]]));

  // 主题
  container.appendChild(el("div", { class: "section-title", text: "主题选择" }));
  const themeInput = el("input", { type: "text", value: cfg.theme || "professional-clean", placeholder: "主题名" });
  container.appendChild(kvForm([["theme", themeInput]]));

  // 时区展示
  container.appendChild(el("div", { class: "muted", text: "时区：Asia/Shanghai（UTC+8）" }));

  // env_managed 提示
  if (cfg.env_managed && cfg.env_managed.length) {
    const managed = cfg.env_managed.map((m) => m.field + " ← " + m.env).join("、");
    container.appendChild(el("div", { class: "notice", text: "以下字段由环境变量接管，写入将返回 409 ENV_MANAGED：" + managed }));
  }

  const saveBtn = el("button", { class: "btn", text: "保存配置" });
  const result = el("div", { class: "muted", text: "" });
  container.appendChild(el("div", { class: "preview-controls" }, [saveBtn, result]));

  saveBtn.addEventListener("click", async () => {
    const body = {
      theme: themeInput.value || undefined,
      image: { model: modelInput.value || undefined, base_url: baseUrlInput.value || undefined },
      wechat: { appid: appidInput.value || undefined, author: authorInput.value || undefined },
    };
    if (secretInput.value) body.wechat.secret = secretInput.value;
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
      setTimeout(renderConfig, 400);
    } catch (err) {
      result.textContent = "保存失败：" + (err.code || err.message);
      result.classList.add("error-block");
    }
  });

  // ---------- P8-F3 写作风格 ----------
  container.appendChild(el("div", { class: "section-title", text: "写作风格（style.yaml）" }));

  // 四步 onboarding：缺 style.yaml（仅 version 无风格字段）时按定位→方向→语气→主题引导
  const styleFields = [
    ["name", "风格名称", "text"],
    ["industry", "行业", "text"],
    ["target_audience", "目标受众", "text"],
    ["topics", "话题（逗号分隔）", "text"],
    ["tone", "语气", "text"],
    ["voice", "声音", "text"],
    ["word_count", "字数", "number"],
    ["content_style", "内容风格", "text"],
    ["writing_persona", "写作人格", "text"],
    ["theme", "写作主题", "text"],
    ["blacklist", "黑名单（逗号分隔）", "text"],
    ["cover_style", "封面风格", "text"],
    ["author", "署名作者", "text"],
  ];
  const styleInputs = {};
  const styleKvRows = [];
  for (const [key, label, type] of styleFields) {
    let value = style[key];
    if (Array.isArray(value)) value = value.join("，");
    const input = el("input", { type, value: value == null ? "" : String(value), placeholder: label });
    styleInputs[key] = input;
    styleKvRows.push([label, input]);
  }
  container.appendChild(kvForm(styleKvRows));

  const styleSaveBtn = el("button", { class: "btn", text: "保存写作风格" });
  const styleResult = el("div", { class: "muted", text: "" });
  container.appendChild(el("div", { class: "preview-controls" }, [styleSaveBtn, styleResult]));

  styleSaveBtn.addEventListener("click", async () => {
    const body = {};
    for (const [key, _label, type] of styleFields) {
      const raw = styleInputs[key].value;
      if (key === "topics" || key === "blacklist") {
        body[key] = raw ? raw.split(/[,，]/).map((s) => s.trim()).filter(Boolean) : [];
      } else if (key === "word_count") {
        body[key] = raw ? parseInt(raw, 10) : undefined;
      } else {
        body[key] = raw || "";
      }
    }
    try {
      const token = await csrfToken();
      const res = await fetch("/api/v1/style", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status, message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      styleResult.textContent = "已保存";
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
    input.style.cssText = "padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:14px;width:100%";
    dl.appendChild(el("dd", {}, [input]));
  }
  return dl;
}

function renderPublishGate(runId, detail) {
  const section = el("div", { class: "card" });
  section.appendChild(el("div", { class: "section-title", text: "发布闸门（P7）" }));

  const perm = !!(detail.permissions && detail.permissions.publish);
  const statusLine = el("div", { class: "muted", text: "发布权限：" + (perm ? "已开启" : "未开启") });
  section.appendChild(statusLine);

  const authorizeBtn = el("button", { class: "btn", text: perm ? "撤销发布授权" : "开启发布授权" });
  const preflightBox = el("div", { class: "muted", text: "尚未预检" });
  const submitBox = el("div");
  // P7-F4：手动「核对草稿箱」（仅只读 get_draft，不自动轮询、不重发）
  const reconcileBtn = el("button", { class: "btn", text: "核对草稿箱" });
  const reconcileBox = el("div", { class: "muted" });
  section.appendChild(el("div", { class: "preview-controls" }, [authorizeBtn, reconcileBtn]));
  section.appendChild(preflightBox);
  section.appendChild(submitBox);
  section.appendChild(reconcileBox);

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
      renderDetail(runId);
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
        renderDetail(runId);
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

function renderJobBody(job, runId) {
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back-link", href: "#/runs/" + runId, text: "← 返回任务详情" }));
  /* 页面标题由顶栏 h1 承载 */

  const status = job.status || "unknown";
  const meta = el("div", { class: "muted", text: "job_id：" + job.job_id + " · 状态：" + (JOB_STATUS_TEXT[status] || status) + " · 尝试次数：" + (job.attempt || 1) + " · 更新时间：" + (job.updated_at || "—") });
  app.appendChild(meta);

  // stepper（topic/write/review/visual）
  const stepper = el("div", { class: "stepper" });
  const currentStep = job.step || "topic";
  const stepDone = status === "succeeded";
  for (const s of STEP_ORDER) {
    let cls = "step";
    if (stepDone) cls += " done";
    else if (STEP_ORDER.indexOf(s) < STEP_ORDER.indexOf(currentStep)) cls += " done";
    else if (s === currentStep && status === "running") cls += " active";
    else if (s === currentStep && status === "failed") cls += " failed";
    stepper.appendChild(el("div", { class: cls, text: STEP_LABELS[s] }));
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
