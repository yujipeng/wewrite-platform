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
