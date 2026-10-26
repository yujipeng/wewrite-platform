"""P7 发布闸门：授权、一次性 token、提交与对账。

按 docs/s2-technical-design.md 第 2.4 / 3 节：

- ``set_permission``：仅用户主动操作，调上游 ``set_publish_permission``，写
  ``audit/*.jsonl``（actor=local-user、UTC 时间、run_id、前后值、reason），
  撤销时立即作废该 run 未消费 token；
- token：磁盘只存 hash，明文只在签发响应出现一次；TTL 10 分钟；绑定
  run + 标题 + 正文 hash + 预检 revision；撤销后失效；in_flight/unknown/
  succeeded 的 fence 禁止换 token 重发；
- ``commit_once``：锁内 CAS 消费 token 并再次核对权限/revision/绑定内容；
  按 upload_image → upload_thumb → create_draft 顺序；成功只写一次 media_id；
  异常写 unknown，禁止重复提交；
- ``reconcile``：仅在有 media_id 时调用只读 get_draft，双方经
  ``html_to_plaintext`` 归一化后比较文本 hash，只标注「仅文本核对」。

真实微信写入只在有凭据时执行；无凭据走可替换 stub（测试注入 fake）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from app.domain import errors
from app.domain import preflight as preflight_mod
from app.storage import atomic
from app.storage import paths

TOKEN_TTL_MINUTES = 10

_PUBLISHABLE_STATUSES = {"issued", "revoked", "consumed", "in_flight"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def new_token_id() -> str:
    return "pt-" + secrets.token_hex(8)


def _write_token(home: str, token: dict) -> None:
    paths.safe_write_subdir_file(
        home, "publish", f"{token['token_id']}.json",
        json.dumps(token, ensure_ascii=False, indent=2),
    )


def _read_token(home: str, token_id: str) -> dict | None:
    text = paths.safe_read_subdir_text(home, "publish", f"{token_id}.json")
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _list_tokens(home: str) -> list[dict]:
    result = []
    for fname in paths.safe_list_subdir_filenames(home, "publish"):
        if not fname.endswith(".json"):
            continue
        token = _read_token(home, fname[:-5])
        if token is not None:
            result.append(token)
    return result


def _fence_filename(run_id: str) -> str:
    return f"attempt-{run_id}.json"


def _read_fence(home: str, run_id: str) -> dict | None:
    text = paths.safe_read_subdir_text(home, "publish", _fence_filename(run_id))
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _write_fence(home: str, run_id: str, fence: dict) -> None:
    paths.safe_write_subdir_file(
        home, "publish", _fence_filename(run_id),
        json.dumps(fence, ensure_ascii=False, indent=2),
    )


def _audit(home: str, event: str, **fields) -> None:
    date = _now_utc().strftime("%Y-%m-%d")
    record = {"ts": _iso(_now_utc()), "actor": "local-user", "event": event}
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    existing = paths.safe_read_subdir_text(home, "audit", f"{date}.jsonl") or ""
    paths.safe_write_subdir_file(home, "audit", f"{date}.jsonl", existing + line)


class PublishGate:
    """发布闸门。``adapter`` 提供上游能力，测试可注入 fake。"""

    def __init__(self, home: str, adapter=None):
        self.home = home
        self.adapter = adapter

    # ---------- 授权 ----------

    def set_permission(self, run_id: str, allow: bool, reason: str | None) -> dict:
        paths.validate_run_id(run_id)
        with atomic.PlatformLock(self.home):
            state = paths.read_state_yaml(self.home, run_id)
            if not isinstance(state, dict) or not state.get("run_id"):
                raise errors.not_found({"reason": "run_not_found", "run_id": run_id})
            before = bool((state.get("permissions") or {}).get("publish", False))
            new_state = self.adapter.set_publish_permission(allow, run_id)
            after = bool((new_state.get("permissions") or {}).get("publish", False))
            _audit(
                self.home,
                "publish_permission",
                run_id=run_id,
                before=before,
                after=after,
                reason=reason or "",
            )
            if not allow:
                self._revoke_tokens(run_id)
            proj = preflight_mod.content_projection(self.home, run_id)
            return {
                "permissions": {"publish": after},
                "revision": preflight_mod._revision_of(proj),
            }

    def _revoke_tokens(self, run_id: str) -> None:
        for token in _list_tokens(self.home):
            if token.get("run_id") != run_id:
                continue
            if token.get("status") == "issued":
                token["status"] = "revoked"
                token["updated_at"] = _iso(_now_utc())
                _write_token(self.home, token)

    # ---------- token ----------

    def issue_token(self, run_id: str, revision: str, title: str) -> dict:
        paths.validate_run_id(run_id)
        with atomic.PlatformLock(self.home):
            state = paths.read_state_yaml(self.home, run_id)
            if not isinstance(state, dict) or not state.get("run_id"):
                raise errors.not_found({"reason": "run_not_found", "run_id": run_id})
            proj = preflight_mod.content_projection(self.home, run_id)
            current_revision = preflight_mod._revision_of(proj)
            if revision != current_revision:
                raise errors.conflict({"reason": "revision_mismatch"})
            if proj["title"] != title:
                raise errors.conflict({"reason": "title_mismatch"})
            perm = (state.get("permissions") or {}).get("publish", False)
            if not perm:
                raise errors.publish_disabled({"reason": "publish_not_allowed"})
            # 其它预检项失败 → 422 PREFLIGHT_FAILED
            preflight_mod.require_preflight_ok(self.home, run_id)

            # fence 检查：in_flight/unknown/succeeded 禁止换 token
            fence = _read_fence(self.home, run_id)
            if fence and fence.get("status") in {"in_flight", "unknown", "succeeded"}:
                raise errors.publish_unknown({"reason": "fence_blocked", "status": fence.get("status")})

            token_id = new_token_id()
            plain = secrets.token_urlsafe(32)
            now = _now_utc()
            token = {
                "version": 1,
                "token_id": token_id,
                "token_hash": sha256_hex(plain),
                "run_id": run_id,
                "title": title,
                "title_hash": sha256_hex(title),
                "body_hash": proj["body_sha256"],
                "preflight_revision": revision,
                "expires_at": _iso(now + timedelta(minutes=TOKEN_TTL_MINUTES)),
                "status": "issued",
                "attempt_fence": (fence.get("attempt") or 0) + 1 if fence else 1,
                "created_at": _iso(now),
                "consumed_at": None,
            }
            _write_token(self.home, token)
            _audit(self.home, "publish_token_issued", run_id=run_id, token_id=token_id)
            return {"token": plain, "token_id": token_id, "expires_at": token["expires_at"]}

    # ---------- 提交 ----------

    def commit_once(self, run_id: str, token: str, title: str) -> dict:
        paths.validate_run_id(run_id)
        if not isinstance(token, str) or not token:
            raise errors.token_invalid({"reason": "token_missing"})
        with atomic.PlatformLock(self.home):
            state = paths.read_state_yaml(self.home, run_id)
            if not isinstance(state, dict) or not state.get("run_id"):
                raise errors.not_found({"reason": "run_not_found", "run_id": run_id})

            token_hash = sha256_hex(token)
            matched = None
            for t in _list_tokens(self.home):
                if t.get("run_id") == run_id and t.get("token_hash") == token_hash:
                    matched = t
                    break
            if matched is None:
                raise errors.token_invalid({"reason": "token_not_found"})

            # 有效期
            try:
                expires = datetime.fromisoformat(matched.get("expires_at") or "")
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if _now_utc() >= expires:
                    raise errors.token_invalid({"reason": "token_expired"})
            except ValueError:
                raise errors.token_invalid({"reason": "token_expired"})

            if matched.get("status") != "issued":
                raise errors.token_invalid({"reason": "token_not_issued", "status": matched.get("status")})

            # fence：unknown/succeeded 禁止重复提交
            fence = _read_fence(self.home, run_id)
            if fence and fence.get("status") in {"unknown", "succeeded"}:
                raise errors.publish_unknown({"reason": "fence_blocked", "status": fence.get("status")})

            # 再次核对权限、revision、绑定内容
            proj = preflight_mod.content_projection(self.home, run_id)
            current_revision = preflight_mod._revision_of(proj)
            perm = (state.get("permissions") or {}).get("publish", False)
            if not perm:
                raise errors.publish_disabled({"reason": "publish_not_allowed"})
            if matched.get("preflight_revision") != current_revision:
                raise errors.conflict({"reason": "revision_changed"})
            if title != matched.get("title"):
                raise errors.conflict({"reason": "title_mismatch"})
            if matched.get("body_hash") != proj["body_sha256"]:
                raise errors.conflict({"reason": "body_changed"})

            # CAS 消费为 in_flight
            matched["status"] = "in_flight"
            matched["consumed_at"] = _iso(_now_utc())
            _write_token(self.home, matched)

            fence = {
                "version": 1,
                "run_id": run_id,
                "attempt": matched.get("attempt_fence"),
                "token_id": matched.get("token_id"),
                "revision": current_revision,
                "body_hash": proj["body_sha256"],
                "cover_hash": proj["cover_sha256"],
                "title": title,
                "image_sha256s": proj["image_sha256s"],
                "status": "in_flight",
                "updated_at": _iso(_now_utc()),
            }
            _write_fence(self.home, run_id, fence)

        # 锁外执行真实写（外部慢调用），结果写回时重新加锁
        try:
            result = self._commit_to_wechat(run_id, proj, title)
        except Exception:
            with atomic.PlatformLock(self.home):
                self._mark_unknown(run_id)
            raise

        with atomic.PlatformLock(self.home):
            state2 = paths.read_state_yaml(self.home, run_id)
            publish = dict(state2.get("publish") or {})
            publish["status"] = "succeeded"
            publish["media_id"] = result["media_id"]
            publish["published_at"] = _iso(_now_utc())
            import yaml

            state2["publish"] = publish
            paths.safe_write_run_state(
                self.home, run_id, yaml.safe_dump(state2, allow_unicode=True, sort_keys=False)
            )
            fence = _read_fence(self.home, run_id)
            if fence:
                fence["status"] = "succeeded"
                fence["media_id"] = result["media_id"]
                fence["updated_at"] = _iso(_now_utc())
                _write_fence(self.home, run_id, fence)
            for t in _list_tokens(self.home):
                if t.get("run_id") == run_id and t.get("status") == "in_flight":
                    t["status"] = "consumed"
                    t["consumed_at"] = _iso(_now_utc())
                    _write_token(self.home, t)
            _audit(self.home, "publish_succeeded", run_id=run_id, media_id=result["media_id"])
            return {"status": "succeeded", "media_id": result["media_id"]}

    def _commit_to_wechat(self, run_id: str, proj: dict, title: str) -> dict:
        return self.adapter.commit(run_id, proj, title)

    def _mark_unknown(self, run_id: str) -> None:
        state = paths.read_state_yaml(self.home, run_id)
        if isinstance(state, dict) and state.get("run_id"):
            publish = dict(state.get("publish") or {})
            publish["status"] = "unknown"
            publish["unknown_at"] = _iso(_now_utc())
            import yaml

            state["publish"] = publish
            paths.safe_write_run_state(
                self.home, run_id, yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
            )
        fence = _read_fence(self.home, run_id)
        if fence:
            fence["status"] = "unknown"
            fence["updated_at"] = _iso(_now_utc())
            _write_fence(self.home, run_id, fence)
        _audit(self.home, "publish_unknown", run_id=run_id)

    # ---------- 对账 ----------

    def reconcile(self, run_id: str, media_id: str | None) -> dict:
        paths.validate_run_id(run_id)
        state = paths.read_state_yaml(self.home, run_id)
        if not isinstance(state, dict) or not state.get("run_id"):
            raise errors.not_found({"reason": "run_not_found", "run_id": run_id})
        publish = state.get("publish") or {}
        stored_media_id = media_id or publish.get("media_id")
        if not stored_media_id:
            return {
                "status": "manual_review",
                "media_id": None,
                "consistent": None,
                "note": "无 media_id，请人工到公众号后台草稿箱核对。",
            }
        proj = preflight_mod.content_projection(self.home, run_id)
        remote_html = self.adapter.get_draft(stored_media_id)
        from wewrite.toolkit.publisher import html_to_plaintext

        local_text = html_to_plaintext(proj["body_html"])
        remote_text = html_to_plaintext(remote_html)
        local_hash = sha256_hex(local_text)
        remote_hash = sha256_hex(remote_text)
        consistent = local_hash == remote_hash
        note = "仅文本核对，不代表图片与排版一致。"
        # 保存核对记录（提交 HTML、双方归一化文本 hash 与核对时间）
        record = {
            "run_id": run_id,
            "media_id": stored_media_id,
            "local_text_hash": local_hash,
            "remote_text_hash": remote_hash,
            "consistent": consistent,
            "checked_at": _iso(_now_utc()),
        }
        paths.safe_write_subdir_file(
            self.home, "publish", f"reconcile-{run_id}.json",
            json.dumps(record, ensure_ascii=False, indent=2),
        )
        return {
            "status": "consistent" if consistent else "mismatch",
            "media_id": stored_media_id,
            "consistent": consistent,
            "local_text_hash": local_hash,
            "remote_text_hash": remote_hash,
            "note": note,
        }
