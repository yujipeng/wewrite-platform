"""M4 P6 测试：worker 消费 queued job、写作退出码、取消语义、心跳超时恢复、
resume/accept-existing/retry、批处理图片回填。"""

from __future__ import annotations

import json
import os

import yaml

from app.storage import jobs as jobs_store
from app.worker.executor import Worker, WriterBackend, _writer_env


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
        self.last_env = None

    def run(self, brief_path, output_path, env=None):
        self.last_env = env
        if self.exit_code == 0:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(self.content)
        return self.exit_code


class SequenceWriter:
    """按调用顺序返回不同结果，用于验证「自动续写一次」确实触发第二次调用。"""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0

    def run(self, brief_path, output_path, env=None):
        idx = self.calls
        self.calls += 1
        if idx >= len(self.contents):
            return 4  # 超出预期次数按失败处理
        item = self.contents[idx]
        # item 为字符串 → 写文件返回 0；为 int → 按退出码返回
        if isinstance(item, str):
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(item)
            return 0
        return item


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


def write_config(home, writer: dict):
    """写 config.yaml 的 writer 段（模拟网页保存后的落盘结果）。"""
    from app.storage.config_store import ConfigStore

    store = ConfigStore(home)
    cfg = store.load_config()
    cfg["writer"].update(writer)
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def write_style(home, word_count):
    """写 style.yaml 只含 word_count（模拟网页保存风格配置后的落盘结果）。"""
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "style.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({"version": 1, "word_count": word_count}, f, allow_unicode=True, sort_keys=False)


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


def test_worker_writer_env_injected_from_config(tmp_path):
    """第 7 条核心：网页改 writer 配置后，子进程调 llm-write 时必须注入生效值。

    不是只测「配置存下来了」，而是证明 worker 把 config.yaml 里的 model 等
    以环境变量传给 writer 后端（默认 WriterBackend 再传给 llm-write 子进程）。
    """
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    write_config(home, {
        "provider": "openai",
        "model": "my-custom-model",
        "base_url": "https://gw.example.com/v1",
        "temperature": 0.7,
        "max_tokens": 2048,
        "api_key": "sk-test",
    })
    writer = FakeWriter(0)
    worker = Worker(home, writer=writer, reviewer=FakeReviewer(), image=FakeImage([]))
    result = worker.run_once()
    assert result["status"] == "succeeded"
    env = writer.last_env
    assert env is not None
    assert env["WEWRITE_WRITER_MODEL"] == "my-custom-model"
    assert env["WEWRITE_WRITER_PROVIDER"] == "openai"
    assert env["WEWRITE_WRITER_BASE_URL"] == "https://gw.example.com/v1"
    assert env["WEWRITE_WRITER_TEMPERATURE"] == "0.7"
    assert env["WEWRITE_WRITER_MAX_TOKENS"] == "2048"
    assert env["WEWRITE_WRITER_API_KEY"] == "sk-test"


