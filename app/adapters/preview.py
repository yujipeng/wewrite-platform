"""预览适配器：调用 wewrite 的 WeChatConverter 输出 HTML，并做脚本过滤。

按 docs/s2-technical-design.md 第 3 节 ``POST /preview`` 契约：

- 主题名称来自 ``wewrite.toolkit.theme.list_themes()``，不接受外部传入路径；
- 正文来源固定白名单 ``draft|article|illustrated_article``，复用 M1 的
  ``safe_open_run_file`` 做路径白名单，不另写路径拼接；
- 使用同一个 ``WeChatConverter`` 输出 HTML；
- 上游转换器可能产出 ``<script>``、事件属性或 ``javascript:`` URL，需过滤并记录计数；
- ``paste_safe_html`` 用 ``make_paste_safe`` 走复制粘贴路径。
"""

from __future__ import annotations

import os
import re

from bs4 import BeautifulSoup

from app.domain import errors
from app.storage import paths

# 预览允许的正文来源白名单（设计第 3 节：draft|article|illustrated_article）
PREVIEW_SOURCES = {
    "draft": "draft",
    "article": "article",
    "illustrated_article": "illustrated_article",
}

# 事件属性前缀（onclick / onerror / onmouseover / ... 全部剥离）
_EVENT_ATTR_RE = re.compile(r"^on[a-z]+$", re.IGNORECASE)

# javascript: URL 前缀（大小写不敏感）
_JS_URL_RE = re.compile(r"^\s*javascript:", re.IGNORECASE)


def filter_unsafe(html: str) -> tuple[str, int]:
    """过滤预览 HTML 中的可执行内容，返回 (安全 HTML, 被剥离计数)。

    剥离三类：
    1. ``<script>`` / ``<style>`` 标签及其内容；
    2. 所有标签上的事件属性（on*）；
    3. 所有 ``javascript:`` URL（href/src 等）。
    """
    soup = BeautifulSoup(html, "html.parser")
    filtered = 0

    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
        filtered += 1

    for el in soup.find_all(True):
        for attr in list(el.attrs):
            if _EVENT_ATTR_RE.match(attr):
                del el[attr]
                filtered += 1
            else:
                val = el.attrs.get(attr)
                # href/src 等多值属性可能是列表
                vals = val if isinstance(val, list) else [val]
                for v in vals:
                    if isinstance(v, str) and _JS_URL_RE.match(v):
                        filtered += 1
                # 直接剔除 javascript: URL（把属性值清空，避免残留可执行引用）
                if isinstance(val, str) and _JS_URL_RE.match(val):
                    el.attrs[attr] = ""
                elif isinstance(val, list):
                    el.attrs[attr] = ["" if (isinstance(x, str) and _JS_URL_RE.match(x)) else x for x in val]

    return str(soup), filtered


def preview(
    home: str,
    run_id: str,
    source: str,
    theme_name: str,
    fetcher_themes=None,
) -> dict:
    """生成预览响应 ``{html,paste_safe_html,title,digest,source_sha256,filtered_count}``。

    ``source`` 只允许 ``draft|article|illustrated_article``，走 M1 的
    ``safe_open_run_file`` 读取白名单产物，不另写路径拼接。
    """
    from wewrite.toolkit.converter import WeChatConverter, make_paste_safe
    from wewrite.toolkit.theme import list_themes, load_theme

    if not isinstance(source, str) or source not in PREVIEW_SOURCES:
        raise errors.validation_error("source 只允许 draft|article|illustrated_article", {"field": "source"})

    # 主题名称必须来自 list_themes()，不接受外部传入路径
    if theme_name is None or not isinstance(theme_name, str):
        raise errors.validation_error("theme 必填", {"field": "theme"})
    if any(sep in theme_name for sep in ("/", "\\", "..")):
        raise errors.validation_error("theme 不合法", {"field": "theme"})
    available = fetcher_themes() if fetcher_themes is not None else list_themes()
    if theme_name not in available:
        raise errors.not_found({"reason": "theme_not_found", "theme": theme_name})

    try:
        theme = load_theme(theme_name)
    except (FileNotFoundError, ValueError) as exc:
        raise errors.not_found({"reason": "theme_not_found", "theme": theme_name}) from exc

    # 复用 M1 安全打开函数读正文产物（白名单 + 逐级 openat + symlink 防御）
    fd, _st = paths.safe_open_run_file(home, run_id, PREVIEW_SOURCES[source])
    with os.fdopen(fd, "rb") as f:
        raw_bytes = f.read()
    try:
        markdown_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise errors.validation_error("正文不是合法 UTF-8 文本", {"field": "source"})

    source_sha256 = paths.sha256_hex(raw_bytes)

    converter = WeChatConverter(theme=theme)
    result = converter.convert(markdown_text)

    safe_html, filtered_count = filter_unsafe(result.html)
    paste_safe_html = make_paste_safe(safe_html)

    return {
        "html": safe_html,
        "paste_safe_html": paste_safe_html,
        "title": result.title,
        "digest": result.digest,
        "source_sha256": source_sha256,
        "filtered_count": filtered_count,
    }
