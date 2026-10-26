"""测试共享 fixture：造最小 WEWRITE_HOME（含 3 种 run 状态）。"""

from __future__ import annotations

import os
import yaml

import pytest

from app.main import create_app


def _write_run(home, run_id, status, *, mode="draft", title="", artifacts=None, extra=None):
    run_dir = os.path.join(home, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    state = {
        "version": 4,
        "run_id": run_id,
        "created": "2026-10-26T08:00:00+08:00",
        "updated": "2026-10-26T09:00:00+08:00",
        "status": status,
        "mode": mode,
        "permissions": {"publish": False},
        "visual": {"mode": "none", "max_images": 4, "max_cost": None},
        "topic": {
            "title": title or f"测试任务 {run_id}",
            "keywords": ["测试"],
            "source": "微博",
            "framework_hint": "问题-证据-行动",
        },
        "artifacts": {},
        "steps": {},
        "last_error": None,
    }
    state.update(extra or {})
    with open(os.path.join(run_dir, "state.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)

    for name, rel in {
        "brief": "brief.yaml",
        "article": "article.md",
        "draft": "draft.md",
        "illustrated_article": "article-illustrated.md",
        "review_report": "review-report.json",
        "images_manifest": "images.json",
        "sources": "sources.yaml",
        "preview": "preview.html",
    }.items():
        if artifacts and artifacts.get(name):
            path = os.path.join(run_dir, rel)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {name} 内容\nrun_id: {run_id}\n")
    return run_dir


def make_three_state_home(base: str) -> str:
    """造 3 种 run 状态：active（进行中）、failed（失败）、completed（已完成）。"""
    home = os.path.join(base, "WEWRITE_HOME")
    os.makedirs(os.path.join(home, "runs"), exist_ok=True)

    # 进行中：只有 sources
    _write_run(home, "20261026-080000-a1b2c3", "active", title="进行中的选题", artifacts={"sources": True})
    # 失败：有 draft + review_report
    _write_run(
        home,
        "20261026-090000-d4e5f6",
        "failed",
        title="审稿未通过的文章",
        artifacts={"draft": True, "review_report": True, "sources": True},
        extra={"last_error": "审稿未通过", "steps": {"review": {"status": "failed", "error": "审稿未通过", "updated": "2026-10-26T09:00:00+08:00"}}},
    )
    # 已完成：全产物
    _write_run(
        home,
        "20261026-100000-abcdef",
        "completed",
        mode="complete",
        title="已完成并发布候选的文章",
        artifacts={
            "brief": True,
            "article": True,
            "draft": True,
            "illustrated_article": True,
            "review_report": True,
            "images_manifest": True,
            "sources": True,
            "preview": True,
        },
        extra={
            "permissions": {"publish": True},
            "editorial": {"decision": "pass", "publishable": True},
            "seo": {"title": "已完成并发布候选的文章", "digest": "一段摘要", "tags": ["A"], "quality_score": 90},
            "publish": {"status": "succeeded", "media_id": "media-123456"},
            "steps": {
                "topic": {"status": "completed", "updated": "2026-10-26T10:00:00+08:00"},
                "write": {"status": "completed", "updated": "2026-10-26T10:10:00+08:00"},
            },
        },
    )
    return home


@pytest.fixture()
def three_state_home(tmp_path):
    return make_three_state_home(str(tmp_path))


@pytest.fixture()
def client(three_state_home, monkeypatch):
    """带合法 Host 的测试客户端；WEWRITE_HOME 指向 fixture。"""
    monkeypatch.setenv("WEWRITE_HOME", three_state_home)
    app = create_app()
    from fastapi.testclient import TestClient

    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update({"host": "127.0.0.1:8765"})
    return c


def auth_headers(client) -> dict:
    """取 bootstrap CSRF token 并构造同源变更请求头（供后续里程碑/测试复用）。"""
    r = client.get("/api/v1/bootstrap")
    token = r.json()["csrf_token"]
    return {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:8765",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": token,
    }
