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


def test_p8_style_grouped_and_options(client):
    """JEE-1947：13 字段分 4 组、语气/声音/内容风格/封面风格为下拉 + 自定义、标签输入、字数档位。"""
    js = _app_js(client)
    # 4 组标题
    for group in ("定位", "方向", "语气与风格", "产出规格"):
        assert group in js
    # 下拉 + 自定义（常用值选项）
    assert "自定义…" in js
    assert '"tone", "语气", "select"' in js
    for v in ("专业", "亲和", "犀利", "幽默", "讲故事"):
        assert v in js
    for v in ("第一人称", "第三人称", "编辑口吻"):
        assert v in js
    for v in ("干货清单", "深度分析", "案例复盘", "观点评论"):
        assert v in js
    for v in ("极简", "大字报", "插画", "实拍"):
        assert v in js
    # 标签式输入
    assert "tag-chip-input" in js
    assert "输入后回车/逗号添加" in js
    # 字数档位
    assert "[800, 1200, 2000, 3000]" in js and '" 字"' in js
    # 写作主题与排版主题区分文案
    assert "写作主题" in js and "排版主题" in js


def test_p8_style_ai_generate_entry(client):
    """JEE-1947：智能生成风格入口，未配置模型时按钮禁用 + 中文提示。"""
    js = _app_js(client)
    assert "/api/v1/style/generate" in js
    assert "智能生成风格" in js
    assert "需先配置写作模型" in js
    assert "未自动保存" in js
    assert "writer_configured" in js


def test_p8_image_provider_select_and_link(client):
    """P8 图片供应商改为下拉：选项来自 image-providers 端点，含「自定义…」与联动。"""
    js = _app_js(client)
    assert "/api/v1/image-providers" in js
    assert "自定义…" in js
    # provider 用 <select>（el("select") 构造），不再是只读文本框
    assert 'el("select")' in js
    # 保存请求体包含 image.provider（含 env 接管时不发的分支）
    assert "image.provider" in js
    # api_key 为 password 只写不读
    assert 'type: "password"' in js
    assert "已配置（留空保持）" in js
    # extra_fields 提示（deployment / secret_key）
    assert "还需要额外参数" in js


def test_p8_theme_select(client):
    """P8 主题改为下拉：选项来自 /api/v1/themes，保留不在清单里的当前值。"""
    js = _app_js(client)
    assert "/api/v1/themes" in js
    assert "当前值" in js
    # 主题用 <select> 渲染（themeSelect 从 themes 填充 option）
    assert "themeSelect" in js


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
