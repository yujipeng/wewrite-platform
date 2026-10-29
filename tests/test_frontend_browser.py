"""P6 真实浏览器回归用例（返修第二轮：冷启动 TDZ + 轮询锁死）。

上一轮 `tests/test_frontend.py` 只做源码字符串断言，测不出「冷启动直达
`#/jobs/{job_id}` 触发 TDZ 白屏」与「进入 P6 后轮询把导航覆盖回 job 页」。
本文件用 Playwright + Chromium 真跑页面：

1. 冷启动直达 job 页：main 非空、无 pageerror、无 TDZ 崩溃；
2. 进入 P6 后切到 `#/config`：等待超过一个轮询周期后内容仍是配置页（不是 job 页）。

无可用 Chromium 时整组 skip（本仓库无 CI，本地可复现环境需自带 Chromium；
用环境变量 `WEWRITE_PLAYWRIGHT_EXECUTABLE` 指定 chrome/chromium 可执行路径）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="未安装 playwright")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_chromium() -> str | None:
    env = os.environ.get("WEWRITE_PLAYWRIGHT_EXECUTABLE")
    if env and os.path.exists(env):
        return env
    # 常见路径（含本机 chrome-cft 的捆绑库浏览器）。裸 chrome 二进制缺 libatk 等
    # 系统库会在 launch 时抛 TargetClosedError，由 fixture 兜底整组 skip；需要真跑
    # 浏览器时用 WEWRITE_PLAYWRIGHT_EXECUTABLE 指向带 LD_LIBRARY_PATH 的 wrapper
    # （如 ~/.local/bin/google-chrome）。
    candidates = [
        os.path.expanduser("~/.local/chrome-cft/chrome-linux64/chrome"),
        os.path.expanduser("~/.local/bin/google-chrome"),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def _launch_server(home: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["WEWRITE_HOME"] = home
    env["WEWRITE_PLATFORM_NO_WORKER"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def _wait_healthz(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("uvicorn 未在超时内就绪")


def _make_job_fixture(home: str) -> tuple[str, str]:
    """造一个 queued job 的 fixture（run_id + job_id）。"""
    import json

    import yaml

    run_id = "20261026-120000-abcdef"
    rundir = os.path.join(home, "runs", run_id)
    os.makedirs(rundir, exist_ok=True)
    state = {
        "version": 4,
        "run_id": run_id,
        "status": "active",
        "mode": "draft",
        "created": "2026-10-26T10:00:00+08:00",
        "updated": "2026-10-26T10:00:00+08:00",
        "permissions": {"publish": False},
        "visual": {"mode": "none", "max_images": 0, "max_cost": None},
        "topic": {"title": "测试选题", "keywords": ["k"], "source": "微博", "framework_hint": ""},
        "artifacts": {},
        "steps": {},
        "last_error": None,
    }
    with open(os.path.join(rundir, "state.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)

    jobdir = os.path.join(home, "jobs")
    os.makedirs(jobdir, exist_ok=True)
    job_id = "j-test000001"
    job = {
        "version": 1,
        "job_id": job_id,
        "run_id": run_id,
        "status": "queued",
        "attempt": 1,
        "cancel_requested": False,
        "step": "topic",
        "progress": {"kind": "step", "completed": 0, "total": 4},
        "images": [],
        "created_at": "2026-10-26T10:00:00Z",
        "updated_at": "2026-10-26T10:00:00Z",
        "heartbeat_at": None,
        "last_error": None,
    }
    with open(os.path.join(jobdir, f"{job_id}.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
    return run_id, job_id


@pytest.fixture(scope="module")
def browser_session(tmp_path_factory):
    chrome = _find_chromium()
    if chrome is None:
        pytest.skip("无可用 Chromium（可用 WEWRITE_PLAYWRIGHT_EXECUTABLE 指定）")

    home = str(tmp_path_factory.mktemp("wehome"))
    _make_job_fixture(home)

    # Playwright 用 TMPDIR 做 mkdtemp；本机环境的 TMPDIR 可能指向不存在的目录，
    # 强制指向 pytest 生成的临时目录（可写）。
    pw_tmp = tmp_path_factory.mktemp("pw-tmp")
    os.environ["TMPDIR"] = str(pw_tmp)

    port = 8894
    proc = _launch_server(home, port)
    try:
        _wait_healthz(port)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            try:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(tmp_path_factory.mktemp("pwprof")),
                    headless=True,
                    executable_path=chrome,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter"],
                    no_viewport=True,
                )
            except Exception as exc:
                pytest.skip(f"Chromium 启动失败（缺系统库或不可用）：{type(exc).__name__}")
            try:
                yield {"ctx": ctx, "base": f"http://127.0.0.1:{port}", "home": home}
            finally:
                ctx.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_p6_cold_start_job_page_no_blank(browser_session):
    """缺陷 1 回归：冷启动直达 #/jobs/{id}，main 非空且无 pageerror。"""
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(f"{base}/#/jobs/j-test000001", wait_until="networkidle")
    page.wait_for_timeout(500)

    main_text = page.inner_text("#app")
    # 不能是 TDZ 白屏
    assert page_errors == [], f"冷启动抛 JS 异常：{page_errors}"
    assert len(main_text.strip()) > 0, "冷启动 job 页 main 为空"
    assert "生成进行中" in main_text
    assert "j-test000001" in main_text


