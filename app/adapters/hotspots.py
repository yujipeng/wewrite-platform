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
from app.storage import paths

# 中文展示名 -> 英文源 ID 映射表（设计第 2.2 节与 issue 第二节第 4 条）。
SOURCE_LABEL_TO_ID = {
    "微博": "weibo",
    "百度": "baidu",
    "今日头条": "toutiao",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
        超时（TimeoutExpired）与 OSError（CLI 不存在等）统一映射 UPSTREAM_ERROR，
        不得逃成 500。
        """
        cmd = [sys.executable, "-m", "wewrite.commands.fetch_hotspots", "--limit", str(limit)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise errors.upstream_error("热榜抓取超时", {"reason": "timeout"}) from exc
        except OSError as exc:
            raise errors.upstream_error("热榜抓取失败", {"reason": "os_error"}) from exc
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


def hotspot_item_id(item: dict) -> str:
    """计算热点稳定标识（issue 3.1.3）：``{source_id}:{sha256(标题|url|hot)前16位}``。

    同一来源的同一热点（标题优先，标题缺失退化为 url，再退化为 hot）稳定映射到同一 id；
    写入快照时固化为 ``item_id``，读旧快照时按同样规则回退现算，避免老数据不可用。
    """
    source_id = str(item.get("source_id") or item.get("source") or "unknown")
    stable = str(item.get("title") or item.get("url") or item.get("hot") or "")
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
    return f"{source_id}:{digest}"


def ensure_item_ids(items: list) -> list:
    """给每条 item 补齐 ``item_id``（写入路径与旧快照回退计算共用同一规则）。"""
    for item in items:
        if isinstance(item, dict) and not item.get("item_id"):
            item["item_id"] = hotspot_item_id(item)
    return items


def normalize_items(items: list) -> list:
    """给每条 item 补齐英文 source_id、中文 source_label 与稳定 item_id。

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
        out["item_id"] = out.get("item_id") or hotspot_item_id(out)
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
    """热榜快照落盘与读取。

    所有读/写都走 ``app.storage.paths`` 的同一套逐级 openat + O_NOFOLLOW 防线，
    不再出现 ``os.path.join(self.home, ...)`` 后直接 ``open()``/``os.replace``。
    """

    def __init__(self, home: str):
        self.home = home

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
            # 成功：走 Paths 安全写（同目录临时文件 + os.replace，全程 dir_fd）
            paths.safe_write_hotspot_file(
                self.home,
                target_date,
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
        """读单日快照；缺失或超保留期 404，symlink/非目录 403。"""
        validate_date(date)
        # 保留期检查：早于最近 30 天窗口或晚于今天，直接 404
        today = datetime.now(_SHANGHAI).date()
        try:
            d = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise errors.not_found()
        if d < today - timedelta(days=RETENTION_DAYS - 1):
            raise errors.not_found()
        if d > today:
            raise errors.not_found()
        try:
            fd, _st = paths.safe_open_hotspot_file(self.home, date)
        except errors.ApiError as exc:
            raise
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            try:
                snap = json.load(f)
            except json.JSONDecodeError:
                raise errors.not_found()
        # 旧快照无 item_id：读时按同规则回退现算（issue 3.1.3），不改写磁盘。
        if isinstance(snap, dict):
            ensure_item_ids(snap.get("items") or [])
        return snap

    def list_dates(self) -> list[str]:
        """列出快照日期（仅最近 30 天，升序）。"""
        names = paths.list_hotspot_filenames(self.home)
        cutoff = datetime.now(_SHANGHAI).date() - timedelta(days=RETENTION_DAYS - 1)
        dates = []
        for d in names:
            try:
                parsed = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            if parsed >= cutoff:
                dates.append(d)
        return sorted(dates)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
