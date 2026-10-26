"""M4 P6 测试：worker 消费 queued job、写作退出码、取消语义、心跳超时恢复、
resume/accept-existing/retry、批处理图片回填。"""

from __future__ import annotations

import json
import os

import yaml

from app.storage import jobs as jobs_store
from app.worker.executor import Worker


def write_state(home, run_id, state: dict):
    d = os.path.join(home, "runs", run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "state.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)


def base_state(run_id, max_images=0):
    return {
        "version": 4,
        "run_id": run_id,
        "status": "active",
        "mode": "draft",
        "permissions": {"publish": False},
        "visual": {"mode": "full" if max_images else "none", "max_images": max_images, "max_cost": None},
        "topic": {"title": "测试选题", "keywords": ["k"], "source": "微博", "framework_hint": "问题-证据-行动"},
        "artifacts": {},
        "steps": {},
        "last_error": None,
    }


class FakeWriter:
    def __init__(self, exit_code=0, content=None):
        self.exit_code = exit_code
        self.content = content or ("这是生成的正文。" * 40)  # 320 字符

    def run(self, brief_path, output_path):
        if self.exit_code == 0:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(self.content)
        return self.exit_code


class FakeReviewer:
    def review(self, run_id, article_text):
        return {"decision": "pass", "publishable": True, "note": "规则审稿桩"}


class FakeImage:
    def __init__(self, results):
        self.results = results

    def generate_batch(self, items, config=None, max_images=None, max_cost=None):
        return self.results


def make_queued_job(home, run_id, job_id="j-test000001"):
    write_state(home, run_id, base_state(run_id))
    store = jobs_store.JobStore(home)
    job = jobs_store.build_job(job_id, run_id)
    store.write(job)
    return job


def test_worker_consumes_queued_job_success(tmp_path):
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    worker = Worker(home, writer=FakeWriter(0), reviewer=FakeReviewer(), image=FakeImage([]))
    result = worker.run_once()
    assert result is not None
    assert result["status"] == "succeeded"
    # 产物落盘
    assert os.path.exists(os.path.join(home, "runs", run_id, "article.md"))
    assert os.path.exists(os.path.join(home, "runs", run_id, "review-report.json"))
    with open(os.path.join(home, "runs", run_id, "review-report.json"), encoding="utf-8") as f:
        report = json.load(f)
    assert report["decision"] == "pass"


def test_worker_writer_exit_3_not_configured(tmp_path):
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    worker = Worker(home, writer=FakeWriter(3), reviewer=FakeReviewer(), image=FakeImage([]))
    result = worker.run_once()
    assert result["status"] == "failed"
    assert result["last_error"]["code"] == "WRITER_NOT_CONFIGURED"


def test_worker_writer_exit_4_not_overwrite_existing(tmp_path):
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    # 已有正文
    article_path = os.path.join(home, "runs", run_id, "article.md")
    os.makedirs(os.path.dirname(article_path), exist_ok=True)
    with open(article_path, "w", encoding="utf-8") as f:
        f.write("既有正文不应被覆盖。" * 20)
    worker = Worker(home, writer=FakeWriter(4), reviewer=FakeReviewer(), image=FakeImage([]))
    result = worker.run_once()
    assert result["status"] == "failed"
    assert result["last_error"]["code"] == "WRITER_FAILED"
    with open(article_path, encoding="utf-8") as f:
        assert "既有正文不应被覆盖" in f.read()


def test_worker_batch_images_backfilled(tmp_path):
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    state = base_state(run_id, max_images=2)
    write_state(home, run_id, state)
    store = jobs_store.JobStore(home)
    store.write(jobs_store.build_job("j-img0000001", run_id))

    image = FakeImage([
        {"output": f"runs/{run_id}/images/0.png", "ok": True},
        {"output": f"runs/{run_id}/images/1.png", "ok": False, "error": "provider failed"},
    ])
    worker = Worker(home, writer=FakeWriter(0), reviewer=FakeReviewer(), image=image)
    result = worker.run_once()
    assert result["status"] == "succeeded"
    imgs = {i["index"]: i for i in result["images"]}
    assert imgs[0]["status"] == "succeeded"
    assert imgs[1]["status"] == "failed"
    # state.images 一次性回填
    with open(os.path.join(home, "runs", run_id, "state.yaml"), encoding="utf-8") as f:
        st = yaml.safe_load(f)
    figures = st["images"]["figures"]
    assert [f["status"] for f in figures] == ["succeeded", "failed"]
    # 配图产物写 article-illustrated.md，不覆盖 article.md
    assert os.path.exists(os.path.join(home, "runs", run_id, "article-illustrated.md"))


def test_worker_cancel_cooperative(tmp_path):
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    store = jobs_store.JobStore(home)
    # queued 可撤回
    job = store.cas_queued_to_running("j-test000001")
    assert job is not None
    store.cancel("j-test000001")
    # 心跳超时恢复
    changed = Worker(home).recover_stale(0)
    assert "j-test000001" in changed
    reloaded = store.load("j-test000001")
    assert reloaded["status"] == "unknown"


def test_job_resume_failed_to_queued(tmp_path, monkeypatch):
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    store = jobs_store.JobStore(home)
    job = store.load("j-test000001")
    job["status"] = "failed"
    store.write(job)
    # resume 走上游 resume_run；用 monkeypatch 拦 adapter
    import app.adapters.wewrite as wa

    monkeypatch.setattr(wa.WeWriteAdapter, "resume_run", lambda self, run_id: {"run_id": run_id})
    resumed = store.resume("j-test000001")
    assert resumed["status"] == "queued"
    assert resumed["attempt"] == 2


def test_job_accept_existing_does_not_change_status(tmp_path):
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    store = jobs_store.JobStore(home)
    job = store.load("j-test000001")
    job["status"] = "succeeded"
    store.write(job)
    accepted = store.accept_existing("j-test000001")
    assert accepted["status"] == "succeeded"
    assert accepted["accepted_existing"] is True


def test_job_retry_images_once_only(tmp_path):
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    store = jobs_store.JobStore(home)
    job = store.load("j-test000001")
    job["images"] = [
        {"index": 0, "status": "failed", "output": "x", "error": "e"},
        {"index": 1, "status": "succeeded", "output": "y", "error": None},
    ]
    store.write(job)
    # 成功图不重试
    try:
        store.retry_images("j-test000001", [1])
        assert False, "应拒绝重试成功图"
    except Exception as exc:
        assert exc.code == "CONFLICT"
    # 失败图重试一次
    store.retry_images("j-test000001", [0])
    # 二次重试拒绝
    try:
        store.retry_images("j-test000001", [0])
        assert False, "应拒绝二次重试"
    except Exception as exc:
        assert exc.code == "CONFLICT"


# ---------- GET /api/v1/jobs?run_id= ----------

def test_list_jobs_by_run_id(monkeypatch, tmp_path):
    """P2 重试/P6 进度入口依赖：按 run_id 过滤 job 列表。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)

    from app.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WEWRITE_HOME", home)
    monkeypatch.setenv("WEWRITE_PLATFORM_NO_WORKER", "1")
    app = create_app()
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update({"host": "127.0.0.1:8765"})

    r = c.get("/api/v1/jobs?run_id=" + run_id)
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert [j["job_id"] for j in jobs] == ["j-test000001"]

    # 无关 run_id → 空
    r = c.get("/api/v1/jobs?run_id=20261026-010101-000000")
    assert r.status_code == 200
    assert r.json()["jobs"] == []

    # 非法 run_id → 400
    r = c.get("/api/v1/jobs?run_id=not-a-valid-id")
    assert r.status_code == 400
    assert r.json()["code"] == "RUN_ID_INVALID"