def test_p6_navigate_away_not_locked_by_polling(browser_session):
    """缺陷 2 回归：进入 P6 后切到 #/config，等待超过一个轮询周期仍是配置页。"""
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # 先进入 P6（queued 态会启动 1.5s 轮询）
    page.goto(f"{base}/#/jobs/j-test000001", wait_until="networkidle")
    page.wait_for_timeout(400)
    assert "生成进行中" in page.inner_text("#app")

    # 切到配置页
    page.goto(f"{base}/#/config", wait_until="networkidle")
    # 等超过两个轮询周期（3.2s），若 timer 未停会被盖回 job 页
    page.wait_for_timeout(3300)

    main_text = page.inner_text("#app")
    assert "生成进行中" not in main_text, "轮询把导航覆盖回 job 页（未停止）"
    assert "配置" in main_text
    assert page_errors == [], f"切换页面抛 JS 异常：{page_errors}"


def _kv_control(page, label):
    """按 kv 表单的 dt 文案定位其后的控件（dd 里的 select/input）。"""
    return page.locator("dt", has_text=label).locator("xpath=./following-sibling::dd[1]").locator("select, input")


def test_p8_config_provider_select_and_linkage(browser_session):
    """JEE-1935 真浏览器验证：provider 为下拉（10 内置 + 自定义…），选中 doubao 联动填充默认值。"""
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(f"{base}/#/config", wait_until="networkidle")
    page.wait_for_timeout(300)

    provider_select = _kv_control(page, "provider")
    assert provider_select.count() == 1, "provider 应为下拉选择框"
    options = provider_select.locator("option")
    assert options.count() == 11, f"应为 10 内置 + 自定义…，实际 {options.count()}"
    assert "自定义…" in [o.strip() for o in options.all_inner_texts()]
    assert "doubao" in [o.strip() for o in options.all_inner_texts()]

    # 选中 doubao → model/base_url 自动联动为默认值
    provider_select.select_option("doubao")
    model_input = _kv_control(page, "model")
    base_url_input = _kv_control(page, "base_url")
    assert model_input.input_value() == "doubao-seedream-5-0-260128"
    assert base_url_input.input_value() == "https://ark.cn-beijing.volces.com/api/v3"

    # 切换 openai → 两项都刷新为 openai 默认值，不残留 doubao 值
    provider_select.select_option("openai")
    assert model_input.input_value() == "dall-e-3"
    assert base_url_input.input_value() == "https://api.openai.com/v1"

    # 自定义：出现 provider 名输入框，可自由填写
    provider_select.select_option("__custom__")
    custom_input = page.locator('input[placeholder="自定义 provider 名称"]')
    assert custom_input.count() == 1, "选自定义…后应出现 provider 名称输入框"
    assert custom_input.is_visible(), "自定义 provider 输入框应可见"
    assert page_errors == [], f"配置页抛 JS 异常：{page_errors}"


