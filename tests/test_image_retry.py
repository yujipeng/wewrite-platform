"""JEE-1961 图片重试链路测试：重试真正触发生成、只重跑 pending、不再卡排队、
改模型可再重试、运行中 409、错误可归类且不泄漏 api_key。

fake image backend 全部注入，不打真实 provider。
"""

from __future__ import annotations

import os

import yaml

from app.storage import jobs as jobs_store
from app.worker.executor import Worker, classify_image_error, _sanitize_error


def write_state(home, run_id, state: dict):
    d = os.path.join(home, "runs", run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "state.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)


def base_state(run_id, max_images=2, max_cost=None):
    return {
        "version": 4,
        "run_id": run_id,
        "status": "active",
        "mode": "draft",
        "permissions": {"publish": False},
        "visual": {"mode": "full", "max_images": max_images, "max_cost": max_cost},
        "topic": {"title": "测试选题", "keywords": ["k"], "source": "微博", "framework_hint": "问题-证据-行动"},
        "artifacts": {},
        "steps": {},
        "last_error": None,
    }


class FakeWriter:
    def run(self, brief_path, output_path, env=None):
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("这是生成的正文。" * 40)
        return 0


class FakeReviewer:
    def review(self, run_id, article_text):
        return {"decision": "pass", "publishable": True, "note": "规则审稿桩"}


class CountingImage:
    """记录每次 generate_batch 的 items 并返回预置结果（逐项对齐）。"""

    def __init__(self, results=None, raise_exc=None):
        self.results = results or []
        self.raise_exc = raise_exc
        self.calls = []

    def generate_batch(self, items, config=None, max_images=None, max_cost=None):
        self.calls.append({"items": items, "max_images": max_images, "max_cost": max_cost})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.results


def make_failed_images_job(home, run_id, job_id="j-retry00001", images=None):
    write_state(home, run_id, base_state(run_id, max_images=2))
    store = jobs_store.JobStore(home)
    job = jobs_store.build_job(job_id, run_id)
    job["status"] = "failed"
    job["step"] = "visual"
    job["images"] = images or []
    store.write(job)
    return job


def test_retry_requeues_and_worker_regenerates(tmp_path):
    """验收 1：重试真正执行 —— job 变 queued，worker 跑后图 0 有真实结果（fake 被调 1 次）。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png", "error": "provider failed"},
    ])
    store = jobs_store.JobStore(home)
    retried = store.retry_images("j-retry00001", [0])
    assert retried["status"] == "queued"
    assert retried["images"][0]["status"] == "pending"

    image = CountingImage([{"output": f"runs/{run_id}/images/0.png", "ok": True}])
    worker = Worker(home, writer=FakeWriter(), reviewer=FakeReviewer(), image=image)
    result = worker.run_once()
    assert result is not None
    assert result["status"] == "succeeded"
    assert result["images"][0]["status"] == "succeeded"
    assert len(image.calls) == 1


def test_retry_only_reruns_pending(tmp_path):
    """验收 2：只重跑 pending —— 图 0 失败、图 1 成功，重试 [0]，fake 只被调 1 次（对应图 0）。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png", "error": "e"},
        {"index": 1, "status": "succeeded", "output": f"runs/{run_id}/images/1.png", "error": None},
    ])
    store = jobs_store.JobStore(home)
    store.retry_images("j-retry00001", [0])

    image = CountingImage([{"output": f"runs/{run_id}/images/0.png", "ok": True}])
    worker = Worker(home, writer=FakeWriter(), reviewer=FakeReviewer(), image=image)
    result = worker.run_once()
    assert len(image.calls) == 1
    # 只传了图 0 的 item
    assert [i["output"] for i in image.calls[0]["items"]] == [f"runs/{run_id}/images/0.png"]
    assert result["images"][1]["status"] == "succeeded"
    assert result["images"][1]["error"] is None


def test_retry_never_stuck_pending_on_failure(tmp_path):
    """验收 3：不再卡排队 —— 重试后 worker 跑完，图 0 最终 succeeded 或 failed，不停在 pending。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png", "error": "e"},
    ])
    store = jobs_store.JobStore(home)
    store.retry_images("j-retry00001", [0])

    image = CountingImage([{"output": f"runs/{run_id}/images/0.png", "ok": False, "error": "still broken"}])
    worker = Worker(home, writer=FakeWriter(), reviewer=FakeReviewer(), image=image)
    result = worker.run_once()
    assert result["status"] == "failed"
    assert result["images"][0]["status"] == "failed"
    assert result["images"][0]["status"] != "pending"
    assert result["last_error"]["code"] == "IMAGE_RETRY_FAILED"


def test_retry_running_job_conflict(tmp_path):
    """验收 5：job 为 running 时重试 → 409 CONFLICT。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png", "error": "e"},
    ])
    store = jobs_store.JobStore(home)
    job = store.load("j-retry00001")
    job["status"] = "running"
    store.write(job)
    try:
        store.retry_images("j-retry00001", [0])
        assert False, "运行中应拒绝重试"
    except Exception as exc:
        assert exc.code == "CONFLICT"
        assert exc.details["reason"] == "job_running"


def test_retry_classifies_auth_error_and_redacts_key(tmp_path):
    """验收 6：fake backend 抛 401 → error_code == IMAGE_AUTH_FAILED，error 不含 api_key。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png", "error": "e"},
    ])
    store = jobs_store.JobStore(home)
    store.retry_images("j-retry00001", [0])

    image = CountingImage([
        {"output": f"runs/{run_id}/images/0.png", "ok": False,
         "error": "401 Unauthorized: api_key=sk-secret-abcdef1234567890 invalid"}
    ])
    worker = Worker(home, writer=FakeWriter(), reviewer=FakeReviewer(), image=image)
    result = worker.run_once()
    img0 = result["images"][0]
    assert img0["error_code"] == "IMAGE_AUTH_FAILED"
    assert "sk-secret-abcdef1234567890" not in img0["error"]
    assert "[REDACTED]" in img0["error"]


def test_classify_image_error_codes():
    assert classify_image_error("401 unauthorized") == "IMAGE_AUTH_FAILED"
    assert classify_image_error("429 rate limit exceeded") == "IMAGE_QUOTA_EXCEEDED"
    assert classify_image_error("model 'foo' not found") == "IMAGE_MODEL_INVALID"
    assert classify_image_error("request timed out") == "IMAGE_TIMEOUT"
    assert classify_image_error("something else broke") == "IMAGE_PROVIDER_ERROR"


def test_sanitize_error_redacts_bearer_and_key():
    out = _sanitize_error("Bearer abcdef1234567890 failed, api_key: sk-secret-abcdef1234567890")
    assert "abcdef1234567890" not in out
    assert "sk-secret-abcdef1234567890" not in out
