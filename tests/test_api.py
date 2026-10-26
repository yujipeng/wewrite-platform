"""接口测试：runs 列表/详情投影、artifact、bootstrap/healthz、Host/CSRF 拒绝。"""

from __future__ import annotations

from conftest import auth_headers


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "0.1.0"}


def test_bootstrap(client):
    r = client.get("/api/v1/bootstrap")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"csrf_token", "version", "server_started_at"}
    assert body["version"] == "0.1.0"


def test_bootstrap_token_restart_invalidates():
    """token 只在进程内存，重启（重新 create_app）后旧 token 失效。"""
    import os

    from app.main import create_app
    from fastapi.testclient import TestClient

    os.environ["WEWRITE_HOME"] = "/tmp/unused-placeholder"

    app1 = create_app()
    c1 = TestClient(app1, base_url="http://127.0.0.1:8765")
    token1 = c1.get("/api/v1/bootstrap", headers={"host": "127.0.0.1:8765"}).json()["csrf_token"]

    app2 = create_app()
    c2 = TestClient(app2, base_url="http://127.0.0.1:8765")
    token2 = c2.get("/api/v1/bootstrap", headers={"host": "127.0.0.1:8765"}).json()["csrf_token"]

    assert token1 != token2
    # 旧 token 在 app2 里无效
    r = c2.post(
        "/api/v1/runs",
        json={},
        headers={
            "host": "127.0.0.1:8765",
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": token1,
        },
    )
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"


def test_runs_list_three_states(client):
    r = client.get("/api/v1/runs")
    assert r.status_code == 200
    runs = r.json()["runs"]
    ids = [x["run_id"] for x in runs]
    assert set(ids) == {
        "20261026-080000-a1b2c3",
        "20261026-090000-d4e5f6",
        "20261026-100000-abcdef",
    }
    # 倒序：created 相同，按 run_id 倒序
    assert ids[0] == "20261026-100000-abcdef"


def test_runs_list_artifact_badges_reflect_existence(client):
    r = client.get("/api/v1/runs")
    runs = {x["run_id"]: x for x in r.json()["runs"]}
    active = runs["20261026-080000-a1b2c3"]
    # 只有 sources 落盘
    assert active["artifacts"]["sources"] is True
    assert active["artifacts"]["article"] is False


def test_run_detail(client):
    r = client.get("/api/v1/runs/20261026-100000-abcdef")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "20261026-100000-abcdef"
    assert body["topic"]["title"] == "已完成并发布候选的文章"
    assert body["permissions"]["publish"] is True
    assert body["publish"]["status"] == "succeeded"
    assert body["publish"]["media_id"] == "media-123456"
    assert body["steps"]["topic"]["status"] == "completed"
    assert body["editorial"]["publishable"] is True
    assert body["seo"]["digest"] == "一段摘要"


def test_run_detail_not_found(client):
    r = client.get("/api/v1/runs/20261026-010101-000000")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


def test_run_detail_invalid_id(client):
    r = client.get("/api/v1/runs/not-a-valid-id")
    assert r.status_code == 400
    assert r.json()["code"] == "RUN_ID_INVALID"


def test_artifact_ok(client):
    r = client.get("/api/v1/runs/20261026-100000-abcdef/artifact/article")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "article"
    assert len(body["sha256"]) == 64
    assert "article" in body["content"]


def test_artifact_non_whitelisted(client):
    r = client.get("/api/v1/runs/20261026-100000-abcdef/artifact/config.yaml")
    assert r.status_code == 404


def test_artifact_missing(client):
    r = client.get("/api/v1/runs/20261026-080000-a1b2c3/artifact/article")
    assert r.status_code == 404


def test_bad_host_rejected(client):
    r = client.get("/healthz", headers={"host": "evil.example"})
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"


def test_good_hosts_accepted(client):
    for host in ["127.0.0.1:8765", "[::1]:8765", "localhost:8765", "localhost"]:
        r = client.get("/healthz", headers={"host": host})
        assert r.status_code == 200, host


def test_get_requires_no_csrf(client):
    """GET 只做 Host 校验，无需 CSRF token。"""
    r = client.get("/api/v1/runs")
    assert r.status_code == 200


def test_post_requires_csrf_and_same_origin(client):
    """POST 缺 CSRF/同源头时被拒。"""
    # 无任何头
    r = client.post("/api/v1/runs", json={})
    assert r.status_code in {403, 422, 404}
    if r.status_code == 403:
        assert r.json()["code"] == "CSRF_INVALID"

    # 同源但缺 token
    r = client.post(
        "/api/v1/runs",
        json={},
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"


def test_post_wrong_content_type(client):
    headers = auth_headers(client)
    headers["Content-Type"] = "text/plain"
    r = client.post("/api/v1/runs", content="{}", headers=headers)
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_post_cross_site_rejected(client):
    headers = auth_headers(client)
    headers["Origin"] = "http://evil.example"
    headers["Sec-Fetch-Site"] = "cross-site"
    r = client.post("/api/v1/runs", json={}, headers=headers)
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"
