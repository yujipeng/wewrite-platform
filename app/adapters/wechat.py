"""微信发布适配器：上传图片/封面 + 创建草稿 + 只读对账。

按 docs/s2-technical-design.md 第 1/3 节：

- ``set_publish_permission`` 调上游 ``wewrite.runs.set_publish_permission``；
- 提交按 ``wewrite.toolkit.wechat_api.upload_image``（正文图片）→
  ``upload_thumb``（封面）→ ``publisher.create_draft`` 固定顺序；
- 真实写入只在有凭据时执行；无凭据 raise（fail closed），由测试注入 fake
  替代真实网络写入。
"""

from __future__ import annotations

import os
import tempfile

from app.domain import errors


def _home() -> str:
    return os.environ.get("WEWRITE_HOME", "") or os.path.join(os.path.expanduser("~"), ".wewrite")


def _resolve_credentials() -> tuple[str, str]:
    """取公众号凭据：环境变量优先，否则 config.yaml。缺一即返回空。"""
    appid = os.environ.get("WECHAT_APPID", "")
    secret = os.environ.get("WECHAT_SECRET", "")
    if appid and secret:
        return appid, secret
    from app.storage.config_store import ConfigStore

    try:
        cfg = ConfigStore(_home()).load_config()
    except Exception:
        cfg = {}
    wechat = cfg.get("wechat") or {}
    appid = appid or wechat.get("appid", "")
    secret = secret or wechat.get("secret", "")
    return appid, secret


def _resolve_author() -> str:
    from app.storage.config_store import ConfigStore

    try:
        return ConfigStore(_home()).project_config()["wechat"].get("author") or "编辑"
    except Exception:
        return "编辑"


class WeChatAdapter:
    """微信写入适配器。``commit``/``get_draft`` 可被测试 fake 替换。"""

    def set_publish_permission(self, allow: bool, run_id: str) -> dict:
        from wewrite.runs import set_publish_permission as _set

        try:
            return _set(allow, run_id)
        except Exception as exc:  # noqa: BLE001
            raise errors.upstream_error(
                "上游授权失败", {"reason": "set_publish_permission_failed"}
            ) from exc

    def commit(self, run_id: str, proj: dict, title: str) -> dict:
        """上传正文图片 → 封面 → create_draft，返回 ``{media_id}``。

        无凭据时抛 ``PUBLISH_DISABLED``（fail closed，绝不伪造写入）。
        """
        appid, secret = _resolve_credentials()
        if not appid or not secret:
            raise errors.publish_disabled({"reason": "wechat_not_configured"})

        from wewrite.toolkit.wechat_api import get_access_token, upload_image, upload_thumb
        from wewrite.toolkit.publisher import create_draft

        from app.storage import paths

        home = _home()
        access_token = get_access_token(appid, secret)

        # 1) 正文内本地图片：安全读取字节 → 临时文件 → upload_image → 替换 src
        html = proj["body_html"]
        for ref in proj["image_refs"]:
            data = paths.safe_read_run_relative(home, run_id, ref)
            if data is None:
                continue
            url = _upload_bytes(upload_image, access_token, data, os.path.splitext(ref)[1])
            html = html.replace(ref, url)

        # 2) 封面上传
        cover_data = paths.safe_read_run_relative(home, run_id, proj["cover_rel"])
        if cover_data is None:
            raise errors.preflight_failed({"reason": "cover_missing"})
        thumb_media_id = _upload_bytes(
            upload_thumb, access_token, cover_data, os.path.splitext(proj["cover_rel"])[1]
        )

        # 3) 创建草稿
        draft = create_draft(
            access_token=access_token,
            title=title,
            html=html,
            digest=proj["digest"],
            thumb_media_id=thumb_media_id,
            author=_resolve_author(),
        )
        return {"media_id": draft.media_id}

    def get_draft(self, media_id: str) -> str:
        """只读获取草稿正文 HTML。无凭据抛 PUBLISH_DISABLED。"""
        appid, secret = _resolve_credentials()
        if not appid or not secret:
            raise errors.publish_disabled({"reason": "wechat_not_configured"})
        from wewrite.toolkit.wechat_api import get_access_token
        from wewrite.toolkit.publisher import get_draft as _get_draft

        access_token = get_access_token(appid, secret)
        return _get_draft(access_token, media_id)


def _upload_bytes(upload_fn, access_token: str, data: bytes, suffix: str) -> str:
    """把内存字节写临时文件再交给上游 upload 函数（需要文件路径），上传后删除。"""
    suffix = suffix or ".png"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return upload_fn(access_token, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
