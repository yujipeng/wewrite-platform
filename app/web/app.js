// WeWrite 工作台前端（原生 ES modules，无构建）
// P1 工作台首页（run 列表）+ P2 任务详情，四态（空/加载/错误/无权限）齐全。

const app = document.getElementById("app");

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
  // #/runs/{run_id}
  const m = hash.match(/^#\/runs\/([0-9]{8}-[0-9]{6}-[a-f0-9]{6})$/);
  if (m) return { page: "detail", runId: m[1] };
  // #/runs/{run_id}/preview
  const pm = hash.match(/^#\/runs\/([0-9]{8}-[0-9]{6}-[a-f0-9]{6})\/preview$/);
  if (pm) return { page: "preview", runId: pm[1] };
  if (hash === "#/hotspots") return { page: "hotspots" };
  if (hash === "#/candidates") return { page: "candidates" };
  return { page: "home" };
}

async function render() {
  const route = currentRoute();
  document.querySelectorAll(".nav-link").forEach((n) => {
    const active =
      (n.dataset.nav === "home" && (route.page === "home" || route.page === "detail" || route.page === "preview")) ||
      (n.dataset.nav === "hotspots" && route.page === "hotspots") ||
      (n.dataset.nav === "candidates" && route.page === "candidates");
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
  }
}

async function renderHome() {
  app.innerHTML = "";
  app.appendChild(el("h1", { class: "page-title", text: "任务列表" }));

  // 搜索框（本地过滤题名与 run_id）
  const search = el("input", {
    class: "search-input",
    type: "search",
    placeholder: "按题名或 run_id 搜索…",
  });

  const container = el("div");
  app.appendChild(search);
  app.appendChild(container);

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
    const list = el("ul", { class: "run-list" });
    for (const r of filtered) {
      list.appendChild(runRow(r));
    }
    container.appendChild(list);
  };

  search.addEventListener("input", draw);
  draw();
}

function runRow(r) {
  const status = r.status || "unknown";
  const badge = el("span", {
    class: "badge badge-" + (status === "active" ? "active" : status === "completed" ? "completed" : status === "failed" ? "failed" : ""),
    text: STATUS_TEXT[status] || status,
  });

  const meta = el("div", { class: "run-meta" }, [
    r.run_id,
    " · ",
    MODE_TEXT[r.mode] || r.mode,
    " · ",
    r.created ? "创建于 " + r.created : "",
  ]);

  const artBadges = el("div", { class: "artifact-badges" });
  for (const [name, label] of Object.entries(ARTIFACT_LABELS)) {
    const on = !!(r.artifacts && r.artifacts[name]);
    artBadges.appendChild(
      el("span", { class: "artifact-badge " + (on ? "on" : "off"), text: label + (on ? "" : "（未生成）") })
    );
  }

  const publishBadge = el("span", {
    class: "badge",
    text: r.publish && r.publish.media_id ? "已发布" : "未发布",
  });

  const main = el("div", { class: "run-main" }, [
    el("div", { class: "run-title", text: r.title || "未命名文章" }),
    meta,
    el("div", { style: "margin-top:6px" }, [badge, publishBadge]),
  ]);

  const item = el("li", {
    class: "run-item",
    onclick: () => { location.hash = "#/runs/" + r.run_id; },
  }, [main, artBadges]);

  return item;
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
  const title = el("h1", { class: "page-title", text: d.title || "未命名文章" });

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

  const previewBtn = el("a", {
    class: "btn",
    href: "#/runs/" + d.run_id + "/preview",
    text: "预览产物",
  });

  const sections = [title, kv, previewBtn];

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

  // 步骤时间线
  sections.push(el("div", { class: "section-title", text: "步骤时间线" }));
  const steps = d.steps || {};
  if (Object.keys(steps).length === 0) {
    sections.push(emptyState("任务尚无步骤记录"));
  } else {
    const timeline = el("ul", { class: "timeline" });
    for (const [step, info] of Object.entries(steps)) {
      const s = info || {};
      const li = el("li", {}, [
        el("span", { class: "step-name", text: STEP_LABELS[step] || step }),
        el("span", { class: "step-status", text: STEP_STATUS_TEXT[s.status] || s.status }),
        s.error ? el("span", { class: "step-error", text: "错误：" + s.error }) : el("span", { class: "muted", text: "更新时间：" + (s.updated || "未提供") }),
      ]);
      timeline.appendChild(li);
    }
    sections.push(timeline);
  }

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
  app.appendChild(el("h1", { class: "page-title", text: "产物预览" }));

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
  app.appendChild(el("h1", { class: "page-title", text: "每日热榜" }));

  const actions = el("div", { class: "preview-controls" });
  const refreshBtn = el("button", { class: "btn", text: "立即刷新（抓取当下）" });
  const persistBtn = el("button", { class: "btn", text: "补抓并保存今日快照" });
  const hint = el("span", { class: "muted", text: "刷新只展示当下数据；保存会写入今日快照" });
  actions.appendChild(refreshBtn);
  actions.appendChild(persistBtn);
  actions.appendChild(hint);
  app.appendChild(actions);

  const container = el("div");
  app.appendChild(container);

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
  app.appendChild(el("h1", { class: "page-title", text: "选题确认" }));

  // 规则生成桩口径：分数来源标注（硬性要求）
  app.appendChild(el("div", { class: "notice" }, [
    "本页候选由规则生成器从当日热榜快照产出（非 AI 评估），相关度/价值分为规则打分，仅作对比参考。",
  ]));

  const actions = el("div", { class: "preview-controls" });
  const dateInput = el("input", { type: "date", value: todayLocal(), style: "padding:6px 10px;border:1px solid var(--border);border-radius:6px" });
  const genBtn = el("button", { class: "btn", text: "生成候选" });
  const forceBtn = el("button", { class: "btn", text: "强制重开候选集" });
  const hint = el("span", { class: "muted", text: "相同快照与风格会自动复用既有候选集" });
  actions.appendChild(dateInput);
  actions.appendChild(genBtn);
  actions.appendChild(forceBtn);
  actions.appendChild(hint);
  app.appendChild(actions);

  const container = el("div");
  app.appendChild(container);

  // 生成候选按钮禁用条件：当日快照全源失败（error 非空 / count==0 / items 空）
  async function refreshGenerateDisabled() {
    try {
      const snap = await api("/api/v1/hotspots");
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
    ]);

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
        body: JSON.stringify({ date: dateInput.value || todayLocal(), style_revision: "sha256:web-default", force }),
      });
      if (!res.ok) {
        let code = "HTTP_" + res.status; let message = res.statusText;
        try { const d = await res.json(); code = d.code || code; message = d.message || message; } catch (_) {}
        const err = new Error(message); err.code = code; err.status = res.status; throw err;
      }
      const body = await res.json();
      drawSet(body);
    } catch (err) {
      container.innerHTML = "";
      container.appendChild(errorState("生成失败：" + (err.code || err.message), () => doGenerate(force)));
    }
  };

  genBtn.addEventListener("click", () => doGenerate(false));
  forceBtn.addEventListener("click", () => doGenerate(true));

  await refreshGenerateDisabled();
  await load();
}

function todayLocal() {
  const d = new Date();
  const off = d.getTimezoneOffset();
  const local = new Date(d.getTime() - off * 60000);
  return local.toISOString().slice(0, 10);
}
