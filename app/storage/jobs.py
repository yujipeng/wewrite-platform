"""Job 存储：``jobs/<job_id>.json`` 的落盘与状态机。

按 docs/s2-technical-design.md 第 2.3 / 3 节。M3 只实现首个 queued job 的持久化
与 queued→cancelled（撤回）转移，不启动 worker 消费（P6 在 M4 启用）。

状态机 ``queued|running|succeeded|failed|cancelled|unknown``；
``queued`` 可撤回，``queued→running`` 是 worker 的 CAS 点（M4），一旦 running
即不可撤回（``JOB_NOT_RETRACTABLE``）。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

import yaml

from app.domain import errors
from app.storage import paths

VALID_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled", "unknown"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_job_id() -> str:
    return "j-" + secrets.token_hex(6)


def build_job(job_id: str, run_id: str, attempt: int = 1) -> dict:
    """构造首个 queued job 文件内容（设计 2.3）。"""
    now = _now()
    return {
        "version": 1,
        "job_id": job_id,
        "run_id": run_id,
        "status": "queued",
        "attempt": attempt,
        "cancel_requested": False,
        "step": "topic",
        "progress": {"kind": "step", "completed": 0, "total": 4},
        "images": [],
        "created_at": now,
        "updated_at": now,
        "heartbeat_at": None,
        "last_error": None,
    }


class JobStore:
    """jobs 目录的读写，全部走 paths 安全层（openat + O_NOFOLLOW）。"""

    def __init__(self, home: str):
        self.home = home

    def load(self, job_id: str) -> dict | None:
        text = paths.safe_read_subdir_text(self.home, "jobs", f"{job_id}.json")
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def write(self, job: dict) -> None:
        paths.safe_write_subdir_file(
            self.home, "jobs", f"{job['job_id']}.json", json.dumps(job, ensure_ascii=False, indent=2)
        )

    def get_or_404(self, job_id: str) -> dict:
        job = self.load(job_id)
        if job is None:
            raise errors.not_found({"reason": "job_not_found", "job_id": job_id})
        return job

    def transition_to_cancelled(self, job: dict) -> dict:
        """queued → cancelled（撤回）。非 queued 抛 JOB_NOT_RETRACTABLE。"""
        if job.get("status") != "queued":
            raise errors.job_not_retractable(
                {"reason": "job_not_queued", "status": job.get("status")}
            )
        job["status"] = "cancelled"
        job["cancel_requested"] = True
        job["updated_at"] = _now()
        self.write(job)
        return job

    # ---------- M4：worker 消费 / 状态机扩展 ----------

    def list_jobs(self) -> list[dict]:
        """枚举全部 job（按文件名排序）。"""
        result = []
        for fname in paths.safe_list_subdir_filenames(self.home, "jobs"):
            if not fname.endswith(".json"):
                continue
            job = self.load(fname[:-5])
            if job is not None:
                result.append(job)
        result.sort(key=lambda j: j.get("job_id") or "")
        return result

    def cas_queued_to_running(self, job_id: str) -> dict | None:
        """原子 CAS：queued → running，返回转换后的 job；非 queued 返回 None。"""
        from app.storage.atomic import PlatformLock

        with PlatformLock(self.home):
            job = self.load(job_id)
            if job is None or job.get("status") != "queued":
                return None
            if job.get("cancel_requested"):
                return None
            job["status"] = "running"
            job["step"] = "topic"
            job["heartbeat_at"] = _now()
            job["updated_at"] = _now()
            self.write(job)
            return job

    def heartbeat(self, job_id: str, step: str | None = None, progress: dict | None = None) -> dict:
        """更新心跳；``step``/``progress`` 可选。"""
        job = self.get_or_404(job_id)
        if step is not None:
            job["step"] = step
        if progress is not None:
            job["progress"] = progress
        job["heartbeat_at"] = _now()
        job["updated_at"] = _now()
        self.write(job)
        return job

    def finish(self, job_id: str, status: str, last_error: dict | None = None) -> dict:
        """终态写回：succeeded|failed。"""
        if status not in {"succeeded", "failed"}:
            raise errors.validation_error("非法终态", {"status": status})
        job = self.get_or_404(job_id)
        job["status"] = status
        job["step"] = "done" if status == "succeeded" else job.get("step", "write")
        job["updated_at"] = _now()
        if last_error is not None:
            job["last_error"] = last_error
        self.write(job)
        return job

    def cancel(self, job_id: str) -> dict:
        """协作式取消：只写 cancel_requested=true；已终态返回当前状态。"""
        job = self.get_or_404(job_id)
        if job.get("status") not in {"queued", "running"}:
            return job  # 已终态：幂等返回当前状态
        job["cancel_requested"] = True
        job["updated_at"] = _now()
        self.write(job)
        return job

    def mark_unknown_if_stale(self, job_id: str, heartbeat_timeout: int = 120) -> dict | None:
        """running 心跳超时 → unknown（外部调用可能已发生，绝不重放微信写入）。"""
        job = self.load(job_id)
        if job is None or job.get("status") != "running":
            return None
        hb = job.get("heartbeat_at")
        if not hb:
            return None
        try:
            from datetime import datetime, timedelta, timezone

            last = datetime.fromisoformat(hb)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last <= timedelta(seconds=heartbeat_timeout):
                return None
        except ValueError:
            return None
        job["status"] = "unknown"
        job["updated_at"] = _now()
        if job.get("last_error") is None:
            job["last_error"] = {"code": "JOB_UNKNOWN", "message": "心跳超时，结果未知"}
        self.write(job)
        return job

    def resume(self, job_id: str) -> dict:
        """失败/取消 job 重试：复用上游 resume_run，保留已完成产物，生成新 attempt。"""
        job = self.get_or_404(job_id)
        if job.get("status") not in {"failed", "cancelled", "unknown"}:
            raise errors.validation_error(
                "仅失败/取消/未知状态的任务可重试", {"reason": "not_resumable", "status": job.get("status")}
            )
        run_id = job.get("run_id")
        from app.adapters import wewrite as wewrite_adapter

        adapter = wewrite_adapter.WeWriteAdapter()
        try:
            adapter.resume_run(run_id)
        except errors.ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise errors.upstream_error("上游恢复失败", {"reason": "resume_failed"}) from exc

        job["attempt"] = int(job.get("attempt") or 0) + 1
        job["status"] = "queued"
        job["cancel_requested"] = False
        job["step"] = "topic"
        job["last_error"] = None
        job["updated_at"] = _now()
        self.write(job)
        return job

    def accept_existing(self, job_id: str) -> dict:
        """仅记录用户接受已有结果，不改变完成状态、不伪造缺失产物。"""
        job = self.get_or_404(job_id)
        job["accepted_existing"] = True
        job["accepted_at"] = _now()
        job["updated_at"] = _now()
        self.write(job)
        return job

    def regenerate(self, job_id: str) -> dict:
        """JEE-1963：对「成功但字数不足」的任务发起一次重新生成（新建 run）。

        准入：job 必须 ``succeeded`` 且其 run state 带 ``length_warning``；普通
        ``succeeded``（无警告）不允许重试，既有语义不放宽。

        实现走「新建 run + 新 job」（路线 B）：上游 ``resume_run`` 对 completed run
        必抛 ValueError（``wewrite/runs.py`` 的 ``Completed runs are immutable``），
        因此改用已有的 ``adapter.create_run(...)`` 继承旧 run 的 topic/mode/visual
        配置新建一个 run；新 job 绑定**新 run_id**、``attempt=1``，并记
        ``regenerated_from`` 指向旧 job_id。旧 run 与旧 job 一律不动（旧 run 保持
        completed、旧 job 保持 succeeded、旧 run 的 length_warning 保留）。

        并发保护：以 ``regenerated_from`` 为准，同一旧 job 已存在 queued|running
        下游 job 时返回冲突（``already_regenerating``），连点两次只产生一次写作。
        整个「建 run + 建 job」在 ``PlatformLock`` 内，避免连点产生两个 run。
        """
        from app.adapters import wewrite as wewrite_adapter
        from app.storage.atomic import PlatformLock

        with PlatformLock(self.home):
            job = self.get_or_404(job_id)
            if job.get("status") != "succeeded":
                raise errors.conflict(
                    "仅字数不足的成功任务可重新生成",
                    {"reason": "not_regenerable", "status": job.get("status")},
                )
            old_run_id = job.get("run_id")
            old_state = paths.read_state_yaml(self.home, old_run_id)
            if not isinstance(old_state, dict) or not old_state.get("length_warning"):
                raise errors.conflict(
                    "该任务无字数不足警告，不可重新生成", {"reason": "no_length_warning"}
                )

            # 并发保护：同一旧 job 已有 queued|running 的下游 job 即冲突。
            for existing in self.list_jobs():
                if existing.get("regenerated_from") == job_id and existing.get("status") in {"queued", "running"}:
                    raise errors.conflict(
                        "该任务已有进行中的重新生成", {"reason": "already_regenerating", "job_id": existing.get("job_id")}
                    )

            topic = old_state.get("topic") or {}
            visual = old_state.get("visual") or {}
            adapter = wewrite_adapter.WeWriteAdapter()
            try:
                new_state = adapter.create_run(
                    topic=topic.get("title") or "",
                    mode=old_state.get("mode") or "draft",
                    visual_mode=visual.get("mode") or "none",
                    max_images=int(visual.get("max_images") or 0),
                    max_image_cost=visual.get("max_cost"),
                )
            except errors.ApiError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise errors.upstream_error("新建 run 失败", {"reason": "create_run_failed"}) from exc

            new_run_id = new_state.get("run_id")
            if not new_run_id:
                raise errors.upstream_error("上游返回缺少 run_id", {"reason": "missing_run_id"})

            # JEE-1967：上游 create_run 只接收 title 字符串，硬编码把 topic 其余字段
            # 置空（wewrite/runs.py::create_run），导致重新生成的 brief 丢失 keywords /
            # source / framework_hint。平台侧在新建 run 成功后、写 job 前，把旧 run 的
            # 这三个 topic 子字段补写回新 run 的 state.yaml（title 以新 run 为准）。
            self._backfill_topic_details(old_state, new_run_id)

            new_id = new_job_id()
            new_job = build_job(new_id, new_run_id, attempt=1)
            new_job["regenerated_from"] = job_id
            self.write(new_job)
            return new_job

    def _backfill_topic_details(self, old_state: dict, new_run_id: str) -> None:
        """把旧 run 的 topic 细节补写进新 run 的 state（JEE-1967）。

        只覆盖 ``keywords`` / ``source`` / ``framework_hint`` 三个子字段，
        ``title`` 以新 run 已有值为准（上游 create_run 已正确继承标题），其余
        字段一律不动。读取走 ``paths.read_state_yaml``，写回走
        ``paths.safe_write_run_state``；调用方已持有 ``PlatformLock``，此处不加锁。
        """
        old_topic = old_state.get("topic") if isinstance(old_state, dict) else None
        if not isinstance(old_topic, dict):
            return
        new_state = paths.read_state_yaml(self.home, new_run_id)
        if not isinstance(new_state, dict):
            return
        new_topic = new_state.setdefault("topic", {})
        if not isinstance(new_topic, dict):
            new_topic = {}
            new_state["topic"] = new_topic
        for field in ("keywords", "source", "framework_hint"):
            if field in old_topic:
                new_topic[field] = old_topic[field]
        paths.safe_write_run_state(
            self.home,
            new_run_id,
            yaml.safe_dump(new_state, allow_unicode=True, sort_keys=False),
        )

    def retry_images(self, job_id: str, indices: list[int]) -> dict:
        """失败图人工重试：每个失败索引最多一次，不重试成功图，超限 CONFLICT。"""
        job = self.get_or_404(job_id)
        images = job.get("images") or []
        to_retry = []
        for idx in indices:
            if not isinstance(idx, int) or idx < 0 or idx >= len(images):
                raise errors.validation_error("indices 越界", {"field": "indices"})
            img = images[idx]
            if img.get("status") == "succeeded":
                raise errors.conflict({"reason": "already_succeeded", "index": idx})
            if img.get("retried"):
                raise errors.conflict({"reason": "already_retried", "index": idx})
            to_retry.append(idx)
        for idx in to_retry:
            images[idx]["retried"] = True
            images[idx]["status"] = "pending"
            images[idx]["error"] = None
        job["images"] = images
        job["updated_at"] = _now()
        self.write(job)
        return job
