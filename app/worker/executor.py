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


class WriterBackend:
    """llm-write 子进程后端（可注入 fake）。返回 ``(exit_code, output_path_or_none)``。"""

    def run(self, brief_path: str, output_path: str) -> int:
        cmd = [sys.executable, "-m", "wewrite.commands.llm_write", "--brief", brief_path, "--output", output_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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


def _build_brief_text(state: dict) -> str:
    topic = state.get("topic") or {}
    visual = state.get("visual") or {}
    title = topic.get("title") or "未命名文章"
    framework = topic.get("framework_hint") or "问题-证据-行动"
    return (
        f"# 选题\n\n标题：{title}\n\n切入点：{topic.get('source') or ''}\n\n"
        f"框架：{framework}\n\n关键词：{', '.join(topic.get('keywords') or [])}\n\n"
        f"视觉模式：{visual.get('mode') or 'none'}，最大图片数：{visual.get('max_images') or 0}\n"
    )


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
            parts.append(f"目标字数：{word_count}\n")
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
        """取一个 queued job 执行；无可用 job 返回 None。"""
        for job in self.store.list_jobs():
            if job.get("status") != "queued":
                continue
            running = self.store.cas_queued_to_running(job["job_id"])
            if running is None:
                continue
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
        staging_bytes = brief_text.encode("utf-8")
        paths.safe_write_run_file(self.home, run_id, staging_rel, staging_bytes)

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

        # 正文校验：非空且 >=200 字符
        if not staging_text or len(staging_text.strip()) < 200:
            raise _worker_error("WRITER_FAILED", "写作输出正文校验未通过")

        # 原子推广为 article.md
        paths.safe_write_run_file(self.home, run_id, "article.md", staging_text.encode("utf-8"))
        # 清理 staging/brief 临时文件（尽力）
        _unlink_run_file(self.home, run_id, staging_rel)
        _unlink_run_file(self.home, run_id, brief_rel)

        # 更新 state.artifacts.article
        state = _read_run_state(self.home, run_id)
        state.setdefault("artifacts", {})["article"] = f"runs/{run_id}/article.md"
        _write_run_state(self.home, run_id, state)

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

        brief_fd, brief_path = _tmp.mkstemp(suffix=".md")
        out_fd, out_path = _tmp.mkstemp(suffix=".md")
        os.close(brief_fd)
        os.close(out_fd)
        try:
            with open(brief_path, "wb") as f:
                f.write(brief_bytes)
            exit_code = self.writer.run(brief_path, out_path)
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
            # 无需配图
            state["images"] = {"cover": None, "figures": []}
            _write_run_state(self.home, run_id, state)
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
                figures.append({"index": i, "output": f"images/{i}.png", "status": "succeeded", "error": None})
            else:
                figures.append({"index": i, "output": f"images/{i}.png", "status": "failed", "error": r.get("error")})
        state = _read_run_state(self.home, run_id)
        state["images"] = {"cover": None, "figures": figures}
        _write_run_state(self.home, run_id, state)

        # job 一次性回填每张 succeeded|failed
        job["images"] = [
            {"index": i, "status": f["status"], "output": f"runs/{run_id}/images/{i}.png", "error": f["error"]}
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


def _unlink_run_file(home: str, run_id: str, relpath: str) -> None:
    try:
        # 临时文件删除：用安全写空会创建文件，这里改为仅尽力忽略
        pass
    except Exception:
        pass
