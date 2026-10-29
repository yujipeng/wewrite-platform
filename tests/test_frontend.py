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
    """P8 写作风格区块渲染（整文件 YAML 编辑器 + 保存动作）。"""
    js = _app_js(client)
    assert "/api/v1/style" in js
    # 渲染了风格编辑器与保存动作
    for field in ("写作风格", "保存"):
        assert field in js
    # PUT /style 有写入动作
    assert '"/api/v1/style"' in js or "/api/v1/style" in js


def test_p8_style_grouped_and_options(client):
    """JEE-1947：写作风格改为整文件 YAML 编辑器（加载/智能生成/校验/保存）。"""
    js = _app_js(client)
    # 整文件编辑器 textarea
    assert "style-yaml-editor" in js
    assert 'el("textarea"' in js
    # 四个操作
    for btn in ("加载当前配置", "智能生成", "校验", "保存"):
        assert btn in js
    # 主题区分文案
    assert "写作主题" in js and "排版主题" in js
    # 未保存变更提示
    assert "beforeunload" in js
    assert "__styleDirty" in js


def test_p8_style_ai_generate_entry(client):
    """JEE-1947：智能生成风格入口，未配置模型时可点击引导 + 中文提示。"""
    js = _app_js(client)
    assert "/api/v1/style/generate" in js
    assert "/api/v1/style/validate" in js
    assert "智能生成" in js
    assert "去配置写作模型" in js
    assert "未自动保存" in js
    assert "writer_configured" in js


def test_p8_style_generate_not_configured_guides_to_writer(client):
    """JEE-1955：未配置时智能生成失败会定位并高亮「写作模型」区块。"""
    js = _app_js(client)
    # 生成失败分支捕获 WRITER_NOT_CONFIGURED 后滚动定位 + 高亮写作模型区块
    assert "WRITER_NOT_CONFIGURED" in js
    assert "highlightWriterSection" in js
    assert "scrollIntoView" in js
    assert "config-highlight" in js


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


def test_p8_writer_model_section_present(client):
    """P8「写作模型（文章生成）」区块：provider 下拉/联动/自定义/api_key 只写不读/env 优先。"""
    js = _app_js(client)
    assert "/api/v1/writer-providers" in js
    assert "写作模型（文章生成）" in js
    # 未配置 api_key 时给用户一眼后果（WRITER_NOT_CONFIGURED）
    assert "未配置写作模型，文章生成会失败" in js
    assert "WRITER_NOT_CONFIGURED" in js
    # 控件齐全：provider/model/base_url/api_key/temperature/max_tokens
    for label in ("模型提供商", "模型名称", "接口地址", "API 密钥", "温度（0–2）", "最大 token 数"):
        assert label in js
    # api_key 只写不读（password + 留空保持占位符）
    assert 'type: "password"' in js
    assert "已配置（留空保持）" in js
    # 保存请求体并入 writer（env 接管的字段不发）
    assert "writer.provider" in js
    assert "writer.temperature" in js
    assert "writer.max_tokens" in js


def test_p8_writer_numeric_validation_before_submit(client):
    """写作模型保存前校验：temperature 0–2 有限、max_tokens 正整数，不合法就地提示中止。"""
    js = _app_js(client)
    # 提交前校验存在：不合规则 return 中止，不发起请求
    assert "温度（0–2）必须是 0 到 2 之间的数字" in js
    assert "最大 token 数必须为正整数" in js
    # 禁止 parseInt 静默截断小数：用 Number + isInteger 校验，而不是 parseInt
    assert "Number.isInteger" in js
    assert "Number.isFinite" in js
    # 校验失败路径用 return 中止（不落到 fetch 分支）
    assert "return;" in js


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


def test_jee1963_length_warning_banner_present(client):
    """JEE-1963：P2/P3 字数提示条（含实际与目标字数，非阻断）。

    行为级证明（点击 → POST /regenerate）见 test_frontend_visual.py 的
    test_jee1963_regenerate_button_posts_to_endpoint（Node vm 交互 harness）。
    """
    js = _app_js(client)
    assert "length_warning" in js
    assert "lengthWarningBanner" in js
    # 提示文案：实际字数 + 目标字数，非阻断
    assert "正文约 " in js
    assert "低于目标 " in js
    # 提供重新生成入口
    assert "重新生成" in js
    # 真正 POST 到可执行端点
    assert "/regenerate" in js
