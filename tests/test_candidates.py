"""M3 P5 候选池与确认门测试。

覆盖：候选生成/幂等/全失败、confirm 并发（跨进程）、过期/hash/mode=publish 拒绝、
idempotency 重放、撤回后 run 仍存在、删除保护、原子写、candidates 目录 symlink 拒绝。
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.adapters import wewrite as wewrite_adapter
from app.domain import confirm_gate
from app.domain import scoring
from app.main import create_app
from app.storage import candidates as candidates_store


def snapshot_items(n=12):
    """造 n 条三源热榜条目（覆盖微博/今日头条/百度）。"""
    items = []
    for i in range(n):
        source = ["微博", "今日头条", "百度"][i % 3]
        source_id = {"微博": "weibo", "今日头条": "toutiao", "百度": "baidu"}[source]
        items.append(
            {
                "title": f"热点标题第{i + 1}条",
                "source": source,
                "source_id": source_id,
                "hot": (n - i) * 100 + 10,
                "url": f"https://example.invalid/{i}",
                "description": f"切入角度{i + 1}",
            }
        )
    return items


def ok_snapshot():
    items = snapshot_items()
    return {
        "version": 1,
        "date": "2026-10-26",
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


def ok_snapshot_variant():
    """同日期但内容不同的快照（用于验证 force 取代语义，snapshot hash 不同）。"""
    snap = ok_snapshot()
    items = [dict(it) for it in snap["items"]]
    # 改一条标题使 snapshot hash 不同
    items[0] = dict(items[0], title="变了标题的热点")
    snap["items"] = items
    return snap


class FakeWewriteAdapter(wewrite_adapter.WeWriteAdapter):
    """写最小 state.yaml 的假上游，替代真实 create_run。"""

    def __init__(self, home: str):
        self.home = home
        self.calls = 0

    def create_run(self, *, topic, mode, visual_mode, max_images, max_image_cost):
        self.calls += 1
        run_id = "20261026-%06d-%s" % (self.calls, secrets.token_hex(3))
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        import yaml

        state = {
            "version": 4,
            "run_id": run_id,
            "created": "2026-10-26T10:00:00+08:00",
            "updated": "2026-10-26T10:00:00+08:00",
            "status": "active",
            "mode": mode,
            "permissions": {"publish": False},
            "visual": {"mode": visual_mode, "max_images": max_images, "max_cost": max_image_cost},
            "topic": {"title": topic, "keywords": [], "source": "微博", "framework_hint": ""},
            "artifacts": {},
            "steps": {},
            "last_error": None,
        }
        with open(os.path.join(run_dir, "state.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)
        return state


def make_client(monkeypatch, home, adapter=None):
    monkeypatch.setenv("WEWRITE_HOME", home)
    app = create_app()
    if adapter is not None:
        app.state.wewrite_adapter = adapter
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update({"host": "127.0.0.1:8765"})
    return c


def post_headers(client, extra=None):
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    h = {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:8765",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": token,
    }
    h.update(extra or {})
    return h


def write_snapshot(home, date, snapshot):
    """直接写快照文件（fixture 用，测试态不经过 API）。"""
    d = os.path.join(home, "hotspots")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{date}.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False)


def generate_set(client, home, date="2026-10-26", style="sha256:test", force=False):
    write_snapshot(home, date, ok_snapshot())
    r = client.post(
        "/api/v1/candidates",
        json={"date": date, "style_revision": style, "force": force},
        headers=post_headers(client),
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 生成 ----------

def test_generate_candidates_writes_10_items(tmp_path, monkeypatch):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    body = generate_set(c, home)
    assert body["set_status"] == "open"
    assert len(body["items"]) == 10
    # 总分降序
    totals = [it["scores"]["total"] for it in body["items"]]
    assert totals == sorted(totals, reverse=True)
    # 文件落盘
    files = [f for f in os.listdir(os.path.join(home, "candidates")) if f.endswith(".json")]
    assert len(files) == 1
    # 无临时文件残留
    assert not [f for f in os.listdir(os.path.join(home, "candidates")) if f.endswith(".tmp")]


def test_candidate_file_named_by_snapshot_sha256(tmp_path, monkeypatch):
    """落盘文件名 == <date>-<snapshot_sha256 前 12 位>.json（阻断项修复回归）。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    body = generate_set(c, home)
    snap_hash = body["snapshot_sha256"]
    digest = snap_hash.replace("sha256:", "")
    expected = f"2026-10-26-{digest[:12]}.json"
    # candidate_set_id 与文件名一致
    assert body["candidate_set_id"] == expected[:-5]
    assert os.path.exists(os.path.join(home, "candidates", expected))
    # candidates 目录只有这一个文件，且无随机 token 文件
    files = [f for f in os.listdir(os.path.join(home, "candidates")) if f.endswith(".json")]
    assert files == [expected]