def test_p8_config_theme_select_from_themes(browser_session):
    """JEE-1935 真浏览器验证：主题为下拉，选项来自 /api/v1/themes（当前 18 项）。"""
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(f"{base}/#/config", wait_until="networkidle")
    page.wait_for_timeout(300)

    theme_select = _kv_control(page, "theme")
    assert theme_select.count() == 1, "主题应为下拉选择框"
    option_values = theme_select.locator("option").evaluate_all(
        "(els) => els.map((e) => e.value)"
    )
    assert len(option_values) >= 18, f"主题应来自 /api/v1/themes（18 项），实际 {len(option_values)}"
    assert "professional-clean" in option_values
    assert page_errors == [], f"配置页抛 JS 异常：{page_errors}"


def test_p8_switch_to_azure_openai_empty_base_url_not_residue(browser_session):
    """返修回归：选 doubao → 改选 azure_openai → 保存，回读 base_url 为空、provider 为 azure_openai。

    覆盖空 base_url 被 `|| undefined` 吞掉导致残留旧地址的缺陷。
    """
    import json
    import urllib.request

    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    def read_config():
        data = urllib.request.urlopen(f"{base}/api/v1/config", timeout=5)
        return json.loads(data.read().decode("utf-8"))

    page.goto(f"{base}/#/config", wait_until="networkidle")
    page.wait_for_timeout(300)

    provider_select = _kv_control(page, "provider")
    base_url_input = _kv_control(page, "base_url")
    save_btn = page.locator("button", has_text="保存配置")

    # 先选 doubao 并保存（写入带地址的配置）
    provider_select.select_option("doubao")
    assert base_url_input.input_value() == "https://ark.cn-beijing.volces.com/api/v3"
    save_btn.click()
    page.wait_for_timeout(900)  # 等待保存 + setTimeout(renderConfig, 400) 重渲染完成

    # 改选 azure_openai（base_url 联动为空串）并保存
    provider_select.select_option("azure_openai")
    assert base_url_input.input_value() == "", "azure_openai 默认 base_url 应为空串"
    save_btn.click()
    page.wait_for_timeout(900)

    # 回读：provider 为 azure_openai，base_url 为空，不残留 doubao 地址
    cfg = read_config()
    assert cfg["image"]["provider"] == "azure_openai"
    assert cfg["image"]["base_url"] == ""
    assert page_errors == [], f"配置页抛 JS 异常：{page_errors}"


def test_p8_writer_model_provider_linkage_and_key_not_echoed(browser_session):
    """JEE-1946 真浏览器验证：写作模型 provider 下拉联动默认值，api_key 不回显。"""
    import json
    import urllib.request

    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    def read_config():
        data = urllib.request.urlopen(f"{base}/api/v1/config", timeout=5)
        return json.loads(data.read().decode("utf-8"))

    page.goto(f"{base}/#/config", wait_until="networkidle")
    page.wait_for_timeout(300)

    # 「写作模型（文章生成）」区块存在
    assert "写作模型（文章生成）" in page.inner_text("#app")

    writer_provider = _kv_control(page, "模型提供商")
    assert writer_provider.count() == 1, "写作模型 provider 应为下拉选择框"
    options = [o.strip() for o in writer_provider.locator("option").all_inner_texts()]
    assert "自定义…" in options
    assert "deepseek" in options and "openai" in options

    # 选 openai → model/base_url 联动为默认值
    writer_provider.select_option("openai")
    model_input = _kv_control(page, "模型名称")
    base_url_input = _kv_control(page, "接口地址")
    assert model_input.input_value() == "gpt-4o"
    assert base_url_input.input_value() == "https://api.openai.com/v1"

    # 切到 deepseek 同步刷新
    writer_provider.select_option("deepseek")
    assert model_input.input_value() == "deepseek-chat"
    assert base_url_input.input_value() == "https://api.deepseek.com"

    # api_key 只写不读：填密钥保存后 GET /config 无明文、无末四位
    api_key_input = _kv_control(page, "API 密钥")
    secret = "SK-WRITER-BROWSER-1234567890"
    api_key_input.fill(secret)
    save_btn = page.locator("button", has_text="保存配置")
    save_btn.click()
    page.wait_for_timeout(900)

    cfg = read_config()
    assert cfg["writer"]["api_key_configured"] is True
    assert "api_key" not in cfg["writer"]
    assert secret not in json.dumps(cfg)
    assert "1234567890" not in json.dumps(cfg)
    assert page_errors == [], f"配置页抛 JS 异常：{page_errors}"


