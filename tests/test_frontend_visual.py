"""前端视觉对齐设计图回归用例（JEE-1930 S8-b 返修）。

静态结构断言 + Node `vm` 交互断言（真实执行 app.js 逻辑，非源码字符串匹配）：
- index.html / styles.css / app.js 对齐设计图 `wewrite-workbench-demo.html` 的视觉与信息架构；
- 无 ID 路由（#/jobs、#/runs、#/preview、#/publish）有确定落点，不静默回退工作台；
- 窄屏抽屉导航入口（menuToggle / drawerMask）真实串联侧栏开关；
- P6 失败步骤用 .step.fail + bubble/lbl 气泡结构，类名与 CSS 一致。

验收口径（issue 第四节）：
1. 深色侧边栏：品牌 logo「W」+「WeWrite 工作台 / 公众号写作流水线」+ 三组导航
   （主流程/运行视图/系统）+ 图标 + active 高亮（--teal 底白字）；
2. 顶栏：面包屑 + 当前页 h1 标题 + 右侧动作区；
3. 内容区卡片化：运行列表表格、步骤时间线（stepper 气泡）、状态 chip、发布闸门卡片，
   对齐设计图配色（青色主色、珊瑚红警示）；
4. 页面标题文案对齐设计图（首页「工作台（运行列表）」而非「任务列表」）。

真实浏览器视觉对比由 JEE-1914 运维侧重跑 Playwright 验收。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from conftest import auth_headers  # noqa: F401  (保持与同目录用例一致)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HARNESS = _REPO_ROOT / "tests" / "frontend_interaction_harness.mjs"


def _run_interaction_harness() -> dict:
    """用 Node vm 执行真实 app.js 逻辑，返回 harness 的 JSON 汇总。"""
    node = shutil.which("node") or shutil.which("nodejs")
    assert node, "测试需要 node 可用（node --check 已通过，但未找到 node 可执行文件）"
    proc = subprocess.run(
        [node, str(_HARNESS)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"交互 harness 失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)



def _html(client) -> str:
    r = client.get("/")
    assert r.status_code == 200
    return r.text


def _css(client) -> str:
    r = client.get("/static/styles.css")
    assert r.status_code == 200
    return r.text


def _app_js(client) -> str:
    r = client.get("/static/app.js")
    assert r.status_code == 200
    return r.text


def test_index_sidebar_brand_and_three_nav_groups(client):
    """验收口径 1：深色侧边栏 + 品牌 + 三组导航 + 图标。"""
    html = _html(client)
    assert "sidebar" in html
    # 品牌 logo「W」与「公众号写作流水线」
    assert 'class="logo"' in html and ">W<" in html
    assert "WeWrite 工作台" in html
    assert "公众号写作流水线" in html
    # 三组导航
    for g in ("主流程", "运行视图", "系统"):
        assert g in html
    # 各导航项与图标
    for item, icon in (
        ("工作台（运行列表）", "◧"),
        ("每日热榜", "🔥"),
        ("选题确认", "☑"),
        ("生成进行中", "▶"),
        ("任务详情", "▤"),
        ("产物预览", "👁"),
        ("发布确认", "🚀"),
        ("配置", "⚙"),
    ):
        assert item in html
        assert icon in html


def test_index_topbar_crumb_title_actions(client):
    """验收口径 2：顶栏面包屑 + h1 + 右侧动作区。"""
    html = _html(client)
    assert "topbar" in html
    assert "crumb" in html
    assert 'id="pageTitle"' in html
    assert 'id="topActions"' in html


def test_index_no_legacy_top_navbar(client):
    """现状（需改掉）：顶部横向导航条，无侧边栏——回归断言已移除。"""
    html = _html(client)
    # 旧顶部导航（.nav-link）与旧品牌样式不再存在
    assert "nav-link" not in html


def test_css_design_tokens(client):
    """验收口径 3：设计图配色 token（青色主色 + 珊瑚红警示）。"""
    css = _css(client)
    assert "--teal" in css
    assert "#0b7d74" in css
    assert "#16a596" in css
    assert "--coral" in css
    # 深色侧边栏底色
    assert "#1f2a30" in css
    # active 高亮 teal 底白字
    assert ".nav a.active" in css


def test_css_card_chip_stepper_widgets(client):
    """验收口径 3：卡片 / chip / stepper 气泡等设计图组件样式存在。"""
    css = _css(client)
    for sel in (".card", ".card-head", ".card-body", ".chip", ".stepper", ".step", ".table"):
        assert sel in css


def test_app_js_home_title_aligned(client):
    """验收口径 4：首页标题文案「工作台（运行列表）」，不再是「任务列表」。"""
    js = _app_js(client)
    assert "工作台（运行列表）" in js
    # 旧文案「任务列表」不得再作为首页标题渲染
    assert '"任务列表"' not in js


def test_app_js_topbar_rendering(client):
    """顶栏面包屑 + h1 + 动作区由 app.js 驱动渲染。"""
    js = _app_js(client)
    assert "pageTitle" in js
    assert "crumbCur" in js
    assert "topActions" in js


def test_app_js_sidebar_active_highlight(client):
    """active 高亮随路由切换（侧边栏 nav 项 classList.toggle active）。"""
    js = _app_js(client)
    assert "classList.toggle" in js
    assert ".nav" in js or "nav a" in js or "sidebar" in js


def test_app_js_p1_runs_table(client):
    """验收口径 3：P1 运行列表卡片化表格（.table / runTable）。"""
    js = _app_js(client)
    assert "runTable" in js or ".table" in js or "运行列表" in js


def test_app_js_detail_stepper_and_chip(client):
    """验收口径 3：任务详情步骤时间线 stepper 气泡 + 状态 chip。"""
    js = _app_js(client)
    assert "stepper" in js
    assert "chip" in js


# ============ 返修阻断项 1–3 + 拦截项 1–3：机械可判定交互断言（非源码字符串匹配） ============

def test_no_id_routes_resolve_to_definite_pages(client):
    """阻断项 1：无 ID 入口 #/jobs、#/runs、#/preview、#/publish 路由有确定落点。"""
    _html(client)  # 确认静态资源可访问（保持同目录风格）
    summary = _run_interaction_harness()
    by_name = {c["name"]: c["ok"] for c in summary["checks"]}
    # 具体断言：四个无 ID 入口都返回确定页面（job/detail/preview/publish），非 home
    assert by_name["route #/jobs -> job"] is True
    assert by_name["route #/runs -> detail"] is True
    assert by_name["route #/preview -> preview"] is True
    assert by_name["route #/publish -> publish"] is True


