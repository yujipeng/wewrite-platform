"""P7 发布预检门：唯一发布闸门。

按 docs/s2-technical-design.md 第 3 节：

- ``GET /runs/{run_id}/publish/preflight`` 只读幂等，返回
  ``{ok,revision,checks:[{name,ok,code,message}]}``；
- 摘要取值固定：``state.seo.digest`` 存在时优先（含空串）→ 否则
  ``converter.digest``；title 同理 ``state.seo.title`` → ``converter.title``；
  预检与提交必须用同一投影，revision 必须包含最终取值；
- 判定 ``len(digest.encode('utf-8')) <= 120``；
- 任一检查不过 → ``422 PREFLIGHT_FAILED``（不是 412）。

本模块同时提供 ``content_projection`` 供 token 签发与 publish 提交复核复用，
保证「同一投影、同一 revision、同一绑定内容」。
"""

from __future__ import annotations

import hashlib
import json
import os
import re

import yaml

from app.domain import errors
from app.storage import paths
from app.storage.config_store import ConfigStore

BODY_MIN_CHARS = 200
BODY_MAX_CHARS = 20000
DIGEST_MAX_BYTES = 120
MAX_IMAGES = 10
MAX_TABLE_COLS = 4

_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _read_state(home: str, run_id: str) -> dict:
    state = paths.read_state_yaml(home, run_id)
    if not isinstance(state, dict) or not state.get("run_id"):
        raise errors.not_found({"reason": "run_not_found", "run_id": run_id})
    return state


def _source_markdown_rel(state: dict) -> str | None:
    """发布正文来源：优先 article-illustrated.md，否则 article.md。"""
    artifacts = state.get("artifacts") or {}
    for key, rel in (("illustrated_article", "article-illustrated.md"), ("article", "article.md")):
        if artifacts.get(key) or (artifacts.get(key) is not False):
            # 即便 artifacts 未登记，也优先按文件真实存在判断（由读取层判定）
            pass
    return None


def _preferred_source(state: dict) -> str:
    """正文来源优先级：带图版优先，其次终稿。"""
    artifacts = state.get("artifacts") or {}
    if artifacts.get("illustrated_article"):
        return "illustrated_article"
    if artifacts.get("article"):
        return "article"
    return "illustrated_article"  # 默认尝试带图版，缺失时读取层回落 article


def _cover_relpath(state: dict) -> str:
    images = state.get("images") or {}
    if isinstance(images.get("cover"), str) and images["cover"]:
        return images["cover"]
    visual = state.get("visual") or {}
    if isinstance(visual.get("cover"), str) and visual["cover"]:
        return visual["cover"]
    return "images/cover.png"


def _theme_name(home: str) -> str:
    try:
        return ConfigStore(home).project_config().get("theme") or "professional-clean"
    except Exception:
        return "professional-clean"


def _convert(markdown_text: str, theme_name: str):
    from wewrite.toolkit.converter import WeChatConverter
    from wewrite.toolkit.theme import load_theme

    try:
        theme = load_theme(theme_name)
    except Exception:
        from wewrite.toolkit.theme import load_theme as _lt
        theme = _lt("professional-clean")
    converter = WeChatConverter(theme=theme)
    return converter.convert(markdown_text)


def _extract_local_images(markdown_text: str) -> list[str]:
    refs: list[str] = []
    for m in _IMAGE_REF_RE.finditer(markdown_text):
        src = m.group(1).strip()
        if src and not src.startswith(("http://", "https://", "data:")):
            refs.append(src)
    return refs


def content_projection(home: str, run_id: str) -> dict:
    """计算发布内容投影：标题/摘要最终取值 + 正文/封面/图片 hash。

    同一投影被 preflight / token 签发 / publish 复核共用，保证 revision 与
    绑定内容一致。正文取带图版（若存在）否则终稿；封面取
    ``state.images.cover`` → ``state.visual.cover`` → 默认 ``images/cover.png``。
    """
    state = _read_state(home, run_id)
    theme = _theme_name(home)

    # 正文来源：带图版优先，读取失败回落 article
    markdown_text = None
    source = _preferred_source(state)
    if source == "illustrated_article":
        markdown_text = paths.safe_read_run_relative(home, run_id, "article-illustrated.md")
    if markdown_text is None:
        markdown_text = paths.safe_read_run_relative(home, run_id, "article.md")
    if markdown_text is None:
        markdown_text = ""
    try:
        markdown_text = markdown_text.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        markdown_text = markdown_text.decode("utf-8", errors="replace") if isinstance(markdown_text, bytes) else str(markdown_text)

    result = _convert(markdown_text, theme)
    body_html = result.html

    seo = state.get("seo") or {}
    title = seo.get("title") if isinstance(seo.get("title"), str) else None
    if title is None:
        title = result.title
    title = title or ""

    digest = seo.get("digest") if isinstance(seo.get("digest"), str) else None
    if digest is None:
        digest = result.digest
    digest = digest or ""

    # 封面
    cover_rel = _cover_relpath(state)
    cover_bytes = paths.safe_read_run_relative(home, run_id, cover_rel)

    # 正文内本地图片（≤10 且可读）
    image_refs = _extract_local_images(markdown_text)
    image_hashes: list[str] = []
    image_unreadable: list[str] = []
    for rel in image_refs:
        raw = paths.safe_read_run_relative(home, run_id, rel)
        if raw is None:
            image_unreadable.append(rel)
            image_hashes.append("")
        else:
            image_hashes.append(sha256_hex(raw))

    body_sha = sha256_hex(body_html)
    cover_sha = sha256_hex(cover_bytes) if cover_bytes is not None else ""

    return {
        "run_id": run_id,
        "title": title,
        "digest": digest,
        "body_markdown": markdown_text,
        "body_html": body_html,
        "body_sha256": body_sha,
        "cover_rel": cover_rel,
        "cover_sha256": cover_sha,
        "image_refs": image_refs,
        "image_sha256s": image_hashes,
        "image_unreadable": image_unreadable,
        "table_cols": _max_table_cols(markdown_text),
    }


