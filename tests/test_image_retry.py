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


# ---------- JEE-1961 返修：密钥保密 / 状态保持 / 全路径终态 / 模型审计 ----------

def _full_record(home, job_id, run_id) -> str:
    """整份任务记录 + state.yaml 的 JSON 序列化，用于密钥零命中断言。"""
    import json as _json

    store = jobs_store.JobStore(home)
    job = store.load(job_id)
    state_path = os.path.join(home, "runs", run_id, "state.yaml")
    with open(state_path, encoding="utf-8") as f:
        state = yaml.safe_load(f)
    return _json.dumps({"job": job, "state": state}, ensure_ascii=False)


def test_rework_exception_path_redacts_key_in_full_record(tmp_path):
    """返修 1：后端抛异常路径，合成密钥在整份任务记录里零命中。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png", "error": "old"},
    ])
    jobs_store.JobStore(home).retry_images("j-retry00001", [0])

    image = CountingImage(raise_exc=RuntimeError("401 Incorrect API key provided: sk-synthetic123456789"))
    result = Worker(home, image=image).run_once()
    assert result["status"] == "failed"
    assert result["images"][0]["status"] == "failed"
    assert "sk-synthetic123456789" not in _full_record(home, "j-retry00001", run_id)


def test_rework_failed_result_path_redacts_key_in_full_record(tmp_path):
    """返修 1：失败结果返回路径，合成密钥在整份任务记录里零命中。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png", "error": "old"},
    ])
    jobs_store.JobStore(home).retry_images("j-retry00001", [0])

    image = CountingImage([
        {"output": f"runs/{run_id}/images/0.png", "ok": False,
         "error": "Incorrect API key provided: sk-synthetic123456789"}
    ])
    result = Worker(home, image=image).run_once()
    assert result["status"] == "failed"
    assert result["images"][0]["status"] == "failed"
    assert "sk-synthetic123456789" not in _full_record(home, "j-retry00001", run_id)


def test_rework_preserves_publish_state_and_cover(tmp_path):
    """返修 2：生成期间改 permissions/media_id/cover，执行后三者保持，不写 cover=None。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png"},
    ])
    state = base_state(run_id)
    state["permissions"]["publish"] = True
    state["images"] = {"cover": {"output": "cover.png"}, "figures": []}
    write_state(home, run_id, state)

    class StateChangingImage:
        def generate_batch(self, items, **kwargs):
            changed = dict(state)
            changed["permissions"] = {"publish": False}
            changed["media_id"] = "synthetic-new-media-id"
            write_state(home, run_id, changed)
            return [{"ok": False, "error": "some provider error"}]

    jobs_store.JobStore(home).retry_images("j-retry00001", [0])
    Worker(home, image=StateChangingImage()).run_once()

    with open(os.path.join(home, "runs", run_id, "state.yaml"), encoding="utf-8") as f:
        final = yaml.safe_load(f)
    assert final["permissions"]["publish"] is False
    assert final.get("media_id") == "synthetic-new-media-id"
    assert final["images"].get("cover") is not None
    assert final["images"]["cover"] == {"output": "cover.png"}


def test_rework_budget_overflow_converges_pending(tmp_path):
    """返修 3：预算超限路径，backend 调用 0 次，pending 图收敛为 failed，不残留。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png"},
    ])
    state = base_state(run_id, max_cost=0)
    write_state(home, run_id, state)
    # 预估成本 > 0 的 max_cost=0 必然超预算
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("image:\n  estimated_cost_per_image: 1\n")

    store = jobs_store.JobStore(home)
    store.retry_images("j-retry00001", [0])
    image = CountingImage([{"output": f"runs/{run_id}/images/0.png", "ok": True}])
    result = Worker(home, image=image).run_once()
    assert len(image.calls) == 0
    assert result["status"] == "failed"
    assert result["images"][0]["status"] == "failed"
    assert result["images"][0]["error_code"] == "IMAGE_QUOTA_EXCEEDED"


def test_rework_exception_path_error_code_nonempty_and_publish_kept(tmp_path):
    """返修 4：异常路径 error_code 非空 + 全文无密钥 + 发布态不变（交叉约束一次判定）。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png"},
    ])
    state = base_state(run_id)
    state["permissions"]["publish"] = True
    state["images"] = {"cover": {"output": "cover.png"}, "figures": []}
    write_state(home, run_id, state)

    jobs_store.JobStore(home).retry_images("j-retry00001", [0])
    image = CountingImage(raise_exc=RuntimeError("401 api_key=sk-synthetic123456789 invalid"))
    result = Worker(home, image=image).run_once()
    assert result["images"][0]["error_code"] == "IMAGE_AUTH_FAILED"
    assert "sk-synthetic123456789" not in _full_record(home, "j-retry00001", run_id)
    with open(os.path.join(home, "runs", run_id, "state.yaml"), encoding="utf-8") as f:
        final = yaml.safe_load(f)
    assert final["permissions"]["publish"] is True
    assert final["images"].get("cover") is not None


def test_rework_audit_uses_executed_config(tmp_path, monkeypatch):
    """返修 5：排队后改 image.provider/model，审计字段等于实际调用配置。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png"},
    ])
    # 先写 config（provider A），排队
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("image:\n  provider: provider-a\n  model: model-a\n")
    store = jobs_store.JobStore(home)
    store.retry_images("j-retry00001", [0])

    # 排队后改 config（provider B）
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("image:\n  provider: provider-b\n  model: model-b\n")

    image = CountingImage([{"output": f"runs/{run_id}/images/0.png", "ok": True}])
    result = Worker(home, image=image).run_once()
    img0 = result["images"][0]
    # 审计字段 = 实际调用配置（provider-b），而不是排队时的 provider-a
    assert img0["executed_provider"] == "provider-b"
    assert img0["executed_model"] == "model-b"
    # 排队时的请求记录仍保留（provider-a）
    assert img0["retry_history"][0]["provider"] == "provider-a"


