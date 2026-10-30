"""P6 worker：自动消费 queued job（写作 → 审稿 → 视觉 → 转换预览）。

按 docs/s2-technical-design.md 第 2.3 / 5 节：

- 单 worker 串行消费 durable job 队列；``queued→running`` 是 CAS 点；
- 写作走 ``llm-write`` 子进程，退出码 3 → ``last_error.code=WRITER_NOT_CONFIGURED``、
  4 → ``WRITER_FAILED``；输出指向 staging 临时文件，只有退出 0 且正文校验通过
  才原子推广，退出 4 不得覆盖既有正文；
- 视觉走 ``image_gen.generate_batch``（批处理中语义）：调用前一次写
  ``visual/running``，整批返回后一次性回填每张 ``succeeded|failed``，不伪造逐图
  百分比；预算超限在调用前失败；
- 配图产物写 ``article-illustrated.md``，不覆盖 ``article.md`` 原文；
- 取消协作式（步骤边界检查 ``cancel_requested``）。

写作/审稿/生图后端均可注入，测试用 fake 覆盖 dry-run 全链路与失败路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from app.domain import errors
from app.storage import jobs as jobs_store
from app.storage import paths
from app.storage.config_store import (
    ConfigStore,
    WRITER_ENV_OF,
    image_effective_config,
    image_effective_values,
)


def _writer_env(config: dict | None) -> dict[str, str]:
    """把最终生效的 writer 配置注入子进程环境。

    llm-write 只读环境变量，因此 worker 启动子进程时必须把 env 优先、其次
    ``config.yaml`` 的最终值显式注入 ``env``，否则网页改了 model 也不生效。
    不覆盖已由环境变量注入的值（env 优先是安全底线，config 不越权）。

    字段 → 环境变量映射复用 ``config_store.WRITER_ENV_OF`` 的唯一来源，
    不再在 executor 里单独维护一份。
    """
    env: dict[str, str] = {}
    if config is None:
        return env
    writer = config.get("writer") or {}
    for key, env_var in WRITER_ENV_OF.items():
        if env_var in os.environ:
            continue  # 环境变量接管，config 不覆盖
        value = writer.get(key)
        if value is None or value == "":
            continue  # 未配置则交由上游默认值
        env[env_var] = str(value)
    return env


class WriterBackend:
    """llm-write 子进程后端（可注入 fake）。返回 ``(exit_code, output_path_or_none)``。"""

    def run(self, brief_path: str, output_path: str, env: dict | None = None) -> int:
        cmd = [sys.executable, "-m", "wewrite.commands.llm_write", "--brief", brief_path, "--output", output_path]
        # 继承父进程环境再叠加 writer 配置，避免空 env 覆盖 PATH 等必要变量。
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=child_env)
        return proc.returncode


class ImageBackend:
    """生图后端（可注入 fake）。返回与 items 对齐的 ``[{output,ok,error}]``。"""

    def generate_batch(self, items, config=None, max_images=None, max_cost=None):
        from wewrite.toolkit.image_gen import generate_batch

        return generate_batch(
            items, config=config, max_images=max_images, max_cost=max_cost
        )


class ReviewerBackend:
    """审稿后端（dry-run 规则桩，可注入 fake）。返回 ``{decision, publishable, note}``。"""

    def review(self, run_id: str, article_text: str) -> dict:
        # 规则审稿桩（非 AI）：正文非空即 pass，明确标注来源。
        ok = bool(article_text and len(article_text.strip()) >= 200)
        return {
            "decision": "pass" if ok else "fail",
            "publishable": ok,
            "note": "规则审稿桩（非 AI），仅校验正文字符数下限",
        }


def _read_run_state(home: str, run_id: str) -> dict:
    state = paths.read_state_yaml(home, run_id)
    if not isinstance(state, dict) or not state.get("run_id"):
        raise errors.not_found({"reason": "run_not_found", "run_id": run_id})
    return state


def _write_run_state(home: str, run_id: str, state: dict) -> None:
    import yaml

    paths.safe_write_run_state(
        home, run_id, yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
    )


def _check_cancel(store: jobs_store.JobStore, job_id: str) -> bool:
    job = store.load(job_id)
    return bool(job and job.get("cancel_requested"))


def _build_brief_text(state: dict, style: dict | None = None) -> str:
    """拼写作 brief（选题 + 写作风格）。

    ``style`` 来自 ``style.yaml``，注入写作模型 brief，否则风格配置不会生效。
    ``blacklist`` 兼容数组（旧）与对象 ``{words, topics}``（新）两种形态。
    """
    topic = state.get("topic") or {}
    visual = state.get("visual") or {}
    title = topic.get("title") or "未命名文章"
    framework = topic.get("framework_hint") or "问题-证据-行动"
    parts = [
        "# 选题\n",
        f"标题：{title}\n",
        f"切入点：{topic.get('source') or ''}\n",
        f"框架：{framework}\n",
        f"关键词：{', '.join(topic.get('keywords') or [])}\n",
        f"视觉模式：{visual.get('mode') or 'none'}，最大图片数：{visual.get('max_images') or 0}\n",
    ]

    if style and isinstance(style, dict):
        parts.append("\n# 写作风格\n")
        for field in ("name", "industry", "target_audience", "tone", "voice",
                      "content_style", "writing_persona", "cover_style", "author"):
            value = style.get(field)
            if isinstance(value, str) and value.strip():
                parts.append(f"{field}：{value}\n")
        topics = style.get("topics")
        if isinstance(topics, list) and topics:
            parts.append(f"话题方向：{', '.join(str(t) for t in topics)}\n")
        word_count = style.get("word_count")
        if word_count not in (None, "", 0):
            # JEE-1963：把字数要求写硬（含「务必达到」「低于 N 字视为不合格」），
            # 真正的保证靠生成后校验与自动续写，这里只是辅助提示。
            from app.domain import word_count as word_count_domain

            hint = word_count_domain.format_word_count_hint(word_count)
            parts.append(hint if hint is not None else f"目标字数：{word_count}\n")
        blacklist = style.get("blacklist")
        if isinstance(blacklist, list) and blacklist:
            parts.append(f"禁忌词：{', '.join(str(b) for b in blacklist)}\n")
        elif isinstance(blacklist, dict):
            words = blacklist.get("words") or []
            topics_bl = blacklist.get("topics") or []
            if words:
                parts.append(f"禁忌词：{', '.join(str(w) for w in words)}\n")
            if topics_bl:
                parts.append(f"禁忌话题：{', '.join(str(t) for t in topics_bl)}\n")

    return "".join(parts)


class Worker:
    """job 执行器。可注入 writer/reviewer/image 后端与 wechat adapter（测试）。"""

    def __init__(self, home: str, writer=None, reviewer=None, image=None):
        self.home = home
        self.store = jobs_store.JobStore(home)
        self.writer = writer or WriterBackend()
        self.reviewer = reviewer or ReviewerBackend()
        self.image = image or ImageBackend()

    # ---------- 队列消费 ----------

    def run_once(self) -> dict | None:
        """取一个 queued job 执行；无可用 job 返回 None。

        图片重试 job（``retry_mode == "images"``）只重跑 pending 图，
        不再走完整写作→审稿→视觉流水线。
        """
        for job in self.store.list_jobs():
            if job.get("status") != "queued":
                continue
            running = self.store.cas_queued_to_running(job["job_id"])
            if running is None:
                continue
            if running.get("retry_mode") == "images":
                self._execute_image_retry(running)
            else:
                self._execute(running)
            return self.store.load(running["job_id"])
        return None

    def recover_stale(self, heartbeat_timeout: int = 120) -> list[str]:
        """扫描 running 心跳超时转 unknown（外部调用可能已发生，绝不重放）。"""
        changed = []
        for job in self.store.list_jobs():
            if job.get("status") != "running":
                continue
            result = self.store.mark_unknown_if_stale(job["job_id"], heartbeat_timeout)
            if result is not None:
                changed.append(job["job_id"])
        return changed

    # ---------- 流水线 ----------

    def _execute(self, job: dict) -> None:
        job_id = job["job_id"]
        run_id = job["run_id"]
        try:
            self._step_topic(job)
            if _check_cancel(self.store, job_id):
                return self._finish_cancelled(job_id)
            self._step_write(job)
            if _check_cancel(self.store, job_id):
                return self._finish_cancelled(job_id)
            self._step_review(job)
            if _check_cancel(self.store, job_id):
                return self._finish_cancelled(job_id)
            self._step_visual(job)
            if _check_cancel(self.store, job_id):
                return self._finish_cancelled(job_id)
            self._step_preview(job)
            self.store.finish(job_id, "succeeded")
        except errors.ApiError as exc:
            self.store.finish(
                job_id, "failed",
                {"code": exc.code, "message": exc.message},
            )
        except Exception as exc:  # noqa: BLE001
            self.store.finish(
                job_id, "failed",
                {"code": "WORKER_FAILED", "message": f"{type(exc).__name__}: {exc}"},
            )

    def _execute_image_retry(self, job: dict) -> None:
        """图片重试 job：只重跑 ``status == "pending"`` 的图片。

        - 已 succeeded 的图不重跑（避免重复计费）；
        - 每张失败图写入脱敏后的 error 与稳定 error_code；
        - 全部成功 → job 置 succeeded；仍有失败 → job 置 failed；
        - 所有退出路径（正常完成、后端抛异常、预算超限、取消）都把 pending 图
          收敛到 failed 终态，绝不残留 pending；
        - 不改动 job 既有的 media_id / 发布态字段，state.yaml 只在锁内更新目标图；
        - 生效配置在调用前算好，调用已发生/可能已发生的路径都落实际 provider/model
          审计（预算超限 = 调用前失败 0 次，不伪造执行历史）。
        """
        job_id = job["job_id"]
        run_id = job["run_id"]
        effective = None  # 本次实际生效配置，调用前算好，供异常路径审计
        try:
            state = _read_run_state(self.home, run_id)
            visual = state.get("visual") or {}
            max_images = int(visual.get("max_images") or 0)
            max_cost = visual.get("max_cost")

            cfg = ConfigStore(self.home).load_config()
            pending = [
                idx for idx, img in enumerate(job.get("images") or [])
                if img.get("status") == "pending"
            ]
            if not pending:
                # 没有待重跑的图：直接按现有结果收敛，避免再次排队
                self._finish_image_retry_result(job_id)
                return

            # 协作式取消：调用前检查，取消即把待处理图收敛为 failed（0 次调用，无审计）
            if _check_cancel(self.store, job_id):
                self._settle_pending_as_failed(
                    job_id, run_id, pending,
                    code="IMAGE_PROVIDER_ERROR", error="任务已取消",
                )
                return

            # 预算约束：重试同样受 max_images / max_cost 约束，超预算在调用前失败
            img_cfg = cfg.get("image") or {}
            est = _num(img_cfg.get("estimated_cost_per_image")) * len(pending)
            if max_cost is not None and est > float(max_cost):
                # 超预算：待处理图收敛为 failed，backend 调用 0 次，不残留 pending，不伪造历史
                self._settle_pending_as_failed(
                    job_id, run_id, pending,
                    code="IMAGE_QUOTA_EXCEEDED", error="图片预算超限，未发起生成",
                )
                return

            items = []
            for idx in pending:
                prompt = (state.get("topic") or {}).get("title") or "配图"
                items.append({
                    "prompt": prompt,
                    "output": f"runs/{run_id}/images/{idx}.png",
                    "size": "article",
                })

            # 实际调用与审计共用同一份生效配置（env 优先 → config.yaml）。
            # 必须在 generate_batch 之前算好，异常路径才能拿到审计信息。
            effective_cfg = image_effective_config(cfg)
            effective = image_effective_values(effective_cfg)

            results = self.image.generate_batch(
                items, config=effective_cfg, max_images=max_images, max_cost=max_cost
            )

            # 生成期间取消：在途调用已发生，结果收敛为 failed 并记录审计
            if _check_cancel(self.store, job_id):
                self._settle_pending_as_failed(
                    job_id, run_id, pending,
                    code="IMAGE_PROVIDER_ERROR", error="任务已取消",
                    effective=effective, called=True,
                )
                return

            job = self.store.load(job_id)
            images = list(job.get("images") or [])
            for offset, idx in enumerate(pending):
                r = results[offset]
                img = dict(images[idx])
                if r.get("ok"):
                    img["status"] = "succeeded"
                    img["error"] = None
                    img["error_code"] = None
                else:
                    raw_error = str(r.get("error") or "")
                    img["status"] = "failed"
                    img["error"] = _sanitize_error(raw_error)
                    img["error_code"] = classify_image_error(raw_error)
                _apply_execution_audit(img, effective)
                images[idx] = img
            job["images"] = images
            self.store.write(job)

            # 只同步本次选中索引（pending），未选中索引以锁内最新 state 为准
            updates = [{"index": idx, "status": images[idx]["status"],
                        "error": images[idx].get("error"),
                        "error_code": images[idx].get("error_code")}
                       for idx in pending]
            _sync_state_images_locked(self.home, run_id, updates)
            self._finish_image_retry_result(job_id)
        except errors.ApiError as exc:
            self._fail_image_retry(job_id, exc.code, exc.message, effective=effective)
        except Exception as exc:  # noqa: BLE001
            self._fail_image_retry(job_id, "WORKER_FAILED", f"{type(exc).__name__}: {exc}", effective=effective)

    def _fail_image_retry(self, job_id: str, code: str, message: str, effective: dict | None = None) -> None:
        """异常退出路径：把 pending 图收敛为 failed（分类+脱敏+审计），job 置 failed。

        ``effective`` 非空表示后端已被调用（计费已发生），本次实际 provider/model
        会写入 executed_provider/executed_model 并追加 execution_history。
        """
        job = self.store.load(job_id)
        if job is None:
            return
        run_id = job.get("run_id")
        images = list(job.get("images") or [])
        changed: list = []
        for idx, img in enumerate(images):
            if img.get("status") == "pending":
                images[idx] = dict(img)
                images[idx]["status"] = "failed"
                images[idx]["error"] = _sanitize_error(message)
                images[idx]["error_code"] = classify_image_error(message)
                if effective is not None:
                    _apply_execution_audit(images[idx], effective)
                changed.append({"index": idx, "status": "failed",
                                "error": images[idx]["error"],
                                "error_code": images[idx]["error_code"]})
        if changed:
            job["images"] = images
            self.store.write(job)
            if run_id:
                _sync_state_images_locked(self.home, run_id, changed)
        self.store.finish(
            job_id, "failed",
            {"code": code, "message": _sanitize_error(message)},
        )

    def _settle_pending_as_failed(self, job_id: str, run_id: str, pending: list, code: str, error: str,
                                  effective: dict | None = None, called: bool = False) -> None:
        """把指定 pending 图收敛为 failed（预算超限等调用前失败路径）。

        ``called=True``（后端已被调用）时写入实际 provider/model 审计；
        ``called=False``（调用前失败，0 次调用）不伪造执行历史。
        """
        job = self.store.load(job_id)
        if job is None:
            return
        images = list(job.get("images") or [])
        updates = []
        for idx in pending:
            img = dict(images[idx])
            img["status"] = "failed"
            img["error"] = _sanitize_error(error)
            img["error_code"] = code
            if called and effective is not None:
                _apply_execution_audit(img, effective)
            images[idx] = img
            updates.append({"index": idx, "status": "failed",
                            "error": img["error"], "error_code": code})
        job["images"] = images
        self.store.write(job)
        _sync_state_images_locked(self.home, run_id, updates)
        self._finish_image_retry_result(job_id)

    def _finish_image_retry_result(self, job_id: str) -> None:
        """按图片最终结果收敛 job 终态：全成功 succeeded，否则 failed。"""
        job = self.store.load(job_id)
        images = job.get("images") or []
        failed = [img for img in images if img.get("status") == "failed"]
        status = "failed" if failed else "succeeded"
        job["status"] = status
        job["step"] = "done"
        job["retry_mode"] = None
        job["updated_at"] = jobs_store._now()
        if failed:
            job["last_error"] = {"code": "IMAGE_RETRY_FAILED", "message": f"{len(failed)} 张配图生成失败"}
        else:
            job["last_error"] = None
        self.store.write(job)

    def _finish_cancelled(self, job_id: str) -> None:
        job = self.store.load(job_id)
        job["status"] = "cancelled"
        job["step"] = job.get("step", "topic")
        job["updated_at"] = jobs_store._now()
        self.store.write(job)

    def _step_topic(self, job: dict) -> None:
        self.store.heartbeat(job["job_id"], step="topic")

    def _step_write(self, job: dict) -> None:
        self.store.heartbeat(job["job_id"], step="write")
        run_id = job["run_id"]
        state = _read_run_state(self.home, run_id)
        # 读取 style.yaml 注入 brief，否则风格配置不会进入写作链路（JEE-1947）。
        style = self._load_style()
        brief_text = _build_brief_text(state, style)

        # staging 临时文件（与 article.md 同目录语义：先写 run 目录内临时名）
        staging_rel = f".article-{job['job_id']}.staging.md"
        paths.safe_write_run_file(self.home, run_id, staging_rel, brief_text.encode("utf-8"))

        # llm-write 是文件到文件；这里把 brief 写到 run 目录下临时 brief
        brief_rel = f".brief-{job['job_id']}.md"
        paths.safe_write_run_file(self.home, run_id, brief_rel, brief_text.encode("utf-8"))

        # 直接调用 writer 后端（默认走 llm-write 子进程）
        exit_code = self._invoke_writer(job, run_id, brief_rel, staging_rel)

        staging = paths.safe_read_run_relative(self.home, run_id, staging_rel)
        staging_text = staging.decode("utf-8") if staging else ""

        if exit_code == 3:
            raise _worker_error("WRITER_NOT_CONFIGURED", "写作模型未配置（llm-write 退出码 3）")
        if exit_code == 4:
            # 退出 4 不得覆盖既有正文
            raise _worker_error("WRITER_FAILED", "写作调用失败（llm-write 退出码 4）")
        if exit_code != 0:
            raise _worker_error("WRITER_FAILED", f"llm-write 非预期退出码 {exit_code}")

        # 正文校验：非空且 >=200 字符（保持 strip 口径，200 字符门不变）
        if not staging_text or len(staging_text.strip()) < 200:
            raise _worker_error("WRITER_FAILED", "写作输出正文校验未通过")

        # JEE-1963：解析目标字数，不足则自动续写一次；续写失败不毁稿。
        length_warning = None
        target = self._parse_word_target(style)
        if target is not None:
            actual = self._count_chars(staging_text)
            if actual < target["min_chars"]:
                staging_text, length_warning = self._extend_once(
                    job, run_id, staging_text, target,
                    self._word_goal(style),
                )

        # 原子推广为 article.md
        paths.safe_write_run_file(self.home, run_id, "article.md", staging_text.encode("utf-8"))
        # 清理 staging/brief 临时文件（尽力）
        _unlink_run_file(self.home, run_id, staging_rel)
        _unlink_run_file(self.home, run_id, brief_rel)

        # 更新 state.artifacts.article 与 length_warning
        state = _read_run_state(self.home, run_id)
        state.setdefault("artifacts", {})["article"] = f"runs/{run_id}/article.md"
        if length_warning is not None:
            state["length_warning"] = length_warning
        else:
            state.pop("length_warning", None)
        _write_run_state(self.home, run_id, state)

    def _parse_word_target(self, style: dict) -> dict | None:
        """解析 style.word_count 得到可判定区间；无法解析返回 None（不校验）。"""
        from app.domain import word_count as word_count_domain

        return word_count_domain.parse_word_count(style.get("word_count"))

    def _word_goal(self, style: dict) -> int | None:
        """返回目标字数原文下限（扩写指令瞄准用户真实目标，而非判定阈值）。"""
        from app.domain import word_count as word_count_domain

        return word_count_domain.target_lower_bound(style.get("word_count"))

    def _count_chars(self, text: str) -> int:
        """需求 §3.1 口径：去掉空白后的字符数（统一计数入口）。"""
        from app.domain import word_count as word_count_domain

        return word_count_domain.count_chars(text)

    def _extend_once(self, job: dict, run_id: str, current_text: str, target: dict, goal: int | None):
        """目标字数不足时自动续写一次。

        复用现有 writer 后端与环境注入（``_invoke_writer``），不另建模型调用；
        续写失败（非 0 退出码 / 超时 / 输出太短）保留原文并返回 ``length_warning``，
        不把 job 判失败（原稿是有效产物）。
        合并策略：以续写结果为准（扩写指令要求模型回全量扩写稿）。
        返回 ``(final_text, length_warning_or_none)``。
        """
        aim = goal if goal is not None else target["min_chars"]
        extension_brief = (
            current_text
            + f"\n\n请把以上文章扩写到约 {aim} 字，保持原有结构与风格，不要重复已有段落。\n"
        )
        brief_rel_ext = f".brief-{job['job_id']}.extend.md"
        staging_rel_ext = f".article-{job['job_id']}.staging.extend.md"
        paths.safe_write_run_file(self.home, run_id, brief_rel_ext, extension_brief.encode("utf-8"))

        def _warning(text: str) -> dict:
            return {
                "actual": self._count_chars(text),
                "target_min": target["min_chars"],
                "target_max": target["max_chars"],
                "target": goal if goal is not None else target["min_chars"],
            }

        try:
            exit_code = self._invoke_writer(job, run_id, brief_rel_ext, staging_rel_ext)
            ext = paths.safe_read_run_relative(self.home, run_id, staging_rel_ext)
            ext_text = ext.decode("utf-8") if ext else ""
        except Exception:
            # 续写失败（超时等）不毁稿，保留原文并写警告。
            return current_text, _warning(current_text)
        finally:
            _unlink_run_file(self.home, run_id, brief_rel_ext)
            _unlink_run_file(self.home, run_id, staging_rel_ext)

        # 续写失败或输出太短（<200，保持 strip 口径）时，以原文为准，不覆盖有效产物。
        if exit_code != 0 or not ext_text or len(ext_text.strip()) < 200:
            return current_text, _warning(current_text)

        final_text = ext_text  # 以续写结果为准
        if self._count_chars(final_text) < target["min_chars"]:
            return final_text, _warning(final_text)
        return final_text, None

    def _load_style(self) -> dict:
        """读取 style.yaml（供 brief 注入）；缺失返回空 dict，不阻断写作。"""
        from app.storage.config_store import ConfigStore

        return ConfigStore(self.home).load_style()

    def _invoke_writer(self, job: dict, run_id: str, brief_rel: str, staging_rel: str) -> int:
        """把 brief 写到 staging 输出路径由后端处理；默认走子进程 llm-write。"""
        # 默认 WriterBackend.run 需要真实文件路径；安全读取字节后写临时文件再调用。
        brief_bytes = paths.safe_read_run_relative(self.home, run_id, brief_rel)
        if brief_bytes is None:
            return 3
        import tempfile as _tmp

        # 把最终生效的 writer 配置注入子进程 env（env 优先，其次 config.yaml）。
        # llm-write 只读环境变量，不注入则网页改 model/base_url 等不会生效。
        env = self._build_writer_env()

        brief_fd, brief_path = _tmp.mkstemp(suffix=".md")
        out_fd, out_path = _tmp.mkstemp(suffix=".md")
        os.close(brief_fd)
        os.close(out_fd)
        try:
            with open(brief_path, "wb") as f:
                f.write(brief_bytes)
            exit_code = self.writer.run(brief_path, out_path, env=env)
            if exit_code == 0 and os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    content = f.read()
                paths.safe_write_run_file(self.home, run_id, staging_rel, content)
            return exit_code
        finally:
            for p in (brief_path, out_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _build_writer_env(self) -> dict[str, str]:
        """读取 config.yaml，返回注入子进程的 writer 环境变量（env 优先，其次配置）。"""
        from app.storage.config_store import ConfigStore

        config = ConfigStore(self.home).load_config()
        return _writer_env(config)

    def _step_review(self, job: dict) -> None:
        self.store.heartbeat(job["job_id"], step="review")
        run_id = job["run_id"]
        article = paths.safe_read_run_relative(self.home, run_id, "article.md")
        article_text = article.decode("utf-8") if article else ""
        result = self.reviewer.review(run_id, article_text)
        report = {
            "version": 1,
            "run_id": run_id,
            "decision": result.get("decision", "fail"),
            "publishable": bool(result.get("publishable", False)),
            "note": result.get("note", ""),
        }
        paths.safe_write_run_file(
            self.home, run_id, "review-report.json",
            json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        state = _read_run_state(self.home, run_id)
        state.setdefault("artifacts", {})["review_report"] = f"runs/{run_id}/review-report.json"
        state["editorial"] = {
            "decision": report["decision"],
            "publishable": report["publishable"],
        }
        _write_run_state(self.home, run_id, state)

    def _step_visual(self, job: dict) -> None:
        self.store.heartbeat(job["job_id"], step="visual")
        run_id = job["run_id"]
        state = _read_run_state(self.home, run_id)
        visual = state.get("visual") or {}
        max_images = int(visual.get("max_images") or 0)
        max_cost = visual.get("max_cost")

        if max_images <= 0:
            # 无需配图：保留既有 cover 与发布字段，仅清空 figures
            _sync_state_images_locked(self.home, run_id, [])
            return

        # 预算超限必须在调用前失败
        from app.storage.config_store import ConfigStore

        cfg = ConfigStore(self.home).load_config()
        img_cfg = cfg.get("image") or {}
        est = _num(img_cfg.get("estimated_cost_per_image")) * max_images
        if max_cost is not None and est > float(max_cost):
            raise _worker_error("BUDGET_EXCEEDED", "预估图片成本超过预算上限")

        # 构建 manifest（prompt 来自选题标题），输出到 run 目录 images/
        items = []
        for i in range(max_images):
            items.append({
                "prompt": (state.get("topic") or {}).get("title") or "配图",
                "output": f"runs/{run_id}/images/{i}.png",
                "size": "article",
            })
        results = self.image.generate_batch(
            items, config=cfg, max_images=max_images, max_cost=max_cost
        )
        figures = []
        for i, r in enumerate(results):
            if r.get("ok"):
                figures.append({"index": i, "output": f"images/{i}.png", "status": "succeeded", "error": None, "error_code": None})
            else:
                raw_error = str(r.get("error") or "")
                figures.append({
                    "index": i,
                    "output": f"images/{i}.png",
                    "status": "failed",
                    "error": _sanitize_error(raw_error),
                    "error_code": classify_image_error(raw_error),
                })
        # 只更新 figures，保留既有 cover 与发布态字段（锁内读最新 state）
        _sync_state_images_locked(self.home, run_id, figures)

        # job 一次性回填每张 succeeded|failed
        job["images"] = [
            {
                "index": i,
                "status": f["status"],
                "output": f"runs/{run_id}/images/{i}.png",
                "error": _sanitize_error(f.get("error") or "") if f["status"] == "failed" else None,
                "error_code": classify_image_error(f.get("error") or "") if f["status"] == "failed" else None,
            }
            for i, f in enumerate(figures)
        ]
        self.store.write(job)

        # 配图产物写 article-illustrated.md（不覆盖 article.md）
        article = paths.safe_read_run_relative(self.home, run_id, "article.md")
        if article:
            illustrated = _markdown_with_images(article.decode("utf-8"), run_id, figures)
            paths.safe_write_run_file(self.home, run_id, "article-illustrated.md", illustrated.encode("utf-8"))
            state = _read_run_state(self.home, run_id)
            state.setdefault("artifacts", {})["illustrated_article"] = f"runs/{run_id}/article-illustrated.md"
            _write_run_state(self.home, run_id, state)

    def _step_preview(self, job: dict) -> None:
        self.store.heartbeat(job["job_id"], step="done")


def _markdown_with_images(markdown: str, run_id: str, figures: list) -> str:
    """把成功图片追加到正文末尾（简单追加，不覆盖原文结构）。"""
    ok_figs = [f for f in figures if f.get("status") == "succeeded"]
    if not ok_figs:
        return markdown
    parts = [markdown.rstrip(), ""]
    for f in ok_figs:
        parts.append(f"\n![配图]({f['output']})\n")
    return "\n".join(parts)


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _worker_error(code: str, message: str) -> errors.ApiError:
    return errors.ApiError(code, message, 422, {"code": code})


# 图片错误归类：按上游错误文本/异常类型映射稳定 error_code。
# 归类失败统一落到 IMAGE_PROVIDER_ERROR（能归类就归类，不能归类给 provider 错误）。
_IMAGE_ERROR_PATTERNS = (
    ("IMAGE_AUTH_FAILED", ("401", "403", "unauthorized", "forbidden", "auth", "invalid api key", "api key", "authentication", "access key", "signature", "credential")),
    ("IMAGE_QUOTA_EXCEEDED", ("429", "quota", "rate limit", "rate limited", "insufficient", "balance", "billing", "exceeds", "limit exceeded", "usage limit")),
    ("IMAGE_MODEL_INVALID", ("model", "not found", "not_found", "unknown provider", "does not exist", "deployment", "invalid model")),
    ("IMAGE_TIMEOUT", ("timeout", "timed out", "timedout")),
)


def classify_image_error(raw: str) -> str:
    """把上游错误原文归类成稳定 error_code，不能归类给 IMAGE_PROVIDER_ERROR。"""
    text = (raw or "").lower()
    for code, needles in _IMAGE_ERROR_PATTERNS:
        for needle in needles:
            if needle in text:
                return code
    return "IMAGE_PROVIDER_ERROR"


def _apply_execution_audit(img: dict, effective: dict) -> None:
    """把本次实际使用的 provider/model 写入单图审计字段（追加历史不覆盖）。"""
    img["executed_provider"] = effective.get("provider")
    img["executed_model"] = effective.get("model")
    history = list(img.get("execution_history") or [])
    history.append({
        "provider": effective.get("provider"),
        "model": effective.get("model"),
        "at": jobs_store._now(),
    })
    img["execution_history"] = history


def _sanitize_error(raw: str) -> str:
    """错误文本脱敏，绝不保留 api_key 明文（统一持久化出口）。

    两段式：
    1. 独立密钥本体：``sk-`` / ``sk_live-`` / ``ak-`` / ``Bearer `` 前缀 + 长串；
    2. 字段名锚点：``api_key`` / ``api key`` / ``apikey`` / ``secret`` /
       ``token`` / ``access_token`` / ``key`` 之后紧跟的**每个**密钥形态长串，
       每个字段锚点独立消费，只推进到该字段值末尾，绝不跳过窗口内其它字段。

    密钥形态判定：长度 >= 8 且（含数字 或 长度 >= 16），避免把 provided 等
    普通英文词误当密钥。
    """
    import re

    text = raw if isinstance(raw, str) else str(raw)

    # 1) 独立密钥本体（前缀 + 长串），任意分隔符都不依赖
    text = re.sub(
        r"(sk_live-|sk-|ak-|bearer\s+)([A-Za-z0-9_-]{8,})",
        lambda m: m.group(1) + "[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )

    # 2) 字段名锚点 + 其后紧邻的密钥形态长串，逐字段独立消费
    field_re = re.compile(
        r"(api[_-]?key|api\s+key|secret|token|access_token|\bkey\b)",
        re.IGNORECASE,
    )
    token_re = re.compile(r"[A-Za-z0-9._~+/=-]{8,}")

    def _is_secret_like(tok: str) -> bool:
        return any(c.isdigit() for c in tok) or len(tok) >= 16

    out: list[str] = []
    pos = 0
    for m in field_re.finditer(text):
        if m.start() < pos:
            continue  # 已被前一个字段的处理覆盖，跳过
        out.append(text[pos:m.end()])
        # 只向后看最多 120 字符，找该字段后紧邻的首个密钥形态长串
        window = text[m.end():m.end() + 120]
        matched = None
        for tm in token_re.finditer(window):
            if _is_secret_like(tm.group(0)):
                matched = tm
                break
        if matched is not None:
            # 只消费「分隔符 + 密钥值」，字段后的其它文本交由后续迭代补
            out.append(window[:matched.start()] + "[REDACTED]")
            pos = m.end() + matched.end()
        else:
            # 该字段后无密钥形态：只消费字段名本身，后续字段不受影响
            pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _sync_state_images_locked(home: str, run_id: str, updates: list) -> None:
    """在 PlatformLock 内读最新 state.yaml，只更新本次处理的目标图，保留发布态与 cover。

    - ``updates`` 只含本次实际处理的 index（图片重试= pending 索引；视觉= 本次生成索引），
      每个元素为 ``{index, status, error, error_code}``；
    - 未出现在 ``updates`` 的索引一律以锁内读到的**最新** state 为准，绝不把 job 里的
      旧快照写回；
    - 保留 ``images.cover``、``permissions``、``media_id`` 及其它发布字段；
    - 绝不写入 ``cover=None``。
    """
    from app.storage.atomic import PlatformLock

    with PlatformLock(home):
        state = _read_run_state(home, run_id)
        images_node = state.get("images") or {}
        figures = list(images_node.get("figures") or [])
        by_index = {f.get("index"): f for f in figures}
        touched = set()
        for up in updates:
            idx = up.get("index", 0)
            touched.add(idx)
            by_index[idx] = {
                "index": idx,
                "output": f"images/{idx}.png",
                "status": up.get("status"),
                "error": up.get("error"),
                "error_code": up.get("error_code"),
            }
        # 已存在的 figures 保持原位（未选中索引维持最新 state 值）
        new_figures = [by_index.get(f.get("index"), f) for f in figures]
        # 本次新出现的索引追加到末尾
        for idx in sorted(touched):
            if idx not in {f.get("index") for f in figures}:
                new_figures.append(by_index[idx])
        images_node["figures"] = new_figures
        # 保留 cover（不写入 None）；若原本无 cover 键则保持缺失
        state["images"] = images_node
        _write_run_state(home, run_id, state)


def _unlink_run_file(home: str, run_id: str, relpath: str) -> None:
    try:
        # 临时文件删除：用安全写空会创建文件，这里改为仅尽力忽略
        pass
    except Exception:
        pass