def test_worker_writer_env_prefers_environment_variables(tmp_path, monkeypatch):
    """env 优先：环境变量已接管的字段，config.yaml 不得覆盖。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    write_config(home, {"model": "config-model", "api_key": "sk-config"})
    monkeypatch.setenv("WEWRITE_WRITER_MODEL", "env-model")
    monkeypatch.setenv("WEWRITE_WRITER_API_KEY", "sk-env")
    writer = FakeWriter(0)
    worker = Worker(home, writer=writer, reviewer=FakeReviewer(), image=FakeImage([]))
    result = worker.run_once()
    assert result["status"] == "succeeded"
    env = writer.last_env
    # env 已接管的字段不注入（让 llm-write 自己读到环境变量的值）
    assert "WEWRITE_WRITER_MODEL" not in env
    assert "WEWRITE_WRITER_API_KEY" not in env
    # 未被 env 接管的字段仍来自 config.yaml
    assert env.get("WEWRITE_WRITER_PROVIDER") == "deepseek"


def test_writer_env_maps_fields_without_overriding_environment(tmp_path, monkeypatch):
    """_writer_env 单元：只映射已配置字段，且不覆盖 env 已接管的字段。"""
    monkeypatch.setenv("WEWRITE_WRITER_MODEL", "env-model")
    config = {
        "writer": {
            "provider": "openai",
            "model": "config-model",
            "base_url": "https://gw.example.com/v1",
            "temperature": 0.5,
            "max_tokens": 1000,
            "api_key": "sk-config",
        }
    }
    env = _writer_env(config)
    assert env["WEWRITE_WRITER_PROVIDER"] == "openai"
    assert env["WEWRITE_WRITER_BASE_URL"] == "https://gw.example.com/v1"
    assert env["WEWRITE_WRITER_TEMPERATURE"] == "0.5"
    assert env["WEWRITE_WRITER_MAX_TOKENS"] == "1000"
    assert env["WEWRITE_WRITER_API_KEY"] == "sk-config"
    # env 已接管的 model 不覆盖
    assert "WEWRITE_WRITER_MODEL" not in env
    # 空值字段不注入
    env2 = _writer_env({"writer": {"model": "", "api_key": None, "base_url": "https://x"}})
    assert "WEWRITE_WRITER_MODEL" not in env2
    assert "WEWRITE_WRITER_API_KEY" not in env2
    assert env2["WEWRITE_WRITER_BASE_URL"] == "https://x"


def test_writer_backend_passes_env_to_subprocess(tmp_path, monkeypatch):
    """WriterBackend.run 把注入的 env 传给子进程（不因空 env 丢失父进程环境）。"""
    backend = WriterBackend()
    brief = tmp_path / "brief.md"
    brief.write_text("# 选题", encoding="utf-8")
    out = tmp_path / "out.md"
    monkeypatch.setattr("app.worker.executor.subprocess.run", _FakeRun())
    backend.run(str(brief), str(out), env={"WEWRITE_WRITER_MODEL": "my-model"})
    call = _FakeRun.last_call
    assert call is not None
    assert call["env"]["WEWRITE_WRITER_MODEL"] == "my-model"
    # 继承父进程环境的必要变量
    assert "PATH" in call["env"]


class _FakeRun:
    """记录 subprocess.run 调用，返回退出码 0。"""

    last_call = None

    def __call__(self, cmd, capture_output=True, text=True, timeout=None, env=None):
        _FakeRun.last_call = {"cmd": cmd, "env": env}
        import subprocess as sp

        return sp.CompletedProcess(cmd, 0, "", "")


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


# ---------- JEE-1963：目标字数校验 + 自动续写一次 ----------

def test_word_count_auto_extend_twice(tmp_path):
    """验收第 4 条：首次 300 字、二次 2000 字 → 写作后端调用 2 次，最终正文 >=1600。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    write_style(home, 2000)
    short = "短" * 300
    long = "长" * 2000
    writer = SequenceWriter([short, long])
    worker = Worker(home, writer=writer, reviewer=FakeReviewer(), image=FakeImage([]))
    result = worker.run_once()
    assert result["status"] == "succeeded"
    assert writer.calls == 2
    with open(os.path.join(home, "runs", run_id, "article.md"), encoding="utf-8") as f:
        final = f.read()
    assert len(final.strip()) >= 1600


def test_word_count_short_no_extra_call_without_style(tmp_path):
    """验收第 7 条：style 无 word_count → 只校验 >=200，无额外调用。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    writer = SequenceWriter(["这是生成的正文。" * 40])
    worker = Worker(home, writer=writer, reviewer=FakeReviewer(), image=FakeImage([]))
    result = worker.run_once()
    assert result["status"] == "succeeded"
    assert writer.calls == 1


def test_word_count_still_short_after_extend_warns(tmp_path):
    """验收第 5 条：两次都返回 300 字 → job 仍成功，length_warning 含实际/目标。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    write_style(home, 2000)
    short = "短" * 300
    writer = SequenceWriter([short, short])
    worker = Worker(home, writer=writer, reviewer=FakeReviewer(), image=FakeImage([]))
    result = worker.run_once()
    assert result["status"] == "succeeded"
    assert writer.calls == 2
    with open(os.path.join(home, "runs", run_id, "state.yaml"), encoding="utf-8") as f:
        st = yaml.safe_load(f)
    warn = st["length_warning"]
    assert warn["actual"] == 300
    assert warn["target_min"] == 1600
    assert warn["target_max"] == 2400
    # 原稿保留（300 字正文仍落盘）
    with open(os.path.join(home, "runs", run_id, "article.md"), encoding="utf-8") as f:
        final = f.read()
    assert len(final.strip()) == 300


def test_word_count_extend_failure_keeps_original(tmp_path):
    """验收第 6 条：首次 300 字、二次退出码 4 → 保留首次正文，job 成功，length_warning 存在。"""
    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    make_queued_job(home, run_id)
    write_style(home, 2000)
    short = "短" * 300
    writer = SequenceWriter([short, 4])
    worker = Worker(home, writer=writer, reviewer=FakeReviewer(), image=FakeImage([]))
    result = worker.run_once()
    assert result["status"] == "succeeded"
    assert writer.calls == 2
    with open(os.path.join(home, "runs", run_id, "state.yaml"), encoding="utf-8") as f:
        st = yaml.safe_load(f)
    assert st["length_warning"]["actual"] == 300
    with open(os.path.join(home, "runs", run_id, "article.md"), encoding="utf-8") as f:
        final = f.read()
    assert "短" in final
    assert len(final.strip()) == 300