def test_p8_writer_numeric_validation_blocks_submit(browser_session):
    """JEE-1946 返修回归：max_tokens=1.5 / temperature=3 保存时不发请求且提示错误。"""
    import json
    import urllib.request

    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    def read_config():
        data = urllib.request.urlopen(f"{base}/api/v1/config", timeout=5)
        return json.loads(data.read().decode("utf-8"))

    page.goto(f"{base}/#/config", wait_until="networkidle")
    page.wait_for_timeout(300)

    max_tokens_input = _kv_control(page, "最大 token 数")
    temp_input = _kv_control(page, "温度（0–2）")
    save_btn = page.locator("button", has_text="保存配置")

    before = read_config()["writer"]["max_tokens"]

    # max_tokens=1.5：不发起请求，就地报错
    max_tokens_input.fill("1.5")
    save_btn.click()
    page.wait_for_timeout(400)
    assert "最大 token 数必须为正整数" in page.inner_text("#app")
    assert read_config()["writer"]["max_tokens"] == before, "非法 max_tokens 不应落盘"

    # temperature=3：不发起请求，就地报错
    temp_input.fill("3")
    save_btn.click()
    page.wait_for_timeout(400)
    assert "温度（0–2）必须是 0 到 2 之间的数字" in page.inner_text("#app")
    assert page_errors == [], f"配置页抛 JS 异常：{page_errors}"


def test_p8_style_yaml_editor_render_and_save(browser_session):
    """JEE-1947 真浏览器验证：整文件 YAML 编辑器渲染，参考格式粘贴保存成功且原文保真。"""
    import json
    import urllib.request

    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    def read_style():
        data = urllib.request.urlopen(f"{base}/api/v1/style", timeout=5)
        return json.loads(data.read().decode("utf-8"))

    page.goto(f"{base}/#/config", wait_until="networkidle")
    page.wait_for_timeout(400)

    # 1) 整文件 YAML 编辑器 textarea 渲染，四个操作按钮齐全
    editor = page.locator("textarea.style-yaml-editor")
    assert editor.count() == 1, "应渲染 YAML 全文编辑器"
    for btn in ("加载当前配置", "智能生成", "校验"):
        assert page.locator("button", has_text=btn).count() >= 1, f"缺少按钮 {btn}"
    save_btns = page.locator("button").evaluate_all(
        "(els) => els.map((e) => e.textContent.trim()).filter((t) => t === '保存')"
    )
    assert "保存" in save_btns, "缺少「保存」按钮"

    # 2) 参考格式（blacklist 对象 + word_count 字符串 + reference_accounts）原样粘贴保存成功
    reference = (
        "# 风格配置\n"
        "name: 社会现象心理分析\n"
        "blacklist:\n  words: [震惊]\n  topics: [宗教]\n"
        "word_count: \"1800-2600\"\n"
        "reference_accounts: []\n"
        "tone: 像朋友聊天\n"
    )
    editor.fill(reference)
    page.get_by_role("button", name="保存", exact=True).first.click()
    page.wait_for_timeout(900)

    saved = read_style()
    assert saved.get("word_count") == "1800-2600", f"word_count 应为字符串，实际 {saved.get('word_count')}"
    assert saved.get("reference_accounts") == [], "reference_accounts 应保留"
    assert isinstance(saved.get("blacklist"), dict), "blacklist 应为对象形态"
    # 原文保真：含注释的输入应逐字节回读
    assert saved.get("raw") == reference, "raw 应逐字节等于提交文本"
    assert page_errors == [], f"配置页抛 JS 异常：{page_errors}"