def test_narrow_screen_drawer_navigation(client):
    """阻断项 2 + 拦截 3：窄屏替代导航真实串联（含真实派发 nav 链接 click 关闭抽屉）。"""
    _html(client)
    summary = _run_interaction_harness()
    by_name = {c["name"]: c["ok"] for c in summary["checks"]}
    assert by_name["index.html 有 menuToggle 抽屉开关"] is True
    assert by_name["index.html 有 drawerMask 遮罩"] is True
    assert by_name["点击 menuToggle 打开抽屉（sidebar.open）"] is True
    assert by_name["点击遮罩关闭抽屉"] is True
    assert by_name["抽屉打开后真实派发 nav 链接点击"] is True
    assert by_name["点击 nav 链接关闭抽屉（sidebar.open 移除）"] is True


def test_job_stepper_failed_class_and_bubbles(client):
    """阻断项 3：P6 失败步骤用 .step.fail + bubble/lbl 气泡，失败态有可见区分。"""
    _app_js(client)
    summary = _run_interaction_harness()
    by_name = {c["name"]: c["ok"] for c in summary["checks"]}
    assert by_name["renderJobBody 输出 4 个 .step"] is True
    assert by_name["失败步骤使用 .fail 类（非 .failed）"] is True
    assert by_name["失败步骤含 .bubble 气泡"] is True
    assert by_name["失败步骤含 .lbl 标签"] is True
    assert by_name["失败步骤气泡文案为 ✕"] is True