def test_generate_idempotent_same_hash_style(tmp_path, monkeypatch):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    first = generate_set(c, home)
    second = generate_set(c, home)
    assert first["candidate_set_id"] == second["candidate_set_id"]
    files = [f for f in os.listdir(os.path.join(home, "candidates")) if f.endswith(".json")]
    assert len(files) == 1


def test_generate_force_supersedes_old(tmp_path, monkeypatch):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    first = generate_set(c, home)

    # 同日期、不同 style_revision 且不带 force → 另开新集需 force，拒绝
    r = c.post(
        "/api/v1/candidates",
        json={"date": "2026-10-26", "style_revision": "sha256:other"},
        headers=post_headers(c),
    )
    assert r.status_code == 422
    assert r.json()["details"]["reason"] == "set_exists_requires_force"

    # 显式 force + 内容不同的快照 → 写新集（不同 snapshot hash → 不同文件名）并把旧 open 集置 superseded
    write_snapshot(home, "2026-10-26", ok_snapshot_variant())
    r = c.post(
        "/api/v1/candidates",
        json={"date": "2026-10-26", "style_revision": "sha256:third", "force": True},
        headers=post_headers(c),
    )
    assert r.status_code == 200, r.text
    third = r.json()

    r = c.get("/api/v1/candidates")
    by_id = {s["candidate_set_id"]: s["set_status"] for s in r.json()["candidates"]}
    assert by_id[first["candidate_set_id"]] == "superseded"
    assert by_id[third["candidate_set_id"]] == "open"


