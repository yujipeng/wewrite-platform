"""run 只读投影。

平台自己的只读投影，绝不调用上游 ``wewrite.runs.load_run``（它会升级并回写旧
状态，GET 有副作用）。产物徽标按「文件真实存在」实时判定。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field

from app.storage import paths

# 产物徽标：name -> 展示名
ARTIFACT_LABELS = {
    "brief": "任务书",
    "article": "终稿",
    "draft": "初稿",
    "illustrated_article": "带图版",
    "review_report": "审稿报告",
    "images_manifest": "图片清单",
    "sources": "来源",
    "preview": "预览",
}


@dataclass
class RunSummary:
    run_id: str
    title: str
    status: str
    mode: str
    artifacts: dict = field(default_factory=dict)
    publish: dict = field(default_factory=dict)
    created: str | None = None
    updated: str | None = None


def _safe_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _exists_in_run(home: str, run_id: str, name: str) -> bool:
    """判定产物文件是否真实存在（白名单 + 安全打开成功即存在）。"""
    try:
        fd, _st = paths.safe_open_run_file(home, run_id, name)
    except Exception:
        return False
    os.close(fd)
    return True


def list_runs(home: str) -> list[RunSummary]:
    """枚举 runs 目录，返回按创建时间倒序的 run 列表。"""
    p = paths.Paths(home)
    runs_fd = p.runs_fd()
    if runs_fd == -1:
        return []

    entries = []
    try:
        with os.scandir(runs_fd) as it:
            for entry in it:
                if not entry.name:
                    continue
                try:
                    paths.validate_run_id(entry.name)
                except Exception:
                    continue  # 跳过非法目录名，不把噪声暴露为任务
                entries.append(entry.name)
    finally:
        os.close(runs_fd)

    results: list[RunSummary] = []
    for run_id in entries:
        state = paths.read_state_yaml(home, run_id)
        if not isinstance(state, dict) or not state.get("run_id"):
            continue
        results.append(_summarize(home, run_id, state))

    results.sort(
        key=lambda r: (r.created or "", r.run_id),
        reverse=True,
    )
    return results


def _summarize(home: str, run_id: str, state: dict) -> RunSummary:
    topic = state.get("topic") or {}
    seo = state.get("seo") or {}
    artifacts = {}
    for name in paths.ARTIFACT_NAMES:
        artifacts[name] = _exists_in_run(home, run_id, name)

    publish = state.get("publish") or {}
    mode = state.get("mode") or "draft"
    # mode=publish 只在旧数据里可能出现；平台只接受 draft|complete，展示原值即可
    summary = RunSummary(
        run_id=state.get("run_id") or run_id,
        title=(seo.get("title") or topic.get("title") or "未命名文章"),
        status=state.get("status") or "unknown",
        mode=mode,
        artifacts=artifacts,
        publish={
            "status": publish.get("status") or "not_published",
            "media_id": publish.get("media_id"),
            "unknown_at": publish.get("unknown_at"),
        },
        created=state.get("created"),
        updated=state.get("updated"),
    )
    return summary


def get_run_detail(home: str, run_id: str) -> dict | None:
    """run 详情投影：topic、steps、editorial、visual、permissions、publish。"""
    state = paths.read_state_yaml(home, run_id)
    if not isinstance(state, dict) or not state.get("run_id"):
        return None

    summary = _summarize(home, run_id, state)
    seo = state.get("seo") or {}
    editorial = state.get("editorial") or {}
    publish = state.get("publish") or {}
    permissions = state.get("permissions") or {}
    visual = state.get("visual") or {}
    images = state.get("images") or {}

    return {
        "run_id": summary.run_id,
        "title": summary.title,
        "status": summary.status,
        "mode": summary.mode,
        "created": summary.created,
        "updated": summary.updated,
        "topic": state.get("topic") or {},
        "steps": state.get("steps") or {},
        "editorial": {
            "decision": editorial.get("decision"),
            "publishable": editorial.get("publishable"),
        },
        "seo": {
            "title": seo.get("title"),
            "digest": seo.get("digest"),
            "tags": seo.get("tags", []),
            "quality_score": seo.get("quality_score"),
        },
        "visual": {
            "mode": visual.get("mode"),
            "max_images": visual.get("max_images"),
            "max_cost": visual.get("max_cost"),
        },
        "images": _project_images(images),
        "permissions": {
            "publish": bool(permissions.get("publish", False)),
        },
        "publish": summary.publish,
        "artifacts": summary.artifacts,
        "last_error": state.get("last_error"),
        "length_warning": state.get("length_warning"),
    }


def _project_images(images) -> dict:
    """图片清单投影：兼容 ``{cover, figures}`` 与 ``{images:[...]}`` 两种形态。"""
    if isinstance(images, dict):
        if "cover" in images or "figures" in images:
            return {
                "cover": images.get("cover"),
                "figures": images.get("figures") or [],
            }
        if isinstance(images.get("images"), list):
            return {"cover": None, "figures": images.get("images")}
    return {"cover": None, "figures": []}
