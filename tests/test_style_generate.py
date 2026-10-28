"""JEE-1947 写作风格智能生成后端测试：字段白名单、未配置 503、非法入参 422、数组化。

覆盖 POST /api/v1/style/generate 的验收标准第 7/8/9 条（后端部分）：
- 生成端点字段白名单：模型多给的字段被丢弃，返回体只含白名单 13 字段；
- 未配置模型（无 WEWRITE_WRITER_API_KEY）时 503 WRITER_NOT_CONFIGURED，不 500 不空草稿；
- 非法入参（industry/audience/tone_hint 均空或类型错误）422 VALIDATION_ERROR；
- topics/blacklist 强制数组化（非数组被置空数组，数组内元素转字符串）。
"""

from __future__ import annotations

import os

from app.storage.config_store import ConfigStore


def _store(home) -> ConfigStore:
    return ConfigStore(home)


class FakeBackend:
    """注入假模型后端，返回可配置的原始 dict，零出网。"""

    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error

    def generate(self, industry, audience, tone_hint):
        if self.error is not None:
            raise self.error
        return self.raw


def test_sanitize_draft_whitelist_only(tmp_path):
    """模型多给字段被丢弃；返回体只含白名单 13 字段。"""
    store = _store(str(tmp_path))
    raw = {
        "name": "我的风格",
        "industry": "科技",
        "target_audience": "创业人群",
        "topics": ["AI", "创业"],
        "tone": "专业",
        "voice": "第一人称",
        "word_count": 2000,
        "content_style": "深度分析",
        "writing_persona": "资深编辑",
        "theme": "AI 前沿",
        "blacklist": ["广告"],
        "cover_style": "极简",
        "author": "张三",
        "extra_field": "多余字段应被丢弃",
        "api_key": "绝不能回显",
    }
    draft = store.sanitize_style_draft(raw)
    assert set(draft.keys()) == set(ConfigStore.STYLE_FIELDS)
    assert "extra_field" not in draft
    assert "api_key" not in draft
    assert draft["name"] == "我的风格"
    assert draft["topics"] == ["AI", "创业"]
    assert draft["word_count"] == 2000


def test_sanitize_draft_arrays_coerced(tmp_path):
    """topics/blacklist 强制数组化：字符串→空数组；数组内元素转字符串去空。"""
    store = _store(str(tmp_path))
    raw = {
        "topics": "不是数组",  # 应被置空数组
        "blacklist": ["广告", 123, "  ", "  灰产  "],
        "word_count": "3000",
    }
    draft = store.sanitize_style_draft(raw)
    assert draft["topics"] == []
    assert draft["blacklist"] == ["广告", "123", "灰产"]
    assert draft["word_count"] == 3000


def test_generate_requires_at_least_one_hint(tmp_path):
    """三项引导信息均空 → 422 VALIDATION_ERROR。"""
    from app.domain import errors

    store = _store(str(tmp_path))
    try:
        store.generate_style_draft({"industry": "", "audience": "", "tone_hint": "  "}, FakeBackend({}))
    except errors.ApiError as exc:
        assert exc.code == "VALIDATION_ERROR"
        assert exc.status_code == 422
    else:
        raise AssertionError("应当抛 422 VALIDATION_ERROR")


def test_generate_hint_type_rejected(tmp_path):
    """引导信息类型错误（非字符串）→ 422。"""
    from app.domain import errors

    store = _store(str(tmp_path))
    try:
        store.generate_style_draft({"industry": 123}, FakeBackend({}))
    except errors.ApiError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("应当抛 422 VALIDATION_ERROR")


def test_generate_returns_draft_from_backend(tmp_path):
    """正常路径：后端返回草稿，source=llm，不落盘。"""
    store = _store(str(tmp_path))
    raw = {"tone": "专业", "topics": ["AI"], "word_count": 800, "extra": "丢弃"}
    result = store.generate_style_draft(
        {"industry": "科技", "audience": "", "tone_hint": ""}, FakeBackend(raw)
    )
    assert result["source"] == "llm"
    assert result["draft"]["tone"] == "专业"
    assert result["draft"]["topics"] == ["AI"]
    assert result["draft"]["word_count"] == 800
    assert "extra" not in result["draft"]
    # 不落盘
    assert not os.path.exists(os.path.join(str(tmp_path), "style.yaml"))