def test_force_rebuild_open_set_writes_traceable_audit(tmp_path, monkeypatch):
    """同快照哈希、旧集为未确认 open 集时 force 原地重建成功，审计有可追溯替换记录。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    first = generate_set(c, home)

    # 同快照、不同 style_revision、force=true → 原地重建（同一文件名）
    r = c.post(
        "/api/v1/candidates",
        json={"date": "2026-10-26", "style_revision": "sha256:new-style", "force": True},
        headers=post_headers(c),
    )
    assert r.status_code == 200, r.text
    rebuilt = r.json()
    assert rebuilt["candidate_set_id"] == first["candidate_set_id"]
    assert rebuilt["style_revision"] == "sha256:new-style"

    # 审计里有 candidate_set_rebuilt 事件，且记录被替换集的 cid/created_at/style_revision/snapshot_sha256
    # 审计文件名按「当天 UTC」命名（与 _audit() 同口径），不硬编码业务日期 2026-10-26。
    audit_path = os.path.join(home, "audit", datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")
    assert os.path.exists(audit_path)
    lines = [json.loads(line) for line in open(audit_path, encoding="utf-8") if line.strip()]
    rebuilt_events = [e for e in lines if e.get("event") == "candidate_set_rebuilt"]
    assert rebuilt_events, "缺 candidate_set_rebuilt 审计"
    ev = rebuilt_events[-1]
    assert ev["cid"] == first["candidate_set_id"]
    assert ev["created_at"] == first["created_at"]
    assert ev["style_revision"] == first["style_revision"]
    assert ev["snapshot_sha256"] == first["snapshot_sha256"]


def test_force_confirmed_set_rejected_unchanged(tmp_path, monkeypatch):
    """同快照哈希、不同 style_revision 的 force 在旧集已确认时被拒（409 set_closed），
    且旧集 set_status、item 状态、run_id 一字不变（阻断项回归）。"""
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    # 先确认一个候选 → 集合转 confirmed，item 带 run_id/job_id
    confirmed = _confirm(c, cid, candidate, "key-force-confirmed").json()
    run_id = confirmed["run_id"]

    store = candidates_store.CandidateStore(home)
    before = store.load(cid)

    # 同快照、不同 style_revision、force=true → 必须 409 set_closed
    r = c.post(
        "/api/v1/candidates",
        json={"date": "2026-10-26", "style_revision": "sha256:new-style", "force": True},
        headers=post_headers(c),
    )
    assert r.status_code == 409
    assert r.json()["code"] == "CANDIDATE_NOT_ACTIONABLE"
    assert r.json()["details"]["reason"] == "set_closed"

    # 旧集一字不变
    after = store.load(cid)
    assert after["set_status"] == before["set_status"] == "confirmed"
    before_items = {it["id"]: it for it in before["items"]}
    after_items = {it["id"]: it for it in after["items"]}
    assert after_items[candidate["id"]]["status"] == "confirmed"
    assert after_items[candidate["id"]]["run_id"] == run_id
    assert after_items[candidate["id"]]["job_id"] == before_items[candidate["id"]]["job_id"]
    assert after_items[candidate["id"]] == before_items[candidate["id"]]
    # run 仍在
    assert os.path.exists(os.path.join(home, "runs", run_id, "state.yaml"))


def test_generate_all_failed_rejected_no_write(tmp_path, monkeypatch):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    write_snapshot(
        home,
        "2026-10-26",
        {
            "version": 1, "date": "2026-10-26", "sources": [], "sources_failed": ["weibo", "baidu", "toutiao"],
            "count": 0, "items": [], "error": "All sources failed.",
        },
    )
    r = c.post(
        "/api/v1/candidates",
        json={"date": "2026-10-26", "style_revision": "sha256:test"},
        headers=post_headers(c),
    )
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert r.json()["details"]["reason"] == "all_failed"
    # candidates/ 不新增文件（可能目录都不存在）
    cand_dir = os.path.join(home, "candidates")
    assert not os.path.exists(cand_dir) or not os.listdir(cand_dir)


def test_generate_insufficient_items_distinct_reason(tmp_path, monkeypatch):
    """快照正常但条目不足 10 条：reason=insufficient_items，与全源失败区分。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    snap = ok_snapshot()
    snap["items"] = snap["items"][:1]  # 仅 1 条可用
    snap["count"] = 1
    snap["error"] = None
    write_snapshot(home, "2026-10-26", snap)
    r = c.post(
        "/api/v1/candidates",
        json={"date": "2026-10-26", "style_revision": "sha256:test"},
        headers=post_headers(c),
    )
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert r.json()["details"]["reason"] == "insufficient_items"
    # candidates/ 不新增文件
    cand_dir = os.path.join(home, "candidates")
    assert not os.path.exists(cand_dir) or not os.listdir(cand_dir)


def test_candidate_get_by_id(tmp_path, monkeypatch):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    r = c.get(f"/api/v1/candidates/{cid}")
    assert r.status_code == 200
    assert r.json()["candidate_set_id"] == cid
    assert len(r.json()["items"]) == 10
    assert r.json()["snapshot_sha256"].startswith("sha256:")


# ---------- 确认 ----------

def _confirm(c, cid, candidate, key, mode="draft", hash_override=None):
    return c.post(
        f"/api/v1/candidates/{cid}/confirm",
        json={
            "candidate_id": candidate["id"],
            "content_hash": hash_override or candidate["content_hash"],
            "max_images": 4,
            "max_image_cost": 10.0,
            "mode": mode,
            "visual_mode": "full",
        },
        headers=post_headers(c, {"Idempotency-Key": key}),
    )


