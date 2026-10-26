"""M2 P4 热榜测试：快照落盘/读取、全源失败不覆盖、中英来源分离、
日期/limit 校验、refresh=1 同源校验（fake fetcher 计数为零）。"""

from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from app.domain import errors
from app.main import create_app


class FakeFetcher:
    """可计数的假 fetcher，用于验证「refresh=1 被拒时零出网」。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def fetch(self, limit):
        self.calls += 1
        return self.payload


class TimeoutFetcher:
    """模拟超时：抛 UPSTREAM_ERROR（reason=timeout）。"""

    def __init__(self):
        self.calls = 0

    def fetch(self, limit):
        self.calls += 1
        raise errors.upstream_error("热榜抓取超时", {"reason": "timeout"})


def ok_payload():
    return {
        "timestamp": "2026-10-26T09:00:00+08:00",
        "sources": ["weibo", "toutiao"],
        "sources_failed": ["baidu"],
        "count": 2,
        "items": [
            {"title": "热搜一", "source": "微博", "hot": 1000, "hot_normalized": 100.0, "url": "https://a", "description": ""},
            {"title": "头条一", "source": "今日头条", "hot": 2000, "hot_normalized": 98.0, "url": "https://b", "description": ""},
        ],
    }


def all_failed_payload():
    return {
        "timestamp": "2026-10-26T09:00:00+08:00",
        "sources": [],
        "sources_failed": ["weibo", "baidu", "toutiao"],
        "count": 0,
        "items": [],
        "error": "All sources failed. SKILL.md should fall back to WebSearch.",
    }


def make_client(monkeypatch, home, fetcher):
    monkeypatch.setenv("WEWRITE_HOME", home)
    app = create_app()
    app.state.hotspot_fetcher = fetcher
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update({"host": "127.0.0.1:8765"})
    return c


def post_headers(client) -> dict:
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    return {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:8765",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": token,
    }


def test_get_hotspots_empty_snapshot(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get("/api/v1/hotspots")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["items"] == []
    assert fetcher.calls == 0  # 不带 refresh 不抓取


def test_post_refresh_writes_snapshot(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.post("/api/v1/hotspots/refresh", json={"date": "2026-10-26", "limit": 20}, headers=post_headers(c))
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["sources"] == ["weibo", "toutiao"]
    assert body["sources_failed"] == ["baidu"]
    assert fetcher.calls == 1

    snap_path = os.path.join(str(tmp_path), "hotspots", "2026-10-26.json")
    assert os.path.exists(snap_path)
    with open(snap_path, encoding="utf-8") as f:
        snap = json.load(f)
    assert snap["date"] == "2026-10-26"
    assert snap["sources"] == ["weibo", "toutiao"]
    assert snap["count"] == 2
    # 中英分离：items 同时带中文 source 与英文 source_id
    assert snap["items"][0]["source"] == "微博"
    assert snap["items"][0]["source_id"] == "weibo"
    assert snap["items"][1]["source"] == "今日头条"
    assert snap["items"][1]["source_id"] == "toutiao"
    assert snap["last_success_at"]  # 成功落盘更新 last_success_at
    assert snap["captured_at_utc"] and snap["captured_at_local"]


def test_get_hotspots_reads_saved_snapshot(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    c.post("/api/v1/hotspots/refresh", json={"date": "2026-10-26"}, headers=post_headers(c))
    fetcher.calls = 0

    r = c.get("/api/v1/hotspots")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["date"] == "2026-10-26"
    assert body["items"][0]["source_id"] == "weibo"
    assert fetcher.calls == 0  # 读快照不出网


def test_all_failed_does_not_overwrite_snapshot(tmp_path, monkeypatch):
    home = str(tmp_path)
    ok = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, home, ok)
    c.post("/api/v1/hotspots/refresh", json={"date": "2026-10-26"}, headers=post_headers(c))
    with open(os.path.join(home, "hotspots", "2026-10-26.json"), encoding="utf-8") as f:
        before = f.read()

    fail = FakeFetcher(all_failed_payload())
    c.app.state.hotspot_fetcher = fail
    r = c.post("/api/v1/hotspots/refresh", json={"date": "2026-10-26"}, headers=post_headers(c))
    assert r.status_code == 200  # 全失败以业务字段表达，不抛 HTTP 500
    body = r.json()
    assert body.get("error")
    assert body["count"] == 0
    assert fail.calls == 1

    with open(os.path.join(home, "hotspots", "2026-10-26.json"), encoding="utf-8") as f:
        after = f.read()
    assert before == after  # 不覆盖上一份成功快照


def test_refresh_get_same_origin_allowed_and_counted(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get(
        "/api/v1/hotspots",
        params={"limit": 20, "refresh": 1},
        headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200
    body = r.json()
    for field in ("timestamp", "sources", "sources_failed", "count", "items"):
        assert field in body
    assert fetcher.calls == 1
    # refresh=1 不写快照
    assert not os.path.exists(os.path.join(str(tmp_path), "hotspots"))


def test_refresh_get_missing_headers_rejected_zero_fetch(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get("/api/v1/hotspots", params={"limit": 20, "refresh": 1})
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"
    assert r.json()["details"]["reason"] == "refresh_requires_same_origin"
    assert fetcher.calls == 0


def test_refresh_get_cross_site_rejected_zero_fetch(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get(
        "/api/v1/hotspots",
        params={"limit": 20, "refresh": 1},
        headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"
    assert fetcher.calls == 0


def test_refresh_get_same_site_rejected_zero_fetch(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get(
        "/api/v1/hotspots",
        params={"limit": 20, "refresh": 1},
        headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-site"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"
    assert fetcher.calls == 0


def test_refresh_get_wrong_origin_port_rejected(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get(
        "/api/v1/hotspots",
        params={"limit": 20, "refresh": 1},
        headers={"Origin": "http://127.0.0.1:9999", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"
    assert fetcher.calls == 0


def test_refresh_get_browser_shape_no_origin_with_referer_allowed(tmp_path, monkeypatch):
    """阻断 A 核心回归：真实浏览器同源 GET 不带 Origin，只有
    Sec-Fetch-Site: same-origin + Referer 同源 → 200 且 fetcher 计数 1。"""
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get(
        "/api/v1/hotspots",
        params={"limit": 20, "refresh": 1},
        headers={"Sec-Fetch-Site": "same-origin", "Referer": "http://127.0.0.1:8765/page1.html"},
    )
    assert r.status_code == 200
    assert fetcher.calls == 1


def test_refresh_get_no_origin_cross_site_referer_rejected(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get(
        "/api/v1/hotspots",
        params={"limit": 20, "refresh": 1},
        headers={"Sec-Fetch-Site": "same-origin", "Referer": "http://evil.example/page.html"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"
    assert fetcher.calls == 0


def test_hotspots_dir_symlink_rejected_reads(tmp_path, monkeypatch):
    """阻断 B：hotspots/ 整体换成 symlink，三个读端点均 403 且读不到诱饵。"""
    home = str(tmp_path)
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, home, fetcher)
    # 先正常写一份快照并钉住状态根身份
    c.post("/api/v1/hotspots/refresh", json={"date": "2026-10-26"}, headers=post_headers(c))
    c.get("/api/v1/runs")

    real = os.path.join(home, "hotspots")
    os.rename(real, os.path.join(home, "hotspots.real"))
    outside = os.path.join(home, "evil-hotspots")
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, "2026-10-26.json"), "w", encoding="utf-8") as f:
        f.write('{"date":"2026-10-26","count":99,"items":[{"title":"EVIL","source":"微博","source_id":"weibo"}]}')
    os.symlink(outside, real)

    for path in ("/api/v1/hotspots/2026-10-26", "/api/v1/hotspot-snapshots", "/api/v1/hotspots"):
        r = c.get(path)
        assert r.status_code == 403, path
        assert r.json()["code"] == "PATH_FORBIDDEN", path
        assert "EVIL" not in r.text


def test_hotspot_file_symlink_rejected(tmp_path, monkeypatch):
    """阻断 B：hotspots/<date>.json 叶文件换成 symlink → 403 且读不到外部内容。"""
    home = str(tmp_path)
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, home, fetcher)
    c.post("/api/v1/hotspots/refresh", json={"date": "2026-10-26"}, headers=post_headers(c))
    c.get("/api/v1/runs")

    snap = os.path.join(home, "hotspots", "2026-10-26.json")
    outside = os.path.join(home, "evil-leaf.json")
    with open(outside, "w", encoding="utf-8") as f:
        f.write('{"date":"2026-10-26","items":[{"title":"EVIL-LEAF"}]}')
    os.remove(snap)
    os.symlink(outside, snap)

    r = c.get("/api/v1/hotspots/2026-10-26")
    assert r.status_code == 403
    assert r.json()["code"] == "PATH_FORBIDDEN"
    assert "EVIL-LEAF" not in r.text


def test_hotspots_dir_symlink_rejected_write(tmp_path, monkeypatch):
    """阻断 B：hotspots/ 为 symlink 时 POST /refresh 拒绝且外部目录不产生文件。"""
    home = str(tmp_path)
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, home, fetcher)
    c.get("/api/v1/runs")  # 钉住状态根身份

    outside = os.path.join(home, "evil-write")
    os.makedirs(outside, exist_ok=True)
    os.symlink(outside, os.path.join(home, "hotspots"))

    r = c.post("/api/v1/hotspots/refresh", json={"date": "2026-10-26"}, headers=post_headers(c))
    assert r.status_code == 403
    assert r.json()["code"] == "PATH_FORBIDDEN"
    assert not os.path.exists(os.path.join(outside, "2026-10-26.json"))


def test_hotspot_timeout_fetcher_502(tmp_path, monkeypatch):
    """阻断 C：超时 fetcher → 502 UPSTREAM_ERROR，且快照目录不产生文件。"""
    fetcher = TimeoutFetcher()
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get(
        "/api/v1/hotspots",
        params={"limit": 20, "refresh": 1},
        headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 502
    assert r.json()["code"] == "UPSTREAM_ERROR"
    assert not os.path.exists(os.path.join(str(tmp_path), "hotspots"))


def test_hotspots_limit_validation(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    for bad in (0, 101):
        r = c.get("/api/v1/hotspots", params={"limit": bad})
        assert r.status_code == 422, bad
        assert r.json()["code"] == "VALIDATION_ERROR"


def test_post_refresh_date_validation(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.post("/api/v1/hotspots/refresh", json={"date": "not-a-date"}, headers=post_headers(c))
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert fetcher.calls == 0


def test_hotspot_snapshots_and_single_date(tmp_path, monkeypatch):
    home = str(tmp_path)
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, home, fetcher)
    c.post("/api/v1/hotspots/refresh", json={"date": "2026-10-26"}, headers=post_headers(c))

    r = c.get("/api/v1/hotspot-snapshots")
    assert r.status_code == 200
    assert r.json()["dates"] == ["2026-10-26"]

    r = c.get("/api/v1/hotspots/2026-10-26")
    assert r.status_code == 200
    assert r.json()["count"] == 2

    r = c.get("/api/v1/hotspots/2026-10-25")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


def test_hotspot_single_date_invalid(tmp_path, monkeypatch):
    fetcher = FakeFetcher(ok_payload())
    c = make_client(monkeypatch, str(tmp_path), fetcher)
    r = c.get("/api/v1/hotspots/not-a-date")
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
