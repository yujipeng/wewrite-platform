"""M2 P3 预览测试：converter 输出、脚本过滤、路径白名单、themes。"""

from __future__ import annotations

import os

from app.main import create_app
from app.storage import paths


def write_artifact(home, run_id, name, content):
    rel = {
        "draft": "draft.md",
        "article": "article.md",
        "illustrated_article": "article-illustrated.md",
    }[name]
    d = os.path.join(home, "runs", run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
        f.write(content)


def test_themes_listed(client):
    r = client.get("/api/v1/themes")
    assert r.status_code == 200
    themes = r.json()["themes"]
    assert isinstance(themes, list) and themes
    names = [t["name"] for t in themes]
    assert "professional-clean" in names
    assert all("name" in t and "description" in t for t in themes)


def test_preview_ok_and_filters_script(client, three_state_home):
    run_id = "20261026-100000-abcdef"
    write_artifact(
        three_state_home,
        run_id,
        "article",
        "# 预览标题\n\n正文段落 **加粗**\n\n<script>alert(1)</script>\n\n"
        "<img src=\"javascript:alert(2)\" onerror=\"x()\">\n\n<div onmouseover=\"y()\">内容</div>\n",
    )
    # 首次访问钉住状态根身份
    client.get("/api/v1/runs")
    r = client.post(
        "/api/v1/preview",
        json={"run_id": run_id, "source": "article", "theme": "professional-clean"},
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    for field in ("html", "paste_safe_html", "title", "digest", "source_sha256", "filtered_count"):
        assert field in body
    assert body["title"] == "预览标题"
    # 脚本/事件属性/javascript: 必须被剥离且计数 > 0
    html = body["html"]
    assert "<script" not in html.lower()
    assert "onerror" not in html.lower()
    assert "onmouseover" not in html.lower()
    assert "javascript:" not in html.lower()
    assert body["filtered_count"] > 0
    assert len(body["source_sha256"]) == 64


def test_preview_source_switching(three_state_home, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WEWRITE_HOME", three_state_home)
    app = create_app()
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update({"host": "127.0.0.1:8765"})
    token = c.get("/api/v1/bootstrap").json()["csrf_token"]
    headers = {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:8765",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": token,
    }
    run_id = "20261026-100000-abcdef"
    for name in ("draft", "article", "illustrated_article"):
        write_artifact(three_state_home, run_id, name, f"# {name} 标题\n\n{name} 正文\n")
    c.get("/api/v1/runs")  # 钉住身份

    shas = []
    for name in ("draft", "article", "illustrated_article"):
        r = c.post("/api/v1/preview", json={"run_id": run_id, "source": name, "theme": "professional-clean"}, headers=headers)
        assert r.status_code == 200, name
        body = r.json()
        assert body["title"] == f"{name} 标题"
        shas.append(body["source_sha256"])
    assert len(set(shas)) == 3  # 不同来源不同 hash


def test_preview_invalid_source_rejected(client):
    r = client.post(
        "/api/v1/preview",
        json={"run_id": "20261026-100000-abcdef", "source": "config.yaml", "theme": "professional-clean"},
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"],
        },
    )
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_preview_source_non_string_rejected_not_500(client):
    """阻断 C：source 传 dict/list 必须 422 而非 500。"""
    for bad in ({"a": 1}, ["article"], 123, None):
        r = client.post(
            "/api/v1/preview",
            json={"run_id": "20261026-100000-abcdef", "source": bad, "theme": "professional-clean"},
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:8765",
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"],
            },
        )
        assert r.status_code == 422, bad
        assert r.json()["code"] == "VALIDATION_ERROR"


def test_preview_unknown_theme_rejected(client):
    r = client.post(
        "/api/v1/preview",
        json={"run_id": "20261026-100000-abcdef", "source": "article", "theme": "../../etc"},
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"],
        },
    )
    assert r.status_code in {404, 422}


def test_preview_missing_artifact_not_found(client):
    r = client.post(
        "/api/v1/preview",
        json={"run_id": "20261026-080000-a1b2c3", "source": "article", "theme": "professional-clean"},
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"],
        },
    )
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


def test_preview_missing_artifact_reason_artifact_missing(client, three_state_home):
    """验收标准 2：只有 article、无 draft 的 run 请求 source=draft，
    返回 404 且 details.reason == artifact_missing、artifact == draft、run_id 回填。"""
    run_id = "20261026-080000-a1b2c3"
    write_artifact(three_state_home, run_id, "article", "# 只有终稿\n")
    client.get("/api/v1/runs")  # 钉住身份
    r = client.post(
        "/api/v1/preview",
        json={"run_id": run_id, "source": "draft", "theme": "professional-clean"},
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"],
        },
    )
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "NOT_FOUND"
    assert body["details"]["reason"] == "artifact_missing"
    assert body["details"]["artifact"] == "draft"
    assert body["details"]["run_id"] == run_id


def test_preview_theme_not_found_reason(client):
    """验收标准 4：合法格式但不在 list_themes() 的 theme → 404 theme_not_found。"""
    r = client.post(
        "/api/v1/preview",
        json={"run_id": "20261026-100000-abcdef", "source": "article", "theme": "no-such-theme-xyz"},
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"],
        },
    )
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "NOT_FOUND"
    assert body["details"]["reason"] == "theme_not_found"
    assert body["details"]["theme"] == "no-such-theme-xyz"


def test_preview_non_whitelisted_artifact_unchanged(client):
    """验收标准 5（部分）：白名单外产物名仍 404 artifact_not_whitelisted，不被误改。"""
    import pytest

    from app.domain import errors

    with pytest.raises(errors.ApiError) as ei:
        paths.safe_open_run_file(os.environ["WEWRITE_HOME"], "20261026-100000-abcdef", "config.yaml")
    assert ei.value.code == "NOT_FOUND"
    assert ei.value.details["reason"] == "artifact_not_whitelisted"
