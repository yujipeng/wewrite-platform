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
        """JEE-1963：对「成功但字数不足」的任务发起一次重新生成。

        准入：job 必须 ``succeeded`` 且其 run state 带 ``length_warning``；普通
        ``succeeded``（无警告）不允许重试，既有语义不放宽。复用上游 ``resume_run``
        产生新的 queued job（新 attempt），不回改旧 job 的 ``succeeded``；同一 run
        已有未终态 job 时返回冲突（并发保护，连点两次只产生一次写作）。开始时清除
        旧 run 的 ``length_warning``，避免新任务未出结果时继续显示旧警告。
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
            run_id = job.get("run_id")
            # 并发保护优先：同 run 已有未终态 job 即冲突（连点两次只产生一次写作）。
            for existing in self.list_jobs():
                if existing.get("run_id") == run_id and existing.get("status") in {"queued", "running"}:
                    raise errors.conflict(
                        "该任务已有进行中的生成", {"reason": "already_regenerating", "job_id": existing.get("job_id")}
                    )
            state = paths.read_state_yaml(self.home, run_id)
            if not isinstance(state, dict) or not state.get("length_warning"):
                raise errors.conflict(
                    "该任务无字数不足警告，不可重新生成", {"reason": "no_length_warning"}
                )

            adapter = wewrite_adapter.WeWriteAdapter()
            try:
                adapter.resume_run(run_id)
            except errors.ApiError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise errors.upstream_error("上游恢复失败", {"reason": "resume_failed"}) from exc

            # 清除旧警告：新任务未出结果时不显示旧 length_warning（产出达标后也无警告）。
            self._clear_run_length_warning(run_id)

            new_id = new_job_id()
            new_job = build_job(new_id, run_id, attempt=int(job.get("attempt") or 0) + 1)
            self.write(new_job)
            return new_job

    def _clear_run_length_warning(self, run_id: str) -> None:
        """清掉 run state 里的 ``length_warning``（幂等，缺失不报错）。"""
        import yaml

        state = paths.read_state_yaml(self.home, run_id)
        if not isinstance(state, dict):
            return
        if "length_warning" not in state:
            return
        state.pop("length_warning", None)
        paths.safe_write_run_state(
            self.home, run_id, yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
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
