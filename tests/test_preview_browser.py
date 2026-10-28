"""JEE-1948 P3 预览 404 语义化的真浏览器回归（Playwright + Chromium）。

覆盖完成定义要求的两个场景：
  1. 只有 article.md、无 draft.md 的 run 进 P3：默认选中「终稿」且渲染成功（无 404）；
  2. 无任何正文产物的 run 进 P3：「渲染预览」按钮禁用且有引导文案。

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
    # 优先用 chrome-cft 的 google-chrome 包装脚本（它会注入 libroot 里的系统库），
    # 再退回裸二进制与常见系统路径。
    candidates = [
        os.path.expanduser("~/.local/bin/google-chrome"),
        os.path.expanduser("~/.local/chrome-cft/chrome-linux64/chrome"),
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


def _wait_healthz(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("uvicorn 未在超时内就绪")


def _write_run(home: str, run_id: str, title: str, artifacts: dict) -> None:
    import yaml

    rundir = os.path.join(home, "runs", run_id)
    os.makedirs(rundir, exist_ok=True)
    state = {
        "version": 4,
        "run_id": run_id,
        "status": "completed",
        "mode": "complete",
        "created": "2026-10-28T10:00:00+08:00",
        "updated": "2026-10-28T11:00:00+08:00",
        "permissions": {"publish": False},
        "visual": {"mode": "none", "max_images": 4, "max_cost": None},
        "topic": {"title": title, "keywords": ["测试"], "source": "微博", "framework_hint": ""},
        "artifacts": {},
        "steps": {},
        "last_error": None,
    }
    with open(os.path.join(rundir, "state.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)
    rel = {
        "article": "article.md",
        "draft": "draft.md",
        "illustrated_article": "article-illustrated.md",
    }
    for name, ok in (artifacts or {}).items():
        if ok:
            with open(os.path.join(rundir, rel[name]), "w", encoding="utf-8") as f:
                f.write(f"# {title} 的 {name}\n\n正文内容\n")


@pytest.fixture(scope="module")
def browser_session(tmp_path_factory):
    chrome = _find_chromium()
    if chrome is None:
        pytest.skip("无可用 Chromium（可用 WEWRITE_PLAYWRIGHT_EXECUTABLE 指定）")

    home = str(tmp_path_factory.mktemp("preview-home"))
    os.makedirs(os.path.join(home, "runs"), exist_ok=True)
    _write_run(home, "20261028-101000-abc123", "只有终稿的任务", {"article": True})
    _write_run(home, "20261028-102000-def456", "无产物的任务", {})

    # Playwright 用 TMPDIR 做 mkdtemp；本机环境的 TMPDIR 可能指向不存在的目录，
    # 强制指向可写临时目录。
    pw_tmp = tmp_path_factory.mktemp("pw-tmp")
    os.environ["TMPDIR"] = str(pw_tmp)

    port = 8898
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
                yield {"ctx": ctx, "base": f"http://127.0.0.1:{port}"}
            finally:
                ctx.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_preview_only_article_defaults_to_article_and_renders(browser_session):
    """验收标准 1：只有 article.md 的 run 进 P3，默认选中终稿、draft 标未生成且不可选、渲染成功。"""
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(f"{base}/#/runs/20261028-101000-abc123/preview", wait_until="networkidle")
    page.wait_for_timeout(1200)

    source_select = page.locator("select").first
    assert source_select.input_value() == "article", "应默认选中第一个实际存在的来源（终稿）"
    assert page.locator("select option[value='draft']").first.is_disabled(), "draft 未生成应不可选"
    main_text = page.inner_text("#app")
    assert "（未生成）" in main_text, "来源下拉应标注未生成"
    assert page.locator("iframe.preview-frame").count() > 0, "正常路径应渲染出 iframe"
    assert "产物不存在或尚未生成" not in main_text, "不应出现旧的笼统 404 提示"
    assert page_errors == [], f"抛 JS 异常：{page_errors}"


def test_preview_no_artifacts_disables_button_with_guidance(browser_session):
    """验收标准 6：无任何产物的 run 进 P3，渲染按钮禁用且有引导文案。"""
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(f"{base}/#/runs/20261028-102000-def456/preview", wait_until="networkidle")
    page.wait_for_timeout(1200)

    render_btn = page.locator("button", has_text="渲染预览").first
    assert render_btn.is_disabled(), "无产物时渲染按钮应禁用"
    main_text = page.inner_text("#app")
    assert "该任务还没有可预览的正文" in main_text, "应显示引导文案"
    assert page_errors == [], f"抛 JS 异常：{page_errors}"


def test_preview_artifact_status_line_matches_artifacts(browser_session):
    """验收标准 8：P3 页显示「已有产物」一行，与实际 artifacts 一致。"""
    ctx = browser_session["ctx"]
    base = browser_session["base"]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    page.goto(f"{base}/#/runs/20261028-101000-abc123/preview", wait_until="networkidle")
    page.wait_for_timeout(800)
    main_text = page.inner_text("#app")
    assert "当前任务已有产物" in main_text, "应显示产物状态提示行"
    assert "终稿 ✓" in main_text, "article 存在应显示 ✓"
    assert "初稿 ✗" in main_text, "draft 缺失应显示 ✗"