def test_confirm_creates_run_and_queued_job(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    r = _confirm(c, cid, candidate, "key-1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "run_id" in data and "job_id" in data
    # job 落盘 queued
    job_path = os.path.join(home, "jobs", f"{data['job_id']}.json")
    assert os.path.exists(job_path)
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    assert job["status"] == "queued"
    assert job["run_id"] == data["run_id"]
    # intent 落盘 committed
    intent_path = os.path.join(home, "intents", f"{cid}-{candidate['id']}.json")
    assert os.path.exists(intent_path)
    # 候选转 confirmed，集合关闭
    r2 = c.get(f"/api/v1/candidates/{cid}")
    items = {it["id"]: it for it in r2.json()["items"]}
    assert items[candidate["id"]]["status"] == "confirmed"
    assert r2.json()["set_status"] == "confirmed"
    assert adapter.calls == 1


def test_confirm_idempotent_replay_returns_same_job(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    r1 = _confirm(c, cid, candidate, "same-key")
    r2 = _confirm(c, cid, candidate, "same-key")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert adapter.calls == 1  # 不二次调用上游


def test_confirm_different_key_conflict(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    assert _confirm(c, cid, candidate, "key-a").status_code == 200
    r = _confirm(c, cid, candidate, "key-b")
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"
    assert adapter.calls == 1


def test_confirm_mode_publish_forbidden(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    r = _confirm(c, cid, candidate, "key-pub", mode="publish")
    assert r.status_code == 403
    assert r.json()["code"] == "PUBLISH_DISABLED"
    assert adapter.calls == 0


def test_confirm_hash_changed_rejected(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    r = _confirm(c, cid, candidate, "key-hash", hash_override="sha256:deadbeef")
    assert r.status_code == 409
    assert r.json()["code"] == "CANDIDATE_NOT_ACTIONABLE"
    assert r.json()["details"]["reason"] == "hash_changed"
    assert adapter.calls == 0


def test_confirm_tampered_body_and_saved_hash_rejected(tmp_path, monkeypatch):
    """篡改正文且同步篡改保存哈希 → 确认仍被拒（重算比对，阻断项修复回归）。

    场景：磁盘候选被篡改（正文标题与保存的 content_hash 字段一并改写成一致），
    但确认请求仍携带篡改前调用方持有的原始 content_hash。确认门必须按当前正文
    关键字段重算哈希并与请求值比对，发现不一致即 409 hash_changed。
    """
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]
    original_hash = candidate["content_hash"]

    # 篡改磁盘正文关键字段，并把保存的 content_hash 同步改成篡改后正文的哈希
    from app.domain import scoring

    store = candidates_store.CandidateStore(home)
    data = store.load(cid)
    item = [it for it in data["items"] if it["id"] == candidate["id"]][0]
    item["title"] = "被篡改的标题"
    item["content_hash"] = scoring.content_hash(item)  # 与篡改正文一致
    store._write_set(data)

    # 调用方仍持原始 content_hash，确认门按当前正文重算后必须拒绝
    r = _confirm(c, cid, candidate, "key-tamper", hash_override=original_hash)
    assert r.status_code == 409
    assert r.json()["code"] == "CANDIDATE_NOT_ACTIONABLE"
    assert r.json()["details"]["reason"] == "hash_changed"
    assert adapter.calls == 0


def test_confirm_expired_rejected(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    # 把 expires_at 改到过去
    store = candidates_store.CandidateStore(home)
    data = store.load(cid)
    data["expires_at"] = "2000-01-01T00:00:00+00:00"
    store._write_set(data)

    r = _confirm(c, cid, candidate, "key-exp")
    assert r.status_code == 409
    assert r.json()["code"] == "CANDIDATE_NOT_ACTIONABLE"
    assert r.json()["details"]["reason"] == "expired"
    assert adapter.calls == 0


def test_confirm_requires_idempotency_key(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    r = c.post(
        f"/api/v1/candidates/{cid}/confirm",
        json={"candidate_id": candidate["id"], "content_hash": candidate["content_hash"], "mode": "draft"},
        headers=post_headers(c),
    )
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


# ---------- 撤回 ----------

def test_retract_keeps_run_and_marks_topic_retracted(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    confirmed = _confirm(c, cid, candidate, "key-retract").json()
    run_id = confirmed["run_id"]

    r = c.post(
        f"/api/v1/candidates/{cid}/retract",
        json={"candidate_id": candidate["id"]},
        headers=post_headers(c),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "retracted"

    # 候选转 retracted
    items = {it["id"]: it for it in c.get(f"/api/v1/candidates/{cid}").json()["items"]}
    assert items[candidate["id"]]["status"] == "retracted"

    # run 仍存在且 topic.retracted=true
    import yaml

    with open(os.path.join(home, "runs", run_id, "state.yaml"), encoding="utf-8") as f:
        state = yaml.safe_load(f)
    assert state["topic"]["retracted"] is True

    # job 转 cancelled
    job_id = confirmed["job_id"]
    with open(os.path.join(home, "jobs", f"{job_id}.json"), encoding="utf-8") as f:
        job = json.load(f)
    assert job["status"] == "cancelled"


def test_retract_not_confirmed_rejected(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    r = c.post(
        f"/api/v1/candidates/{cid}/retract",
        json={"candidate_id": candidate["id"]},
        headers=post_headers(c),
    )
    assert r.status_code == 409
    assert r.json()["code"] == "JOB_NOT_RETRACTABLE"


# ---------- 删除保护 ----------

def test_delete_open_unconfirmed_set_allowed(tmp_path, monkeypatch):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]

    r = c.delete(f"/api/v1/candidates/{cid}", headers=post_headers(c))
    assert r.status_code == 200
    assert r.json()["set_status"] == "deleted"
    # 墓碑：磁盘仍保留，状态为 deleted
    store = candidates_store.CandidateStore(home)
    assert store.load(cid)["set_status"] == "deleted"


def test_delete_confirmed_set_rejected(tmp_path, monkeypatch):
    home = str(tmp_path)
    adapter = FakeWewriteAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]
    _confirm(c, cid, candidate, "key-del")

    r = c.delete(f"/api/v1/candidates/{cid}", headers=post_headers(c))
    assert r.status_code == 409
    assert r.json()["code"] == "CANDIDATE_NOT_ACTIONABLE"
    assert r.json()["details"]["reason"] == "set_closed"


# ---------- 路径安全 ----------

def test_candidates_dir_symlink_rejected(tmp_path, monkeypatch):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    generate_set(c, home)
    c.get("/api/v1/runs")  # 钉住状态根身份

    real = os.path.join(home, "candidates")
    os.rename(real, os.path.join(home, "candidates.real"))
    outside = os.path.join(home, "evil-candidates")
    os.makedirs(outside, exist_ok=True)
    os.symlink(outside, real)

    r = c.get("/api/v1/candidates")
    assert r.status_code == 403
    assert r.json()["code"] == "PATH_FORBIDDEN"


def test_candidate_file_symlink_rejected(tmp_path, monkeypatch):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    c.get("/api/v1/runs")

    file_path = os.path.join(home, "candidates", f"{cid}.json")
    outside = os.path.join(home, "evil-leaf.json")
    with open(outside, "w", encoding="utf-8") as f:
        f.write('{"candidate_set_id":"EVIL","items":[{"title":"EVIL-LEAF"}]}')
    os.remove(file_path)
    os.symlink(outside, file_path)

    r = c.get(f"/api/v1/candidates/{cid}")
    assert r.status_code == 403
    assert r.json()["code"] == "PATH_FORBIDDEN"
    assert "EVIL-LEAF" not in r.text


# ---------- 原子写权限 ----------

def test_candidate_file_mode_0600(tmp_path, monkeypatch):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    mode = os.stat(os.path.join(home, "candidates", f"{cid}.json")).st_mode & 0o777
    assert mode == 0o600


# ---------- 跨进程并发确认 ----------

def _worker_confirm(home, cid, candidate_id, content_hash, key, out_q):
    """跨进程确认 worker：各自独立 ConfirmGate + FakeWewriteAdapter。"""
    adapter = FakeWewriteAdapter(home)
    gate = confirm_gate.ConfirmGate(home, adapter=adapter)
    try:
        result = gate.confirm(
            cid,
            {
                "candidate_id": candidate_id,
                "content_hash": content_hash,
                "mode": "draft",
                "visual_mode": "full",
                "max_images": 4,
                "max_image_cost": 10.0,
            },
            key,
        )
        out_q.put(("ok", result))
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", type(exc).__name__)
        out_q.put((code, None))


def test_concurrent_confirm_only_one_succeeds(tmp_path, monkeypatch):
    """跨进程并发确认同一候选：只有一个成功，其余 CONFLICT。"""
    import multiprocessing

    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    body = generate_set(c, home)
    cid = body["candidate_set_id"]
    candidate = body["items"][0]

    ctx = multiprocessing.get_context("spawn")
    out_q = ctx.Queue()
    procs = []
    n = 6
    for i in range(n):
        p = ctx.Process(
            target=_worker_confirm,
            args=(home, cid, candidate["id"], candidate["content_hash"], f"key-{i}", out_q),
        )
        p.start()
        procs.append(p)
    for p in procs:
        p.join(30)

    results = []
    while not out_q.empty():
        results.append(out_q.get())

    oks = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "CONFLICT"]
    assert len(oks) == 1, results
    assert len(conflicts) == n - 1, results

    # 只有一个 job 落盘
    jobs = [f for f in os.listdir(os.path.join(home, "jobs")) if f.endswith(".json")]
    assert len(jobs) == 1