def test_p8_style_validate_error_shows_line(browser_session):
    """JEE-1947 真浏览器验证：校验坏 YAML 显示中文原因（不 500）。"""
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(f"{base}/#/config", wait_until="networkidle")
    page.wait_for_timeout(400)

    editor = page.locator("textarea.style-yaml-editor")
    editor.fill("name: ok\n  bad-indent: [unclosed\n")
    page.locator("button", has_text="校验").click()
    page.wait_for_timeout(900)

    body_text = page.inner_text("#app")
    assert "校验失败" in body_text or "YAML" in body_text or "语法" in body_text
    assert page_errors == [], f"配置页抛 JS 异常：{page_errors}"


def _write_hotspot_snapshot(home: str, date: str, items: list) -> None:
    """直接写热榜快照 fixture（含 item_id，供 P4 真浏览器用例使用）。"""
    import json

    from app.adapters import hotspots as hotspots_adapter

    d = os.path.join(home, "hotspots")
    os.makedirs(d, exist_ok=True)
    for it in items:
        it["item_id"] = hotspots_adapter.hotspot_item_id(it)
    snap = {
        "version": 1,
        "date": date,
        "captured_at_utc": "2026-10-26T01:00:00+00:00",
        "captured_at_local": "2026-10-26T09:00:00+08:00",
        "sources": ["weibo", "toutiao", "baidu"],
        "sources_failed": [],
        "last_success_at": "2026-10-26T01:00:00+00:00",
        "count": len(items),
        "error": None,
        "items": items,
        "stale_sources": [],
    }
    with open(os.path.join(d, f"{date}.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)


def _p4_items(n=6):
    """造 n 条三源热点，带稳定 url。"""
    items = []
    for i in range(n):
        source = ["微博", "今日头条", "百度"][i % 3]
        source_id = {"微博": "weibo", "今日头条": "toutiao", "百度": "baidu"}[source]
        items.append({
            "title": f"热点标题第{i + 1}条",
            "source": source,
            "source_id": source_id,
            "hot": (n - i) * 100 + 10,
            "url": f"https://example.invalid/{i}",
            "description": f"切入角度{i + 1}",
        })
    return items


def test_p4_hotspot_selection_generate_and_unique_confirm(browser_session):
    """JEE-1962 真浏览器验证：P4 勾选 2 条 →「生成候选（已选 2 条）」可用、
    「确认选题」禁用；只勾 1 条 →「确认选题」可用。"""
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    home = browser_session["home"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    _write_hotspot_snapshot(home, "2026-10-26", _p4_items(6))
    page.goto(f"{base}/#/hotspots", wait_until="networkidle")
    page.wait_for_timeout(500)

    gen_btn = page.locator("button", has_text="生成候选（已选")
    confirm_btn = page.locator("button", has_text="确认选题（唯一）")
    assert gen_btn.count() == 1, "应有「生成候选（已选 N 条）」按钮"
    assert confirm_btn.count() == 1, "应有「确认选题（唯一）」按钮"

    # 初始：0 条选中 → 生成禁用、确认禁用
    assert gen_btn.is_disabled(), "0 条选中时生成候选应禁用"
    assert confirm_btn.is_disabled(), "0 条选中时确认选题应禁用"

    # 勾选 2 条 → 生成可用、确认禁用并提示只能选一条
    checkboxes = page.locator("ul.run-list input[type=checkbox]")
    assert checkboxes.count() == 6, f"应有 6 个勾选框，实际 {checkboxes.count()}"
    checkboxes.nth(0).check()
    checkboxes.nth(1).check()
    page.wait_for_timeout(200)
    assert not gen_btn.is_disabled(), "勾选 2 条时生成候选应可用"
    assert "生成候选（已选 2 条）" in gen_btn.inner_text()
    assert confirm_btn.is_disabled(), "勾选 2 条时确认选题应禁用"
    assert "选题只能选一条" in page.inner_text("#app")

    # 取消一条 → 只勾 1 条 → 确认选题可用
    checkboxes.nth(1).uncheck()
    page.wait_for_timeout(200)
    assert confirm_btn.is_enabled(), "只勾 1 条时确认选题应可用"
    assert page_errors == [], f"P4 勾选交互抛 JS 异常：{page_errors}"


def test_p4_hotspot_generate_selected_writes_candidate_set(browser_session):
    """JEE-1962 真浏览器验证：勾选 2 条点「生成候选」→ 按 hotspot_ids 生成候选集并跳转 P5。"""
    import json
    import urllib.request

    ctx = browser_session["ctx"]
    base = browser_session["base"]
    home = browser_session["home"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    _write_hotspot_snapshot(home, "2026-10-26", _p4_items(6))
    page.goto(f"{base}/#/hotspots", wait_until="networkidle")
    page.wait_for_timeout(500)

    checkboxes = page.locator("ul.run-list input[type=checkbox]")
    checkboxes.nth(0).check()
    checkboxes.nth(1).check()
    page.wait_for_timeout(200)

    # 点「生成候选（已选 2 条）」→ 跳转到 P5
    page.locator("button", has_text="生成候选（已选").click()
    page.wait_for_timeout(900)

    assert "#/candidates" in page.url, f"应跳转到 P5，实际 {page.url}"
    # 候选集已落盘：items 数量 == 2
    data = json.loads(urllib.request.urlopen(f"{base}/api/v1/candidates", timeout=5).read().decode("utf-8"))
    sets = data.get("candidates") or []
    assert len(sets) >= 1, "应生成至少一个候选集"
    latest = sets[0]
    assert len(latest["items"]) == 2, f"按 2 条热点生成应为 2 条候选，实际 {len(latest['items'])}"
    assert page_errors == [], f"P4 生成候选交互抛 JS 异常：{page_errors}"


def test_p4_confirm_selected_opens_confirm_modal_one_step(browser_session):
    """返修回归（阻断 1）：只勾选 1 条点「确认选题」→ 一步打开该条候选确认弹层。

    弹层含「确认选题」标题、图片上限/预算输入与 allow_publish 文案，路由带
    candidate_set_id/candidate_id（#/candidates/<set>/<candidate>）。
    """
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    home = browser_session["home"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    _write_hotspot_snapshot(home, "2026-10-26", _p4_items(6))
    page.goto(f"{base}/#/hotspots", wait_until="networkidle")
    page.wait_for_timeout(500)

    # 模块级 fixture 共享 JS 状态：先清空上一条用例残留的勾选，保证本用例从 0 开始
    page.locator("button", has_text="清空选择").click()
    page.wait_for_timeout(200)

    # 只勾选 1 条 → 确认选题可用
    checkboxes = page.locator("ul.run-list input[type=checkbox]")
    checkboxes.nth(0).check()
    page.wait_for_timeout(200)

    page.locator("button", has_text="确认选题（唯一）").click()
    page.wait_for_timeout(900)

    # 路由带候选集/候选 id
    assert "#/candidates/" in page.url, f"应带候选集/候选 id 跳转，实际 {page.url}"
    # 确认弹层已打开（标题 + 图片上限/预算 + allow_publish 文案）
    body_text = page.inner_text("#app")
    assert "确认选题" in body_text, "应打开确认弹层"
    assert "图片上限（0–10）" in body_text, "确认弹层应有图片上限输入"
    assert "预算（元）" in body_text, "确认弹层应有预算输入"
    assert "allow_publish=false" in body_text, "确认弹层应展示 allow_publish=false"
    # 弹层里的「确认生成」按钮存在
    assert page.locator("button", has_text="确认生成").count() == 1, "应有「确认生成」按钮"
    assert page_errors == [], f"P4 确认选题一步打开弹层抛 JS 异常：{page_errors}"


def test_p4_confirm_selected_no_confirmable_candidate_shows_hint(browser_session):
    """返修回归（阻断 1 已有候选分支）：该 URL 只有已确认/已关闭候选时，点「确认选题」
    必须给出明确的「不可确认」中文提示，不得静默退回候选列表。

    用唯一 url 构造快照，避免被共享 browser 上下文中其它 P4 用例生成的开集候选污染。
    """
    import json

    ctx = browser_session["ctx"]
    base = browser_session["base"]
    home = browser_session["home"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # 唯一 url，仅本用例使用
    item = {
        "title": "唯一热点标题",
        "source": "微博",
        "source_id": "weibo",
        "hot": 999,
        "url": "https://example.invalid/jee1962-no-confirm",
        "description": "切入角度",
    }
    _write_hotspot_snapshot(home, "2026-10-26", [item])

    # 直写一个「已确认」候选集：source_url 对应该热点，set_status=confirmed（无可确认候选）
    cand_dir = os.path.join(home, "candidates")
    os.makedirs(cand_dir, exist_ok=True)
    cid = "2026-10-26-aaaaaaaacdef"
    candidate_set = {
        "version": 1,
        "candidate_set_id": cid,
        "set_status": "confirmed",
        "created_at": "2026-10-26T08:00:00Z",
        "expires_at": "2026-10-27T08:00:00Z",
        "timezone": "Asia/Shanghai",
        "snapshot_sha256": "sha256:deadbeef",
        "style_revision": "sha256:web-default",
        "items": [{
            "id": "c01",
            "status": "confirmed",
            "title": "唯一热点标题",
            "angle": "解读",
            "scores": {"hot": 999, "hot_normalized": 100.0, "relevance": 80.0, "value": 70.0, "total": 79.0},
            "framework": "问题-证据-行动",
            "source": "微博",
            "source_id": "weibo",
            "source_url": "https://example.invalid/jee1962-no-confirm",
            "history_flag": False,
            "content_hash": "sha256:deadbeef",
            "run_id": "20261026-120000-abcdef",
            "job_id": "j-test000001",
        }],
    }
    with open(os.path.join(cand_dir, f"{cid}.json"), "w", encoding="utf-8") as f:
        json.dump(candidate_set, f, ensure_ascii=False)

    page.goto(f"{base}/#/hotspots", wait_until="networkidle")
    page.wait_for_timeout(500)

    # 模块级 fixture 共享 JS 状态：先清空残留勾选
    page.locator("button", has_text="清空选择").click()
    page.wait_for_timeout(200)

    # 只勾选该热点（其候选已确认、无 open/proposed 候选）
    checkboxes = page.locator("ul.run-list input[type=checkbox]")
    checkboxes.nth(0).check()
    page.wait_for_timeout(200)

    page.locator("button", has_text="确认选题（唯一）").click()
    page.wait_for_timeout(600)

    # 仍在 P4（未静默跳转），且给出明确的不可确认中文提示
    assert "#/hotspots" in page.url, f"不应静默跳转，实际 {page.url}"
    body_text = page.inner_text("#app")
    assert "无法再次确认" in body_text, "应提示该热点候选均已确认/关闭无法再次确认"
    assert "选题确认" in body_text or "生成候选" in body_text, "提示应含下一步动作指引"
    assert page_errors == [], f"P4 不可确认分支抛 JS 异常：{page_errors}"
