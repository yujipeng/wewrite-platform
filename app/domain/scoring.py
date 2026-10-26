"""P5 候选评分与 content_hash。

按 docs/s2-technical-design.md 第 2.1 节：

- ``hot_normalized`` 复用上游 ``wewrite.commands.fetch_hotspots`` 的三源算法：
  每 source 按原始 ``hot`` 降序、0-based rank，第 r 名为
  ``round(100*(n-r)/n, 1)``，各源归一化完成后才全局降序取 limit；
- 总分固定 ``round(0.3*hot_normalized + 0.4*relevance + 0.3*value, 2)``；
- ``relevance``/``value`` 为 0–100 分，缺失即判该候选失败，不得以 0 分凑数。

本轮候选生成是**规则生成桩**（不调真实 LLM）：``relevance``/``value`` 由规则打分器
给出，页面必须标注「规则生成，非 AI 评估」。

``content_hash`` 由候选正文关键字段稳定计算（sha256），确认时重算比对：
``title + "\x1f" + angle + "\x1f" + framework + "\x1f" + source_id + "\x1f" + source_url``。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from app.domain import errors

# 候选条目稳定哈希的字段分隔符（字段内不会出现该控制符）
_HASH_SEP = "\x1f"


def sha256_hex(text: str) -> str:
    """UTF-8 编码后的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(item: dict) -> str:
    """由候选正文关键字段稳定计算哈希（设计第 3 节硬性要求，算法说明见模块 docstring）。"""
    parts = [
        str(item.get("title") or ""),
        str(item.get("angle") or ""),
        str(item.get("framework") or ""),
        str(item.get("source_id") or item.get("source") or ""),
        str(item.get("source_url") or ""),
    ]
    return "sha256:" + sha256_hex(_HASH_SEP.join(parts))


def normalize_hot(items: list[dict], limit: int = 10) -> list[dict]:
    """复用上游三源归一化算法，返回按 hot_normalized 全局降序取 limit 的条目。

    每条 item 需含 ``source``（中文展示名或英文 ID）与 ``hot``。归一化按
    source 分组：每组按原始 ``hot`` 降序，第 r 名（0-based）为
    ``round(100*(n-r)/n, 1)``；各源完成后全局降序取前 ``limit`` 条。
    """
    by_source: dict[str, list[dict]] = {}
    for it in items:
        key = str(it.get("source") or "")
        by_source.setdefault(key, []).append(it)

    for source_items in by_source.values():
        source_items.sort(key=lambda x: _num(x.get("hot")), reverse=True)
        n = len(source_items)
        for rank, it in enumerate(source_items):
            it["hot_normalized"] = round(100 * (n - rank) / n, 1) if n > 0 else 0

    ordered = sorted(items, key=lambda x: _num(x.get("hot_normalized")), reverse=True)
    return ordered[:limit]


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ---- 规则生成桩：relevance / value 打分（非 AI）----

_FRAMEWORK_RANK = ["问题-证据-行动", "热点-解读-建议", "案例-方法-清单", "对比-结论-行动"]

_ANGLE_KEYWORDS = ["为什么", "怎么办", "背后", "影响", "真相", "解读", "趋势", "避坑", "数据"]


def _clamp100(v: float) -> float:
    return max(0.0, min(100.0, v))


def _relevance(title: str, angle: str, index: int) -> float:
    """规则相关性分：标题长度、切入角度关键词、列表名次三者折中。

    这是**规则生成桩**：不是真实 LLM 写作评估器的打分，只保证 0–100 且可复现。
    """
    score = 60.0
    t = title or ""
    if 10 <= len(t) <= 28:
        score += 10
    elif len(t) < 6:
        score -= 20
    if angle and any(k in (angle or "") for k in _ANGLE_KEYWORDS):
        score += 10
    # 热榜名次越靠前，可写性略高（3 档衰减）
    score -= (index % 3) * 3
    return _clamp100(round(score, 1))


def _value(title: str, framework: str) -> float:
    """规则价值分：框架类型 + 标题信息量。

    这是**规则生成桩**：不是真实 LLM 写作评估器的打分。
    """
    score = 55.0
    if framework in _FRAMEWORK_RANK:
        # 排位越靠前的框架越接近「干货型」写作
        score += (len(_FRAMEWORK_RANK) - _FRAMEWORK_RANK.index(framework)) * 4
    if title and any(k in title for k in ("数据", "清单", "对比", "指南", "避坑")):
        score += 5
    return _clamp100(round(score, 1))


def score_candidate(raw: dict, index: int) -> dict | None:
    """由热榜条目生成一个候选（含 hot_normalized / relevance / value / total）。

    ``raw`` 缺 title/source/hot 判为失败；``hot_normalized`` 已由
    ``normalize_hot`` 预先填入。``total`` 固定按设计公式，不另写归一化。
    返回完整候选 dict；失败返回 None。
    """
    title = str(raw.get("title") or "").strip()
    source = str(raw.get("source") or "").strip()
    if not title or not source:
        return None

    hot = _num(raw.get("hot"))
    hot_normalized = _num(raw.get("hot_normalized"))
    if hot <= 0 and hot_normalized <= 0:
        # 无热度的条目不构成可写候选
        return None

    framework = _FRAMEWORK_RANK[index % len(_FRAMEWORK_RANK)]
    angle = raw.get("description") or ""
    if not angle:
        angle = _angle_for(title, index)

    relevance = _relevance(title, angle, index)
    value = _value(title, framework)
    total = round(0.3 * hot_normalized + 0.4 * relevance + 0.3 * value, 2)

    candidate = {
        "id": _candidate_id(index),
        "status": "proposed",
        "title": title,
        "angle": angle,
        "scores": {
            "hot": hot,
            "hot_normalized": hot_normalized,
            "relevance": relevance,
            "value": value,
            "total": total,
        },
        "framework": framework,
        "source": source,
        "source_id": raw.get("source_id") or "",
        "source_url": raw.get("url") or "",
        "history_flag": False,
        "content_hash": "",
    }
    candidate["content_hash"] = content_hash(candidate)
    return candidate


def _angle_for(title: str, index: int) -> str:
    words = _ANGLE_KEYWORDS
    return f"{words[index % len(words)]}：{title}" if title else ""


def _candidate_id(index: int) -> str:
    """候选 ID：服务端生成格式 ``c01..c10``（按 1-based 序号）。"""
    return f"c{index + 1:02d}"


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_style_revision(value: str) -> str:
    """style_revision 校验：接受 ``sha256:...`` 或任意非空字符串（配置投影引用）。"""
    if not isinstance(value, str) or not value.strip():
        raise errors.validation_error("style_revision 必填", {"field": "style_revision"})
    return value.strip()
