"""上游 ``wewrite`` 适配器：版本锁定 + 异常归一化。

按 docs/s2-technical-design.md 第 1/3 节，平台只调用公开函数。M3 只用到
``wewrite.runs.create_run``（confirm 时调用一次），其余能力后续里程碑接入。

上游事实（已实测）：
- ``create_run`` 是 keyword-only，参数为 ``topic/mode/visual_mode/max_images/
  max_image_cost/allow_publish``，**没有 ``run_id`` 入参**，``run_id`` 由上游生成
  后回填；不要试图预置。
- ``mode="publish"`` 时上游会强制 ``allow_publish=True``，这是绕过闸门的后门，
  平台在确认门入口就拒绝 ``mode=publish``，绝不让它到达上游。
"""

from __future__ import annotations

from app.domain import errors

# 平台只接受这两种 mode（设计第 3 节），其余一律拒绝。
ALLOWED_MODES = {"draft", "complete"}


class WeWriteAdapter:
    """包装 ``wewrite.runs``，归一化上游异常为 ApiError。"""

    def create_run(
        self,
        *,
        topic: str,
        mode: str,
        visual_mode: str,
        max_images: int,
        max_image_cost: float | None,
    ) -> dict:
        """调用上游 create_run（固定 allow_publish=false），返回上游 run state。"""
        if mode not in ALLOWED_MODES:
            raise errors.publish_disabled(
                "mode 只允许 draft|complete", {"reason": "mode_not_allowed", "mode": mode}
            )
        try:
            from wewrite.runs import create_run as _create_run
        except Exception as exc:  # 上游不可用
            raise errors.upstream_error("wewrite 包不可用", {"reason": "import_failed"}) from exc

        try:
            state = _create_run(
                topic=topic,
                mode=mode,
                visual_mode=visual_mode,
                max_images=max_images,
                max_image_cost=max_image_cost,
                allow_publish=False,
            )
        except ValueError as exc:
            raise errors.validation_error("上游拒绝创建 run", {"reason": "upstream_rejected"}) from exc
        except Exception as exc:
            raise errors.upstream_error("创建 run 失败", {"reason": "create_run_failed"}) from exc

        if not isinstance(state, dict) or not state.get("run_id"):
            raise errors.upstream_error("上游返回缺少 run_id", {"reason": "missing_run_id"})
        return state

    def resume_run(self, run_id: str) -> dict:
        """复用上游 ``wewrite.runs.resume_run``（保留已完成产物，生成新 attempt）。"""
        try:
            from wewrite.runs import resume_run as _resume
        except Exception as exc:  # 上游不可用
            raise errors.upstream_error("wewrite 包不可用", {"reason": "import_failed"}) from exc
        try:
            state = _resume(run_id)
        except ValueError as exc:
            raise errors.validation_error("上游拒绝恢复 run", {"reason": "upstream_rejected"}) from exc
        except Exception as exc:
            raise errors.upstream_error("恢复 run 失败", {"reason": "resume_failed"}) from exc
        return state