# ---------- 第三轮返修：脱敏兜底 / 只同步选中索引 / 配置与审计一致 ----------

def test_sanitize_error_review_forms():
    """返修 1：验收用具体形态，五条全部零命中。"""
    secret = "abcdefghijk123456"
    forms = [
        f"401 api_key: {secret}",
        f"api_key:  {secret}",  # 两个空格
        f"Incorrect API key provided: {secret}",
        f"token {secret}",
        f'{{"api_key":"{secret}"}}',
    ]
    for text in forms:
        out = _sanitize_error(text)
        assert secret not in out, f"泄漏：{text!r} -> {out!r}"
        assert "[REDACTED]" in out


def test_rework_sanitize_full_record_no_key(tmp_path):
    """返修 1：三个持久化出口（job.last_error / job.images[].error / state.yaml）零命中。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png", "error": "old"},
    ])
    jobs_store.JobStore(home).retry_images("j-retry00001", [0])
    secret = "abcdefghijk123456"
    # 异常抛出路径
    image = CountingImage(raise_exc=RuntimeError(f"401 api_key: {secret}"))
    Worker(home, image=image).run_once()
    rec = _full_record(home, "j-retry00001", run_id)
    assert secret not in rec
    # 失败结果返回路径（重新排队再跑）
    store = jobs_store.JobStore(home)
    job = store.load("j-retry00001")
    job["status"] = "failed"; job["retry_mode"] = None
    job["images"][0]["status"] = "failed"
    store.write(job)
    store.retry_images("j-retry00001", [0])
    image2 = CountingImage([{"output": f"runs/{run_id}/images/0.png", "ok": False,
                             "error": f"Incorrect API key provided: {secret}"}])
    Worker(home, image=image2).run_once()
    rec2 = _full_record(home, "j-retry00001", run_id)
    assert secret not in rec2


def test_rework_sync_only_selected_indices(tmp_path):
    """返修 2：只重试图 0，生成期间图 1 被改成 succeeded 且换 output，执行后图 1 保持故障桩值。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png", "error": "e0"},
        {"index": 1, "status": "failed", "output": f"runs/{run_id}/images/1.png", "error": "e1"},
        {"index": 2, "status": "succeeded", "output": f"runs/{run_id}/images/2.png", "error": None},
    ])
    # 先写 state：图 1 原始为 failed，图 2 succeeded
    state = base_state(run_id, max_images=3)
    state["images"] = {"cover": None, "figures": [
        {"index": 0, "status": "failed", "output": "images/0.png", "error": "e0"},
        {"index": 1, "status": "failed", "output": "images/1.png", "error": "e1"},
        {"index": 2, "status": "succeeded", "output": "images/2.png", "error": None},
    ]}
    write_state(home, run_id, state)

    class StateChangingImage:
        def generate_batch(self, items, **kwargs):
            # 生成期间故障桩把未选中的图 1 改成 succeeded 并换 output
            changed = dict(state)
            changed["images"] = {"cover": None, "figures": [
                {"index": 0, "status": "failed", "output": "images/0.png", "error": "e0"},
                {"index": 1, "status": "succeeded", "output": "images/new-1.png", "error": None},
                {"index": 2, "status": "succeeded", "output": "images/2.png", "error": None},
            ]}
            write_state(home, run_id, changed)
            return [{"ok": True, "output": f"runs/{run_id}/images/0.png"}]

    store = jobs_store.JobStore(home)
    store.retry_images("j-retry00001", [0])
    Worker(home, image=StateChangingImage()).run_once()

    with open(os.path.join(home, "runs", run_id, "state.yaml"), encoding="utf-8") as f:
        final = yaml.safe_load(f)
    figs = {f["index"]: f for f in final["images"]["figures"]}
    # 图 0 被本次重试更新为 succeeded
    assert figs[0]["status"] == "succeeded"
    # 图 1 保持故障桩写入的值（succeeded + new-1.png），不被旧 job 快照覆盖
    assert figs[1]["status"] == "succeeded"
    assert figs[1]["output"] == "images/new-1.png"


def test_rework_config_used_for_call_and_audit(tmp_path, monkeypatch):
    """返修 3：同一份生效配置用于实际调用与审计，执行历史记录每次实际 provider/model。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_failed_images_job(home, run_id, images=[
        {"index": 0, "status": "failed", "output": f"runs/{run_id}/images/0.png"},
    ])
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("image:\n  provider: file-provider\n  model: file-model\n")
    monkeypatch.setenv("WEWRITE_IMAGE_PROVIDER", "env-provider")
    monkeypatch.setenv("WEWRITE_IMAGE_MODEL", "env-model")

    class RecordingImage:
        def __init__(self):
            self.calls = []
        def generate_batch(self, items, config=None, max_images=None, max_cost=None):
            self.calls.append(config)
            return [{"ok": True, "output": f"runs/{run_id}/images/0.png"}]

    store = jobs_store.JobStore(home)
    store.retry_images("j-retry00001", [0])
    image = RecordingImage()
    result = Worker(home, image=image).run_once()
    # 后端收到的 config.image 与审计字段完全一致（env 优先）
    received = image.calls[0]["image"]
    img0 = result["images"][0]
    assert received["provider"] == "env-provider"
    assert received["model"] == "env-model"
    assert img0["executed_provider"] == "env-provider"
    assert img0["executed_model"] == "env-model"
    # 执行历史能看出每次实际用的是哪个
    assert img0["execution_history"][-1]["provider"] == "env-provider"
    assert img0["execution_history"][-1]["model"] == "env-model"