def test_no_id_entry_distinguishes_error_denied_empty(client):
    """拦截 1：无 ID 入口区分 500/网络错误态、403 无权限态、成功空列表空态。"""
    _app_js(client)
    summary = _run_interaction_harness()
    by_name = {c["name"]: c["ok"] for c in summary["checks"]}
    for name in (
        "renderDetail(null) 500 → 错误态(含重试)",
        "renderDetail(null) 网络错误 → 错误态(含重试)",
        "renderDetail(null) 403 → 无权限态",
        "renderDetail(null) 空列表 → 空态(非错误态)",
        "renderPreview(null) 500 → 错误态(含重试)",
        "renderPublish(null) 500 → 错误态(含重试)",
        "renderJob(null) jobs 500 → 错误态(含重试,非空态)",
        "renderJob(null) jobs 403 → 无权限态",
    ):
        assert by_name[name] is True, f"harness 断言失败：{name}"


def test_publish_page_action_keeps_url_body_title_highlight_consistent(client):
    """拦截 2：发布确认页授权成功后地址 ↔ 正文 ↔ h1 ↔ 侧栏高亮四者一致。"""
    _app_js(client)
    summary = _run_interaction_harness()
    by_name = {c["name"]: c["ok"] for c in summary["checks"]}
    for name in (
        "发布页初始 h1 为「发布确认」",
        "发布页初始 publish 导航高亮",
        "授权成功后正文仍是发布页(含发布闸门)",
        "授权成功后地址不变(#/runs/{id}/publish)",
        "授权成功后 h1 仍是「发布确认」",
        "授权成功后 publish 导航仍高亮",
    ):
        assert by_name[name] is True, f"harness 断言失败：{name}"


def test_p8_writer_not_configured_clickable_guide(client):
    """JEE-1955 返修：未配置时按钮可点击，点击后真实触发 scrollIntoView + config-highlight。"""
    _app_js(client)
    summary = _run_interaction_harness()
    by_name = {c["name"]: c["ok"] for c in summary["checks"]}
    for name in (
        "未配置时按钮可点击（文案「去配置写作模型」）",
        "存在「写作模型（文章生成）」区块",
        "点击未配置按钮后真实调用 scrollIntoView",
        "点击未配置按钮后区块加 config-highlight",
    ):
        assert by_name[name] is True, f"harness 断言失败：{name}"


def test_p8_writer_not_configured_error_message_no_code(client):
    """JEE-1955 返修：WRITER_NOT_CONFIGURED 渲染中文提示且不含裸 code。"""
    _app_js(client)
    summary = _run_interaction_harness()
    by_name = {c["name"]: c["ok"] for c in summary["checks"]}
    for name in (
        "点击「生成」确实发起了 style/generate 请求",
        "WRITER_NOT_CONFIGURED 渲染中文可操作提示",
        "WRITER_NOT_CONFIGURED 渲染不含裸 code",
    ):
        assert by_name[name] is True, f"harness 断言失败：{name}"


def test_jee1963_regenerate_button_posts_to_endpoint(client):
    """JEE-1963 返修：有 length_warning 时「重新生成」真实 POST /regenerate 并直达新 job；无警告不出现按钮。"""
    _app_js(client)
    summary = _run_interaction_harness()
    by_name = {c["name"]: c["ok"] for c in summary["checks"]}
    for name in (
        "有 length_warning 时详情页出现「重新生成」按钮",
        "点击「重新生成」真实 POST /api/v1/jobs/{job_id}/regenerate",
        "点击后直达新 job（#/jobs/j-000000000000，新编号小于旧编号）",
        "新编号大于旧编号时按钮仍出现",
        "点击后直达新 job（#/jobs/j-ffffffffffff，新编号大于旧编号）",
        "无 length_warning 的成功任务不出现「重新生成」按钮",
    ):
        assert by_name[name] is True, f"harness 断言失败：{name}"

