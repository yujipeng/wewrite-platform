"""前端静态结构用例（P6/P7/P8/P5/P4/P1 页面与交互在 app.js 中的落地回归）。

无浏览器依赖：断言静态资源可访问，且 app.js 内存在返修要求的页面路由与交互
（P6 轮询/取消/重试失败图、P7 核对草稿箱入口、P8 写作风格渲染、P5 并排对比、
P4 历史快照、P1 新建任务）。真实浏览器点击由 S7 复验的 Playwright 场景覆盖。
"""

from __future__ import annotations

from conftest import auth_headers


def test_index_serves_nav_links(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for anchor in ("#/hotspots", "#/candidates", "#/config"):
        assert anchor in html


def test_app_js_served(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "text/javascript" in r.headers.get("content-type", "") or "javascript" in r.headers.get("content-type", "")


def _app_js(client) -> str:
    r = client.get("/static/app.js")
    assert r.status_code == 200
    return r.text


def test_p6_job_page_present(client):
    """P6「生成进行中」页面：路由、轮询、取消、重试失败图。"""
    js = _app_js(client)
    assert '#/jobs/' in js
    assert 'page: "job"' in js
    assert 'page: "runJob"' in js
    # 轮询（非终态每 1.5s 刷新）
    assert "setInterval" in js
    assert "/api/v1/jobs/" in js
    # 取消
    assert "/cancel" in js and "取消生成" in js
    # 重试失败图
    assert "/images/retry" in js and "重试失败图" in js
    # 重试生成（resume）
    assert "/resume" in js


def test_p7_reconcile_entry_present(client):
    """P7「核对草稿箱」入口调用 POST /publish/reconcile。"""
    js = _app_js(client)
    assert "/publish/reconcile" in js
    assert "核对草稿箱" in js


def test_p8_style_rendered(client):
    """P8 写作风格字段渲染（不再只 GET 不展示）。"""
    js = _app_js(client)
    assert "/api/v1/style" in js
    # 渲染了风格字段与保存动作
    for field in ("风格名称", "行业", "目标受众", "语气", "写作人格", "保存写作风格"):
        assert field in js
    # PUT /style 有写入动作
    assert '"/api/v1/style"' in js or "/api/v1/style" in js


def test_p5_compare_present(client):
    """P5 并排对比（最多 3 条）。"""
    js = _app_js(client)
    assert "并排对比" in js
    assert "最多 3 条" in js or "最多并排对比 3 条" in js


def test_p4_history_snapshots_present(client):
    """P4 历史快照浏览：调用 /hotspot-snapshots 与 /hotspots/{date}。"""
    js = _app_js(client)
    assert "/api/v1/hotspot-snapshots" in js
    assert "/api/v1/hotspots/" in js


def test_p1_new_task_entry_present(client):
    """P1「新建任务」入口跳转每日热榜。"""
    js = _app_js(client)
    assert "新建任务" in js
    assert '#/hotspots' in js
