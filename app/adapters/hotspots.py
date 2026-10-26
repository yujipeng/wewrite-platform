"""热榜抓取适配器与快照存储。

按 docs/s2-technical-design.md 第 2.2 / 3 节：

- 上游 CLI ``python -m wewrite.commands.fetch_hotspots --limit N`` 输出 JSON；
- 上游 ``sources``/``sources_failed`` 是英文源 ID，``items[].source`` 是中文展示名。
  落盘与接口一律用英文 ID 作为机器键，中文名单独作为 ``source_label``；
- ``date`` 仅用于命名快照文件，抓取始终是当下数据；
- 全源失败即使进程退出码为 0，也必须检查 ``error``/``count``/``items`` 判失败，
  且不覆盖上一份成功快照。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from app.domain import errors
from app.storage.atomic import atomic_write_text, PlatformLock

# 中文展示名 -> 英文源 ID 映射表（设计第 2.2 节与 issue 第二节第 4 条）。
SOURCE_LABEL_TO_ID = {
    "微博": "weibo",
    "百度": "baidu",
    "今日头条": "toutiao",
}

# 英文源 ID -> 中文展示名（反向映射，用于把 items[].source 归一化）。
SOURCE_ID_TO_LABEL = {v: k for k, v in SOURCE_LABEL_TO_ID.items()}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_UTC = timezone.utc
_SHANGHAI = timezone(timedelta(hours=8))

RETENTION_DAYS = 30


def validate_date(value: str) -> str:
    """校验 YYYY-MM-DD 格式（并验证是真实日期）。"""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise errors.validation_error("date 必须为 YYYY-MM-DD 格式", {"field": "date"})
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise errors.validation_error("date 不是合法日期", {"field": "date"})
    return value


def normalize_limit(value) -> int:
    """limit 取 1–100，非法抛 VALIDATION_ERROR。"""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise errors.validation_error("limit 必须为 1–100 的整数", {"field": "limit"})
    if limit < 1 or limit > 100:
        raise errors.validation_error("limit 必须为 1–100 的整数", {"field": "limit"})
    return limit


def _local_date(dt: datetime) -> str:
    return dt.astimezone(_SHANGHAI).strftime("%Y-%m-%d")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class HotspotFetcher:
    """抓取热榜。默认走上游 CLI 子进程；测试注入 fake 以计数零出网。"""

    def __init__(self, timeout: int = 45):
        self.timeout = timeout

    def fetch(self, limit: int) -> dict:
        """调用上游 CLI 并解析 stdout JSON。

        上游全源失败仍以退出码 0 输出含 error/count 的 JSON；本函数只保证
        「抓取动作本身」的成功与否，是否全失败由调用方据 error/count/items 判定。
        """
        cmd = [sys.executable, "-m", "wewrite.commands.fetch_hotspots", "--limit", str(limit)]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise errors.upstream_error(
                "热榜抓取失败", {"reason": "cli_nonzero_exit", "exit_code": proc.returncode}
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise errors.upstream_error("热榜抓取返回无法解析", {"reason": "invalid_json"}) from exc
        if not isinstance(data, dict):
            raise errors.upstream_error("热榜抓取返回结构异常", {"reason": "invalid_shape"})
        return data


def normalize_items(items: list) -> list:
    """给每条 item 补齐英文 source_id 并保留中文 source_label。

    上游 items[].source 是中文展示名（微博/百度/今日头条）；用映射表补英文 ID。
    未知中文名无法映射时按未知源处理，不伪造 ID。
    """
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get("source", "")
        source_id = item.get("source_id") or SOURCE_LABEL_TO_ID.get(label, "")
        out = dict(item)
        out["source_label"] = label
        out["source_id"] = source_id
        result.append(out)
    return result


def _build_snapshot(data: dict, date: str) -> dict:
    """把上游输出投影为设计 2.2 的快照结构。"""
    now = datetime.now(timezone.utc)
    items = normalize_items(data.get("items") or [])
    sources = [s for s in (data.get("sources") or []) if isinstance(s, str)]
    sources_failed = [s for s in (data.get("sources_failed") or []) if isinstance(s, str)]
    return {
        "version": 1,
        "date": date,
        "captured_at_utc": _iso(now),
        "captured_at_local": _iso(now.astimezone(_SHANGHAI)),
        "sources": sources,
        "sources_failed": sources_failed,
        "last_success_at": _iso(now),
        "count": len(items),
        "error": data.get("error"),
        "items": items,
        "stale_sources": data.get("stale_sources") or [],
    }


def is_all_failed(snapshot: dict) -> bool:
    """全源失败判定：error 存在或 count==0 或 items 为空。"""
    return bool(snapshot.get("error")) or snapshot.get("count", 0) == 0 or not snapshot.get("items")


class HotspotStore:
    """热榜快照落盘与读取。所有路径引用基准为 WEWRITE_HOME。"""

    def __init__(self, home: str):
        self.home = home

    def _snap_dir(self) -> str:
        return os.path.join(self.home, "hotspots")

    def _snap_path(self, date: str) -> str:
        return os.path.join(self._snap_dir(), f"{date}.json")

    def _now_local_date(self) -> str:
        return _local_date(datetime.now(timezone.utc))

    def fetch_and_persist(self, fetcher: HotspotFetcher, limit: int, date: str | None = None) -> dict:
        """抓取当下数据并落盘（POST /hotspots/refresh）。

        全失败不覆盖上一份成功快照，但本调用返回当下失败结果供页面展示错误。
        """
        target_date = validate_date(date) if date else self._now_local_date()
        raw = fetcher.fetch(limit)
        snapshot = _build_snapshot(raw, target_date)

        if not is_all_failed(snapshot):
            # 成功：原子写快照（不覆盖性由 atomic_write_text 的 os.replace 保证，
            # 这里额外用锁保证并发下先到先得且不丢上一份成功数据）
            os.makedirs(self._snap_dir(), exist_ok=True)
            with PlatformLock(self.home):
                atomic_write_text(
                    self._snap_path(target_date),
                    json.dumps(snapshot, ensure_ascii=False, indent=2),
                )

        # 响应统一返回设计第 3 节的字段集
        return {
            "timestamp": raw.get("timestamp"),
            "sources": snapshot["sources"],
            "sources_failed": snapshot["sources_failed"],
            "count": snapshot["count"],
            "items": snapshot["items"],
            "error": snapshot["error"],
        }

    def fetch_now(self, fetcher: HotspotFetcher, limit: int) -> dict:
        """refresh=1：只抓取并返回当下数据，不写快照、不更新 last_success_at。"""
        raw = fetcher.fetch(limit)
        snapshot = _build_snapshot(raw, self._now_local_date())
        return {
            "timestamp": raw.get("timestamp"),
            "sources": snapshot["sources"],
            "sources_failed": snapshot["sources_failed"],
            "count": snapshot["count"],
            "items": snapshot["items"],
            "error": snapshot["error"],
        }

    def read_latest(self) -> dict:
        """返回当前已保存的最新快照（不带 refresh 的 GET）。"""
        dates = self.list_dates()
        if not dates:
            return {
                "date": None,
                "items": [],
                "sources": [],
                "sources_failed": [],
                "last_success_at": None,
                "error": None,
                "count": 0,
            }
        latest = dates[-1]  # 升序，取最后一天
        snap = self.read_date(latest)
        return {
            "date": snap["date"],
            "items": snap["items"],
            "sources": snap["sources"],
            "sources_failed": snap["sources_failed"],
            "last_success_at": snap.get("last_success_at"),
            "error": snap.get("error"),
            "count": snap["count"],
        }

    def read_date(self, date: str) -> dict:
        """读单日快照；缺失或超保留期 404。"""
        validate_date(date)
        # 保留期检查：早于最近 30 天窗口直接 404
        today = datetime.now(_SHANGHAI).date()
        try:
            d = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise errors.not_found()
        if d < today - timedelta(days=RETENTION_DAYS - 1):
            raise errors.not_found()
        path = self._snap_path(date)
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            raise errors.not_found()

    def list_dates(self) -> list[str]:
        """列出快照日期（仅最近 30 天，升序）。"""
        snap_dir = self._snap_dir()
        if not os.path.isdir(snap_dir):
            return []
        cutoff = datetime.now(_SHANGHAI).date() - timedelta(days=RETENTION_DAYS - 1)
        dates = []
        for name in os.listdir(snap_dir):
            if not name.endswith(".json"):
                continue
            d = name[:-5]
            if not _DATE_RE.fullmatch(d):
                continue
            try:
                parsed = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            if parsed >= cutoff:
                dates.append(d)
        return sorted(dates)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