def _revision_of(proj: dict) -> str:
    canon = {
        "title": proj["title"],
        "digest": proj["digest"],
        "body_sha256": proj["body_sha256"],
        "cover_sha256": proj["cover_sha256"],
        "image_refs": proj["image_refs"],
        "image_sha256s": proj["image_sha256s"],
        "table_cols": proj["table_cols"],
    }
    payload = json.dumps(canon, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _max_table_cols(markdown_text: str) -> int:
    """解析 Markdown 表格，返回最大列数（无表格返回 0）。"""
    lines = markdown_text.split("\n")
    max_cols = 0
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            in_table = True
            cols = _count_table_cols(stripped)
            max_cols = max(max_cols, cols)
        elif in_table and not stripped.startswith("|"):
            in_table = False
    return max_cols


def _count_table_cols(line: str) -> int:
    body = line.strip().strip("|")
    return len(body.split("|")) if body else 0


def check(home: str, run_id: str) -> dict:
    """执行预检，返回 ``{ok,revision,checks}``（不抛异常，除非 run 不存在）。"""
    state = _read_state(home, run_id)
    proj = content_projection(home, run_id)
    checks: list[dict] = []

    # 标题非空
    checks.append({
        "name": "title",
        "ok": bool(proj["title"]),
        "code": "TITLE_EMPTY" if not proj["title"] else None,
        "message": "" if proj["title"] else "标题为空",
    })

    # 摘要 UTF-8 字节数 ≤120
    digest_bytes = len(proj["digest"].encode("utf-8"))
    digest_ok = digest_bytes <= DIGEST_MAX_BYTES
    checks.append({
        "name": "digest",
        "ok": digest_ok,
        "code": "DIGEST_TOO_LONG" if not digest_ok else None,
        "message": "" if digest_ok else f"摘要 {digest_bytes} 字节超过 120 字节上限",
    })

    # 正文 200–20000 字符
    body_len = len(proj["body_markdown"])
    body_ok = BODY_MIN_CHARS <= body_len <= BODY_MAX_CHARS
    checks.append({
        "name": "body",
        "ok": body_ok,
        "code": "BODY_LENGTH" if not body_ok else None,
        "message": "" if body_ok else f"正文字符数 {body_len} 不在 200–20000 之间",
    })

    # 图片 ≤10 且文件可读
    images_ok = len(proj["image_refs"]) <= MAX_IMAGES and not proj["image_unreadable"]
    if len(proj["image_refs"]) > MAX_IMAGES:
        images_msg = f"图片 {len(proj['image_refs'])} 张超过上限 10"
        images_code = "IMAGES_TOO_MANY"
    elif proj["image_unreadable"]:
        images_msg = "存在不可读的图片文件"
        images_code = "IMAGE_UNREADABLE"
    else:
        images_msg, images_code = "", None
    checks.append({"name": "images", "ok": images_ok, "code": images_code, "message": images_msg})

    # 表格 ≤4 列
    tables_ok = proj["table_cols"] <= MAX_TABLE_COLS
    checks.append({
        "name": "tables",
        "ok": tables_ok,
        "code": "TABLE_TOO_WIDE" if not tables_ok else None,
        "message": "" if tables_ok else f"表格最大 {proj['table_cols']} 列超过 4 列上限",
    })

    # 封面存在
    cover_ok = proj["cover_sha256"] != ""
    checks.append({
        "name": "cover",
        "ok": cover_ok,
        "code": "COVER_MISSING" if not cover_ok else None,
        "message": "" if cover_ok else "封面不存在",
    })

    # permissions.publish=true
    perms = state.get("permissions") or {}
    perm_ok = bool(perms.get("publish", False))
    checks.append({
        "name": "permissions",
        "ok": perm_ok,
        "code": "PUBLISH_NOT_ALLOWED" if not perm_ok else None,
        "message": "" if perm_ok else "未开启发布授权",
    })

    # editorial.publishable=true
    editorial = state.get("editorial") or {}
    ed_ok = editorial.get("publishable") is True
    checks.append({
        "name": "editorial",
        "ok": ed_ok,
        "code": "NOT_PUBLISHABLE" if not ed_ok else None,
        "message": "" if ed_ok else "审稿未通过",
    })

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "revision": _revision_of(proj), "checks": checks}


def require_preflight_ok(home: str, run_id: str) -> dict:
    """预检不通过即抛 ``422 PREFLIGHT_FAILED``（token 签发/提交用）。"""
    result = check(home, run_id)
    if not result["ok"]:
        raise errors.preflight_failed({"revision": result["revision"], "checks": result["checks"]})
    return result
