"""前端视觉对齐设计图回归用例（JEE-1930 S8-b 返修）。

只做静态结构断言：验证 index.html / styles.css / app.js 已对齐设计图
`wewrite-workbench-demo.html` 的视觉与信息架构，不改变任何后端 API 契约。

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

from conftest import auth_headers  # noqa: F401  (保持与同目录用例一致)


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
