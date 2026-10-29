"""确认门：候选 confirm / retract 的核心状态机。

按 docs/s2-technical-design.md 第 2.1 / 3 节：

- 锁内校验候选存在、``status=proposed``、``now < expires_at``、``content_hash`` 未变；
- intent journal 流程：写 ``intents/<id>.json``（``phase=prepared``）→ ``call_started``
  → 调上游 ``create_run(..., allow_publish=false)`` → 回填上游 ``run_id`` 写 ``bound``
  → 写 queued job → ``committed``；
- 同一 ``Idempotency-Key`` 重放返回原 job；不同 key 对已确认候选返回 409 CONFLICT；
- 任一校验不满足 → 409 CANDIDATE_NOT_ACTIONABLE，不得产生 run；
- ``mode=publish`` 在入口即 403 PUBLISH_DISABLED（堵住上游 create_run 的后门）；
- 撤回：仅 job 仍 queued 时可撤回，候选转 retracted，run 保留并标 topic.retracted=true。

全局锁 ``$WEWRITE_HOME/platform.lock``（fcntl.flock）跨进程有效；上游 wewrite.runs
自身不加锁，锁由平台实现。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from app.adapters import wewrite as wewrite_adapter
from app.domain import errors
from app.domain import scoring
from app.storage import atomic
from app.storage import candidates as candidates_store
from app.storage import jobs as jobs_store
from app.storage import paths


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_confirm_body(body: dict) -> dict:
    """校验 confirm 请求体字段，返回归一化 settings。"""
    if not isinstance(body, dict):
        raise errors.validation_error("请求体必须为 JSON 对象")
    candidate_id = body.get("candidate_id")
    content_hash = body.get("content_hash")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise errors.validation_error("candidate_id 必填", {"field": "candidate_id"})
    if not isinstance(content_hash, str) or not content_hash:
        raise errors.validation_error("content_hash 必填", {"field": "content_hash"})

    mode = body.get("mode")
    if mode == "publish":
        raise errors.publish_disabled({"reason": "mode_not_allowed", "mode": mode})
    if mode != "draft" and mode != "complete":
        raise errors.validation_error("mode 只允许 draft|complete", {"field": "mode"})

    visual_mode = body.get("visual_mode")
    if not isinstance(visual_mode, str) or not visual_mode:
        visual_mode = "full"

    try:
        max_images = int(body.get("max_images", 0))
    except (TypeError, ValueError):
        raise errors.validation_error("max_images 必须为整数", {"field": "max_images"})
    if max_images < 0 or max_images > 10:
        raise errors.validation_error("max_images 必须在 0–10", {"field": "max_images"})

    max_image_cost = body.get("max_image_cost", 0)
    try:
        max_image_cost = float(max_image_cost)
    except (TypeError, ValueError):
        raise errors.validation_error("max_image_cost 必须为数字", {"field": "max_image_cost"})
    if max_image_cost < 0:
        raise errors.validation_error("max_image_cost 必须 >= 0", {"field": "max_image_cost"})

    return {
        "candidate_id": candidate_id,
        "content_hash": content_hash,
        "mode": mode,
        "visual_mode": visual_mode,
        "max_images": max_images,
        "max_image_cost": max_image_cost,
    }


class ConfirmGate:
    """候选确认门。``adapter`` 可注入 fake WeWriteAdapter 供测试。"""

    def __init__(self, home: str, adapter: wewrite_adapter.WeWriteAdapter | None = None):
        self.home = home
        self.adapter = adapter or wewrite_adapter.WeWriteAdapter()
        self._candidate_store = candidates_store.CandidateStore(home)
        self._job_store = jobs_store.JobStore(home)

    # ---------- 撤回 ----------

    def retract(self, candidate_set_id: str, candidate_id: str) -> dict:
        """撤回候选：仅 job 仍 queued 时可撤回。"""
        with atomic.PlatformLock(self.home):
            data = self._candidate_store.get_or_404(candidate_set_id)
            item = self._find_item(data, candidate_id)

            if item.get("status") == "proposed":
                raise errors.job_not_retractable({"reason": "not_confirmed"})
            if item.get("status") == "retracted":
                raise errors.job_not_retractable({"reason": "already_retracted"})

            job_id = item.get("job_id") or self._find_job_id(data, candidate_id)
            if not job_id:
                raise errors.job_not_retractable({"reason": "no_job"})
            job = self._job_store.get_or_404(job_id)
            if job.get("status") != "queued":
                raise errors.job_not_retractable(
                    {"reason": "job_not_queued", "status": job.get("status")}
                )

            self._mark_item_retracted(data, item["id"])
            self._mark_run_retracted(job.get("run_id"))
            self._job_store.transition_to_cancelled(job)
            # 唯一选题（issue 3.2）：撤回后若集合内已无其它 confirmed 候选，
            # 集合从 confirmed 重新开放为 open，允许确认另一条。
            if not self._find_confirmed_other(data, item["id"]):
                data["set_status"] = "open"
                self._write_set(data)
            return {
                "candidate_set_id": candidate_set_id,
                "candidate_id": candidate_id,
                "status": "retracted",
                "job_id": job_id,
                "run_id": job.get("run_id"),
            }

    # ---------- 确认 ----------

    def confirm(
        self,
        candidate_set_id: str,
        settings: dict,
        idempotency_key: str,
    ) -> dict:
        """锁内执行候选确认；返回 ``{run_id, job_id}``。"""
        if not idempotency_key:
            raise errors.validation_error("缺少 Idempotency-Key 请求头", {"field": "Idempotency-Key"})

        with atomic.PlatformLock(self.home):
            data = self._candidate_store.get_or_404(candidate_set_id)

            # 幂等重放：同一 key 已 committed 直接返回原 job
            replay = self._find_intent_by_key(candidate_set_id, idempotency_key)
            if replay is not None and replay.get("phase") == "committed" and replay.get("job_id"):
                return {"run_id": replay.get("run_id"), "job_id": replay.get("job_id")}

            item = self._find_item(data, settings["candidate_id"])

            # 不同 idempotency key 对已确认候选返回 409 CONFLICT（设计第 3 节）
            if item.get("status") == "confirmed":
                raise errors.conflict(
                    "候选已确认", {"reason": "already_confirmed", "candidate_id": settings["candidate_id"]}
                )

            # 唯一选题（issue 3.2）：同一候选集内已有其它候选处于 confirmed 且 run 仍有效，
            # 再次确认另一条 → 409 CANDIDATE_NOT_ACTIONABLE reason=set_already_confirmed，
            # 并带上已确认的 candidate_id 与 run_id。若已确认那条已 retract，则允许确认另一条。
            confirmed_other = self._find_confirmed_other(data, settings["candidate_id"])
            if confirmed_other is not None:
                raise errors.candidate_not_actionable(
                    "set_already_confirmed",
                    details={
                        "reason": "set_already_confirmed",
                        "confirmed_candidate_id": confirmed_other.get("id"),
                        "confirmed_run_id": confirmed_other.get("run_id"),
                    },
                )

            reason = self._actionable_reason(data, item)
            if reason is not None:
                raise errors.candidate_not_actionable(reason)

            # content_hash 重算比对：按候选正文关键字段（title/angle/framework/
            # source_id/source_url）稳定重算，与请求值比对，防止正文与保存哈希同时被篡改。
            recomputed = scoring.content_hash(item)
            if recomputed != settings["content_hash"]:
                raise errors.candidate_not_actionable("hash_changed")
            if item.get("content_hash") != settings["content_hash"]:
                raise errors.candidate_not_actionable("hash_changed")

            intent_id = f"{candidate_set_id}-{settings['candidate_id']}"
            intent = {
                "idempotency_key": idempotency_key,
                "correlator": idempotency_key,
                "candidate_set_id": candidate_set_id,
                "candidate_id": settings["candidate_id"],
                "content_hash": settings["content_hash"],
                "snapshot_sha256": data.get("snapshot_sha256"),
                "style_revision": data.get("style_revision"),
                "creation_settings": {
                    "mode": settings["mode"],
                    "visual_mode": settings["visual_mode"],
                    "max_images": settings["max_images"],
                    "max_image_cost": settings["max_image_cost"],
                },
                "run_ids_before": self._list_run_ids(),
                "job_id": None,
                "phase": "prepared",
                "run_id": None,
            }
            self._write_intent(intent_id, intent)

            intent["phase"] = "call_started"
            self._write_intent(intent_id, intent)

            state = self.adapter.create_run(
                topic=item.get("title") or "",
                mode=settings["mode"],
                visual_mode=settings["visual_mode"],
                max_images=settings["max_images"],
                max_image_cost=settings["max_image_cost"],
            )
            run_id = state["run_id"]

            intent["run_id"] = run_id
            intent["phase"] = "bound"
            self._write_intent(intent_id, intent)

            job_id = jobs_store.new_job_id()
            job = jobs_store.build_job(job_id, run_id)
            self._job_store.write(job)
            intent["job_id"] = job_id
            intent["phase"] = "committed"
            self._write_intent(intent_id, intent)

            self._mark_item_confirmed(data, settings["candidate_id"], run_id, job_id)

            return {"run_id": run_id, "job_id": job_id}

    # ---------- 内部 ----------

    def _find_item(self, data: dict, candidate_id: str) -> dict:
        items = data.get("items") or []
        for it in items:
            if it.get("id") == candidate_id:
                return it
        raise errors.candidate_not_actionable("not_proposed")

    def _find_confirmed_other(self, data: dict, candidate_id: str) -> dict | None:
        """返回集合内除 ``candidate_id`` 外仍处 confirmed 且 run 仍有效的候选。

        「唯一选题」判定：已确认的候选若已 retract（status=retracted）不在此列；
        已确认但 run/job 已失效（被清理）的候选同样不在此列，因此 run 失效后
        set_already_confirmed 不再成立，允许确认另一条。
        """
        for it in data.get("items") or []:
            if it.get("id") == candidate_id:
                continue
            if it.get("status") == "confirmed" and self._confirmed_run_valid(it):
                return it
        return None

    def _confirmed_run_valid(self, item: dict) -> bool:
        """判定某 confirmed 候选的 run 是否仍有效（需求 3.2 前置条件「run_id 仍有效」）。

        有效性落到磁盘真实存在性：run 的 state.yaml 可读且 run_id 匹配；若带 job_id，
        job 文件也必须仍存在。run/job 任一被清理即视为失效，不再阻挡确认另一条。
        """
        run_id = item.get("run_id")
        if not run_id:
            return False
        state = paths.read_state_yaml(self.home, run_id)
        if not isinstance(state, dict) or not state.get("run_id"):
            return False
        job_id = item.get("job_id")
        if job_id and self._job_store.load(job_id) is None:
            return False
        return True

    def _has_valid_confirmation(self, data: dict) -> bool:
        """集合内是否仍有任一「run 仍有效」的 confirmed 候选。"""
        for it in data.get("items") or []:
            if it.get("status") == "confirmed" and self._confirmed_run_valid(it):
                return True
        return False

    def _actionable_reason(self, data: dict, item: dict | None) -> str | None:
        if item is None:
            return "not_proposed"
        if not self._set_open_for_confirm(data):
            return "set_closed"
        if item.get("status") != "proposed":
            return "not_proposed"
        if candidates_store.is_expired(data):
            return "expired"
        return None

    def _set_open_for_confirm(self, data: dict) -> bool:
        """确认前置的集合开放判定。

        正常 open 集可确认；已确认集（set_status=confirmed）在「唯一确认的那条 run
        已失效/被清理」时视为可重新确认（否则永远被 set_closed 锁死）。
        """
        status = data.get("set_status")
        if status == "open":
            return True
        if status == "confirmed":
            return not self._has_valid_confirmation(data)
        return False

    def _find_job_id(self, data: dict, candidate_id: str) -> str | None:
        intent = self._read_intent(f"{data.get('candidate_set_id')}-{candidate_id}")
        if intent:
            return intent.get("job_id")
        return None

    def _read_intent(self, intent_id: str) -> dict | None:
        text = paths.safe_read_subdir_text(self.home, "intents", f"{intent_id}.json")
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _write_intent(self, intent_id: str, data: dict) -> None:
        paths.safe_write_subdir_file(
            self.home, "intents", f"{intent_id}.json", json.dumps(data, ensure_ascii=False, indent=2)
        )

    def _find_intent_by_key(self, candidate_set_id: str, idempotency_key: str) -> dict | None:
        prefix = f"{candidate_set_id}-"
        for fname in paths.safe_list_subdir_filenames(self.home, "intents"):
            if not fname.startswith(prefix) or not fname.endswith(".json"):
                continue
            intent = self._read_intent(fname[:-5])
            if intent and intent.get("idempotency_key") == idempotency_key:
                return intent
        return None

    def _list_run_ids(self) -> list[str]:
        from app.storage import projections

        return [r.run_id for r in projections.list_runs(self.home)]

    def _mark_item_confirmed(self, data: dict, candidate_id: str, run_id: str, job_id: str) -> None:
        for it in data.get("items") or []:
            if it.get("id") == candidate_id:
                it["status"] = "confirmed"
                it["run_id"] = run_id
                it["job_id"] = job_id
        data["set_status"] = "confirmed"
        self._write_set(data)

    def _mark_item_retracted(self, data: dict, candidate_id: str) -> None:
        for it in data.get("items") or []:
            if it.get("id") == candidate_id:
                it["status"] = "retracted"
        self._write_set(data)

    def _write_set(self, data: dict) -> None:
        paths.safe_write_subdir_file(
            self.home,
            "candidates",
            f"{data['candidate_set_id']}.json",
            json.dumps(data, ensure_ascii=False, indent=2),
        )

    def _mark_run_retracted(self, run_id: str) -> None:
        """run 保留并标 topic.retracted=true（只读投影路径，不调上游 load_run）。"""
        import yaml

        try:
            fd, _st = paths.safe_open_state(self.home, run_id)
        except errors.ApiError:
            return
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            state = yaml.safe_load(f) or {}
        topic = state.setdefault("topic", {})
        topic["retracted"] = True
        paths.safe_write_run_state(
            self.home, run_id, yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
        )
