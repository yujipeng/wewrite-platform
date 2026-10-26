"""候选池存储：``candidates/<YYYY-MM-DD>-<hash>.json`` 的生成/回读/删除。

按 docs/s2-technical-design.md 第 2.1 / 3 节：

- 路径 ``candidates/<date>-<hash>.json``（不是 ``topics/candidates/<set_id>.json``）；
- schema：``{version,candidate_set_id,set_status,created_at,expires_at,timezone,
  hotspot_snapshot,snapshot_sha256,style_revision,items[]}``；item 含
  ``id,status,title,angle,scores{hot,hot_normalized,relevance,value,total},
  framework,source,source_id,source_url,history_flag,content_hash``；
- item ``status`` ∈ ``proposed|confirmed|retracted|expired``；集合级 ``set_status`` ∈
  ``open|confirmed|superseded|deleted|expired``；
- 生成不使用 ``SET_OPEN_EXISTS``：相同 ``snapshot_sha256`` + 相同 ``style_revision``
  幂等返回既有 open 集；要另开新集显式 ``force=true``，写审计并把旧集置 ``superseded``；
- 24 小时过期后只读展示；删除仅对「从未确认、无 intent/job/run 关联」的 open 集可置
  deleted 墓碑，否则 409 CANDIDATE_NOT_ACTIONABLE reason=set_closed。

候选生成是**规则生成桩**（不调真实 LLM）：评分来源必须标注，不得伪装 AI 评估。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from app.domain import errors
from app.domain import scoring
from app.storage import paths

CANDIDATE_COUNT = 10
EXPIRY_HOURS = 24
TIMEZONE = "Asia/Shanghai"

ITEM_STATUSES = {"proposed", "confirmed", "retracted", "expired"}
SET_STATUSES = {"open", "confirmed", "superseded", "deleted", "expired"}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def set_filename(date: str, snapshot_sha256: str) -> str:
    """候选集文件名：``<date>-<hash>.json``，hash 取 snapshot_sha256 前 12 位。"""
    digest = snapshot_sha256.replace("sha256:", "")
    return f"{date}-{digest[:12]}.json"


def candidate_set_id_for(date: str, snapshot_sha256: str) -> str:
    """候选集 ID：``<date>-<snapshot_sha256 前 12 位>``（与落盘文件名一致）。

    设计第 2 节路径 ``candidates/<date>-<hash>.json`` 中 ``<hash>`` 由快照内容
    稳定哈希派生，保证同一快照幂等地落到同一文件；不是随机 token。
    """
    return set_filename(date, snapshot_sha256)[:-5]  # 去掉 .json 后缀


def _set_date(candidate_set_id: str) -> str:
    """候选集 ID 的前三段（YYYY-MM-DD）。"""
    parts = candidate_set_id.split("-")
    if len(parts) >= 3:
        return "-".join(parts[:3])
    return ""


def validate_date(date: str) -> str:
    if not isinstance(date, str) or not _DATE_RE.fullmatch(date):
        raise errors.validation_error("date 必须为 YYYY-MM-DD 格式", {"field": "date"})
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise errors.validation_error("date 不是合法日期", {"field": "date"})
    return date


def validate_candidate_set_id(value: str) -> str:
    """候选集 ID 形如 ``<date>-<12位hex>``（服务端生成格式，不接受路径）。"""
    if not isinstance(value, str) or not re.fullmatch(r"^\d{4}-\d{2}-\d{2}-[a-f0-9]{12}$", value):
        raise errors.validation_error("candidate_set_id 不合法", {"field": "candidate_set_id"})
    return value


def snapshot_sha256_of(snapshot: dict) -> str:
    """快照稳定哈希：对快照 items（排序后）的关键字段做 sha256。"""
    items = snapshot.get("items") or []
    canon = []
    for it in items:
        if not isinstance(it, dict):
            continue
        canon.append(
            [
                str(it.get("title") or ""),
                str(it.get("source_id") or it.get("source") or ""),
                str(it.get("url") or ""),
                str(it.get("hot") or ""),
            ]
        )
    canon.sort(key=lambda row: "|".join(row))
    payload = json.dumps(canon, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_expired(candidate_set: dict, now: datetime | None = None) -> bool:
    """按 expires_at 判断是否过期（24h）。"""
    try:
        expires = datetime.fromisoformat(candidate_set.get("expires_at") or "")
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return (now or _now_utc()) >= expires


def _set_status(candidate_set: dict, now: datetime) -> str:
    """集合展示状态：deleted/superseded/confirmed 优先，open 且过期则 expired。"""
    status = candidate_set.get("set_status")
    if status in {"deleted", "superseded", "confirmed"}:
        return status
    if status == "open" and is_expired(candidate_set, now):
        return "expired"
    return status or "open"


class CandidateStore:
    """候选集落盘与读取，全部走 paths 安全层（openat + O_NOFOLLOW）。"""

    def __init__(self, home: str):
        self.home = home

    # ---------- 读 ----------

    def _filename_of(self, candidate_set_id: str) -> str:
        validate_candidate_set_id(candidate_set_id)
        return f"{candidate_set_id}.json"

    def load(self, candidate_set_id: str) -> dict | None:
        text = paths.safe_read_subdir_text(self.home, "candidates", self._filename_of(candidate_set_id))
        if text is None:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def get_or_404(self, candidate_set_id: str) -> dict:
        data = self.load(candidate_set_id)
        if data is None:
            raise errors.not_found({"reason": "candidate_set_not_found"})
        return data

    def list_open_ids(self) -> list[str]:
        """列出磁盘上全部候选集文件名（不含扩展名），调用方据内容过滤。"""
        return paths.safe_list_subdir_filenames(self.home, "candidates")

    def list_sets(self, date: str | None = None, status: str | None = None) -> list[dict]:
        """按 date/status 过滤候选集摘要。"""
        now = _now_utc()
        result = []
        for fname in paths.safe_list_subdir_filenames(self.home, "candidates"):
            if not fname.endswith(".json"):
                continue
            cid = fname[:-5]
            data = self.load(cid)
            if data is None:
                continue
            effective = _set_status(data, now)
            if date is not None and _set_date(cid) != date:
                continue
            if status is not None and effective != status:
                continue
            result.append(self._summary(data, effective))
        result.sort(key=lambda s: s.get("created_at") or "", reverse=True)
        return result

    def _summary(self, data: dict, effective_status: str) -> dict:
        return {
            "candidate_set_id": data.get("candidate_set_id"),
            "date": _set_date(data.get("candidate_set_id") or ""),
            "set_status": effective_status,
            "created_at": data.get("created_at"),
            "expires_at": data.get("expires_at"),
            "snapshot_sha256": data.get("snapshot_sha256"),
            "style_revision": data.get("style_revision"),
            "items": data.get("items") or [],
        }

    # ---------- 写（生成） ----------

    def generate(self, date: str, style_revision: str, snapshot: dict, force: bool = False) -> tuple[dict, bool]:
        """从指定日期快照生成候选集（10 条，全失败不写）。

        幂等：相同 snapshot_sha256 + style_revision 且存在 open 集 → 返回既有集
        （``created=False``）。同日期已有其它 open 集（不同快照或不同风格）时，
        另开新集必须显式 ``force=true``（否则 VALIDATION_ERROR 提示）。

        覆盖保护（硬要求，优先于 force）：目标文件名 ``<date>-<snapshot_sha256 前
        12 位>`` 已存在时，若该旧集**有下游引用**（item 已 confirmed/retracted、
        或 item 带 run_id/job_id、或存在 ``intents/<cid>-*`` 关联、或 ``set_status``
        非 open），一律拒绝 ``409 CANDIDATE_NOT_ACTIONABLE reason=set_closed``，不得
        改写该文件任何字段。仅对「仍 open、从未确认、无 intent/job/run」的旧集，
        ``force=true`` 才允许原地重建，重建前写审计（事件名 ``candidate_set_rebuilt``，
        区别于 ``candidate_set_superseded``）。

        全失败（无法产出 10 条候选）→ 抛 VALIDATION_ERROR（reason=all_failed），
        **不写任何候选集文件**。
        """
        validate_date(date)
        scoring.validate_style_revision(style_revision)

        snap_hash = snapshot_sha256_of(snapshot)

        # 幂等：找到相同 snapshot_sha256 + style_revision 的既有 open 集
        existing = self._find_open_set(snap_hash, style_revision)
        if existing is not None and not force:
            return existing, False

        # 生成候选：全失败（无任何条目）或条目不足（<10 条）都不写，语义分开
        items = self._generate_items(snapshot)
        if not items:
            raise errors.validation_error(
                "当日快照全源失败或无可用条目，无法生成候选", {"reason": "all_failed"}
            )
        if len(items) < CANDIDATE_COUNT:
            raise errors.validation_error(
                "当日快照可用条目不足 10 条，无法生成候选",
                {"reason": "insufficient_items", "available": len(items)},
            )

        now = _now_utc()
        cid = candidate_set_id_for(date, snap_hash)
        candidate_set = {
            "version": 1,
            "candidate_set_id": cid,
            "set_status": "open",
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(hours=EXPIRY_HOURS)),
            "timezone": TIMEZONE,
            "hotspot_snapshot": f"hotspots/{date}.json",
            "snapshot_sha256": snap_hash,
            "style_revision": style_revision,
            "items": items,
        }

        with _platform_lock(self.home):
            # 重入锁后再查一次（并发下仍幂等）
            again = self._find_open_set(snap_hash, style_revision)
            if again is not None and not force:
                return again, False

            target = self.load(cid)
            if target is not None:
                # 目标文件已存在（同日期 + 同快照哈希 → 同名文件）
                if not self._is_rebuildable(target):
                    # 有下游引用：绝不覆盖，硬要求优先于 force
                    raise errors.candidate_not_actionable("set_closed")
                if not force:
                    raise errors.validation_error(
                        "该日期已有候选集，另开新集需 force=true",
                        {"reason": "set_exists_requires_force", "existing": target.get("candidate_set_id")},
                    )
                # force + 仍 open/从未确认/无关联 → 原地重建，写可追溯审计
                self._audit(
                    "candidate_set_rebuilt",
                    cid=target.get("candidate_set_id"),
                    created_at=target.get("created_at"),
                    style_revision=target.get("style_revision"),
                    snapshot_sha256=target.get("snapshot_sha256"),
                )
            else:
                # 目标文件不存在（不同快照哈希 → 不同文件名）
                if not force:
                    for other in self._open_sets(date):
                        raise errors.validation_error(
                            "该日期已有候选集，另开新集需 force=true",
                            {"reason": "set_exists_requires_force", "existing": other.get("candidate_set_id")},
                        )
                else:
                    self._supersede_open_sets_locked(cid)

            paths.safe_write_subdir_file(
                self.home,
                "candidates",
                set_filename(date, snap_hash),
                json.dumps(candidate_set, ensure_ascii=False, indent=2),
            )
            self._audit("candidate_set_created", cid=cid, snapshot_sha256=snap_hash, force=force)
        return candidate_set, True

    def _is_rebuildable(self, data: dict) -> bool:
        """原地重建是否安全：仍 open、从未确认、无 intent/job/run 关联。

        对应设计第 2.1 节「不删候选或 run」与第 3 节删除保护的同一套下游引用判定。
        """
        if data.get("set_status") != "open":
            return False
        for it in data.get("items") or []:
            if it.get("status") in {"confirmed", "retracted"}:
                return False
            if it.get("run_id") or it.get("job_id"):
                return False
        prefix = f"{data.get('candidate_set_id')}-"
        for fname in paths.safe_list_subdir_filenames(self.home, "intents"):
            if fname.startswith(prefix):
                return False
        return True

    def _open_sets(self, date: str) -> list[dict]:
        """列出该日期所有仍为 open 的候选集（含过期但 set_status 仍 open 的）。"""
        result = []
        for fname in paths.safe_list_subdir_filenames(self.home, "candidates"):
            if not fname.endswith(".json"):
                continue
            cid = fname[:-5]
            if _set_date(cid) != date:
                continue
            data = self.load(cid)
            if data is not None and data.get("set_status") == "open":
                result.append(data)
        return result

    def _generate_items(self, snapshot: dict) -> list[dict]:
        """规则生成桩：从快照 items 生成候选（非 AI）。"""
        raw_items = [it for it in (snapshot.get("items") or []) if isinstance(it, dict)]
        if not raw_items:
            return []
        normalized = scoring.normalize_hot(raw_items, limit=CANDIDATE_COUNT)
        candidates = []
        for idx, raw in enumerate(normalized):
            if len(candidates) >= CANDIDATE_COUNT:
                break
            c = scoring.score_candidate(raw, idx)
            if c is not None:
                candidates.append(c)
        # 总分降序（设计第 3 节：P5 页面按总分降序展示）
        candidates.sort(key=lambda c: c["scores"]["total"], reverse=True)
        # 重新编号：保持服务端生成格式 c01..c10
        for i, c in enumerate(candidates):
            c["id"] = f"c{i + 1:02d}"
        return candidates

    def _find_open_set(self, snap_hash: str, style_revision: str) -> dict | None:
        for fname in paths.safe_list_subdir_filenames(self.home, "candidates"):
            if not fname.endswith(".json"):
                continue
            data = self.load(fname[:-5])
            if data is None:
                continue
            if (
                data.get("set_status") == "open"
                and data.get("snapshot_sha256") == snap_hash
                and data.get("style_revision") == style_revision
            ):
                return data
        return None

    def _supersede_open_sets_locked(self, except_cid: str) -> None:
        """force（目标文件名不存在、另开新集）时，把其它「仍 open、从未确认、无
        下游引用」的旧集置 superseded；有下游引用的旧集（含已确认/已撤回）不动。"""
        for fname in paths.safe_list_subdir_filenames(self.home, "candidates"):
            if not fname.endswith(".json"):
                continue
            cid = fname[:-5]
            if cid == except_cid:
                continue
            data = self.load(cid)
            if data is None or data.get("set_status") != "open":
                continue
            if not self._is_rebuildable(data):
                continue  # 有下游引用，不得置 superseded（保留其 run/intent 关联）
            data["set_status"] = "superseded"
            paths.safe_write_subdir_file(
                self.home, "candidates", f"{cid}.json", json.dumps(data, ensure_ascii=False, indent=2)
            )
            self._audit("candidate_set_superseded", cid=cid)

    def _audit(self, event: str, **fields) -> None:
        """追加 audit/<date>.jsonl（UTC 时间）。"""
        date = _now_utc().strftime("%Y-%m-%d")
        record = {
            "ts": _iso(_now_utc()),
            "actor": "local-user",
            "event": event,
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        # 追加写：读旧 + 写回（同一平台锁下，无并发追加问题）
        existing = paths.safe_read_subdir_text(self.home, "audit", f"{date}.jsonl") or ""
        paths.safe_write_subdir_file(self.home, "audit", f"{date}.jsonl", existing + line)

    # ---------- 删除保护 ----------

    def delete(self, candidate_set_id: str) -> None:
        """删除候选集：仅对「从未确认、无 intent/job/run 关联」的 open 集可置 deleted
        墓碑，否则 409 CANDIDATE_NOT_ACTIONABLE reason=set_closed。"""
        with _platform_lock(self.home):
            data = self.get_or_404(candidate_set_id)
            if data.get("set_status") != "open":
                raise errors.candidate_not_actionable("set_closed")
            # 任何候选曾确认过 → 拒绝
            for it in data.get("items") or []:
                if it.get("status") in {"confirmed", "retracted"}:
                    raise errors.candidate_not_actionable("set_closed")
            # 无 intent 关联（文件级墓碑检查）
            prefix = f"{candidate_set_id}-"
            for fname in paths.safe_list_subdir_filenames(self.home, "intents"):
                if fname.startswith(prefix):
                    raise errors.candidate_not_actionable("set_closed")
            data["set_status"] = "deleted"
            self._write_set(data)
            self._audit("candidate_set_deleted", cid=candidate_set_id)

    def _write_set(self, data: dict) -> None:
        paths.safe_write_subdir_file(
            self.home,
            "candidates",
            f"{data['candidate_set_id']}.json",
            json.dumps(data, ensure_ascii=False, indent=2),
        )


def _platform_lock(home: str):
    """导入惰性：避免 storage.atomic 与 storage.paths 循环依赖。"""
    from app.storage.atomic import PlatformLock

    return PlatformLock(home)
