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
    # 常见路径（含本机 chrome-cft 的捆绑库浏览器）
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
                yield {"ctx": ctx, "base": f"http://127.0.0.1:{port}"}
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
