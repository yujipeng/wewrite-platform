"""JEE-1947 写作风格整文件编辑后端测试：schema 兼容、YAML 校验、生成裁剪、brief 注入。

覆盖验收标准第 3/4/5/6/7/8/9/10 条（后端部分）：
- 参考格式三处兼容（blacklist 对象 / word_count 字符串 / reference_accounts）；
- YAML 解析错误带行号 + 中文原因；
- 生成端点字段裁剪 + 未配置模型 503；
- brief 注入 style（改 tone 后 brief 含该 tone）。
"""

from __future__ import annotations

import os

import yaml

from app.domain import errors
from app.storage.config_store import ConfigStore, DEFAULT_STYLE_YAML


def _store(home) -> ConfigStore:
    return ConfigStore(home)


class FakeBackend:
    """注入假模型后端，返回整份 YAML 文本，零出网。"""

    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error

    def generate(self, industry, audience, tone_hint):
        if self.error is not None:
            raise self.error
        return self.text or ""


REFERENCE_YAML = """# WeWrite 风格配置
name: "社会现象心理分析"
industry: "心理学 / 社会观察"

topics:
  - 社会热点背后的心理机制
  - 常见认知偏误与决策陷阱

tone: "像朋友聊天，口语化"
voice: "第一人称"
word_count: "1800-2600"

content_style: "干货"
writing_persona: "midnight-friend"

blacklist:
  words: ["震惊", "必看"]
  topics: ["政治敏感", "宗教"]

reference_accounts: []

theme: "sspai"

cover_style: "简洁、留白多"
author: "社会现象心理分析"
"""


# ---------- schema 兼容（验收第 3/4 条） ----------

def test_reference_yaml_saves_and_roundtrips(tmp_path):
    """发起人给的参考格式原样保存成功，回读保留 blacklist 对象 / word_count 字符串 / reference_accounts。"""
    store = _store(str(tmp_path))
    result = store.put_style({"yaml": REFERENCE_YAML})
    assert result.get("warnings") in (None, [])

    raw = store.load_style_raw()
    data = yaml.safe_load(raw)
    assert isinstance(data["blacklist"], dict)
    assert data["blacklist"]["words"] == ["震惊", "必看"]
    assert data["word_count"] == "1800-2600"
    assert data["reference_accounts"] == []


def test_yaml_put_preserves_raw_bytes(tmp_path):
    """返修第 1 条：含注释的 YAML 提交后 raw 逐字节相等（不重新序列化）。"""
    store = _store(str(tmp_path))
    text = "# 顶部注释\nname: \"带引号\"\n\n# 空行与注释\ntone: 专业\nblacklist:\n  words: [\"震惊\"]\n  topics: [\"宗教\"]\n"
    result = store.put_style({"yaml": text})
    assert result.get("raw") == text, "raw 应逐字节等于提交文本"


def test_blacklist_array_still_supported(tmp_path):
    """旧形态 blacklist 数组仍可保存。"""
    store = _store(str(tmp_path))
    result = store.put_style({"blacklist": ["a", "b"]})
    data = yaml.safe_load(store.load_style_raw())
    assert data["blacklist"] == ["a", "b"]


def test_structured_json_put_still_works(tmp_path):
    """旧的结构化 JSON PUT /style 保持向后兼容。"""
    store = _store(str(tmp_path))
    result = store.put_style({"tone": "专业", "topics": ["AI"], "word_count": 2000})
    assert result["tone"] == "专业"
    assert result["topics"] == ["AI"]
    assert result["word_count"] == 2000


# ---------- YAML 校验（验收第 5/6 条） ----------

def test_validate_yaml_parse_error_with_line(tmp_path):
    """坏缩进 → 422 带行号 + 中文原因。"""
    store = _store(str(tmp_path))
    bad = "name: ok\n  tone: 缩进错误\n    x: ["
    try:
        store.parse_style_yaml(bad)
    except errors.ApiError as exc:
        assert exc.code == "VALIDATION_ERROR"
        assert exc.status_code == 422
        assert "YAML" in exc.message
        assert "line" in exc.details or "行" in exc.message
    else:
        raise AssertionError("应当抛 422 VALIDATION_ERROR")


def test_validate_unknown_field_warning(tmp_path):
    """未知字段不静默丢弃：validate 返回 warnings 列明被忽略字段。"""
    store = _store(str(tmp_path))
    result = store.validate_style_yaml("foo: 1\ntone: 专业\n")
    assert result["valid"] is True
    assert "foo" in result["warnings"]


def test_validate_field_error_reported(tmp_path):
    """字段类型错误：validate 返回 errors 列表（不抛 500），且带 line。"""
    store = _store(str(tmp_path))
    result = store.validate_style_yaml("name: ok\nblacklist: 123\n")
    assert result["valid"] is False
    err = result["errors"][0]
    assert "blacklist" in (err.get("field") or "")
    assert err.get("line") == 2, f"应定位到第 2 行，实际 {err.get('line')}"


def test_validate_nested_unknown_field_warning(tmp_path):
    """返修第 3 条：blacklist.foo 嵌套未知键进 warnings（点分名称）。"""
    store = _store(str(tmp_path))
    result = store.validate_style_yaml("blacklist:\n  words: []\n  foo: 1\n")
    assert result["valid"] is True
    assert "blacklist.foo" in result["warnings"]


def test_yaml_size_limit_422(tmp_path):
    """YAML 文本超过 64KB → 422。"""
    store = _store(str(tmp_path))
    big = "name: " + "x" * (64 * 1024)
    try:
        store.put_style({"yaml": big})
    except errors.ApiError as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("应当抛 422")


# ---------- 生成端点（验收第 7/8/9 条） ----------

def test_generate_requires_at_least_one_hint(tmp_path):
    """三项引导信息均空 → 422 VALIDATION_ERROR。"""
    store = _store(str(tmp_path))
    try:
        store.generate_style_yaml({"industry": "", "audience": "", "tone_hint": "  "}, FakeBackend())
    except errors.ApiError as exc:
        assert exc.code == "VALIDATION_ERROR"
        assert exc.status_code == 422
    else:
        raise AssertionError("应当抛 422 VALIDATION_ERROR")


def test_generate_sanitizes_to_whitelist(tmp_path):
    """生成结果服务端裁剪：最终 YAML 只含白名单字段且可解析。"""
    store = _store(str(tmp_path))
    model_text = (
        "name: 我的风格\ntone: 专业\nfoo: 应该被丢弃\n"
        "blacklist:\n  words: [\"震惊\"]\n  topics: []\n"
        "reference_accounts: []\nword_count: \"1200-1600\"\n"
    )
    result = store.generate_style_yaml(
        {"industry": "科技"}, FakeBackend(model_text)
    )
    assert result["source"] == "llm"
    parsed = yaml.safe_load(result["yaml"])
    assert parsed is not None
    assert "foo" not in parsed
    assert parsed["tone"] == "专业"
    assert "name" in parsed  # 白名单字段全量补默认


def test_generate_blacklist_defaults_to_object(tmp_path):
    """返修第 2 条：模型只返回 name 时，输出 blacklist 为对象形态含 words/topics。"""
    store = _store(str(tmp_path))
    result = store.generate_style_yaml(
        {"industry": "科技"}, FakeBackend("name: 只有名称\n")
    )
    parsed = yaml.safe_load(result["yaml"])
    assert isinstance(parsed["blacklist"], dict), "blacklist 应补为对象形态"
    assert "words" in parsed["blacklist"] and "topics" in parsed["blacklist"]


def test_generate_invalid_yaml_returns_502(tmp_path):
    """返修第 5 条：模型返回非法 YAML → 502 UPSTREAM_ERROR（非 422）。"""
    store = _store(str(tmp_path))
    try:
        store.generate_style_yaml(
            {"industry": "科技"}, FakeBackend("tone: [oops")
        )
    except errors.ApiError as exc:
        assert exc.code == "UPSTREAM_ERROR"
        assert exc.status_code == 502
    else:
        raise AssertionError("应当抛 502 UPSTREAM_ERROR")


# ---------- brief 注入（验收第 10 条） ----------

def test_brief_includes_style_tone(tmp_path):
    """改 tone 为独特文字后，构造出的 brief 包含该 tone。"""
    from app.worker.executor import _build_brief_text

    state = {
        "topic": {"title": "标题", "source": "微博", "framework_hint": "问题-证据-行动", "keywords": ["k"]},
        "visual": {"mode": "none", "max_images": 0},
    }
    style = {"tone": "一段独特的测试语气XYZ", "name": "风格A", "topics": ["AI"], "blacklist": {"words": ["震惊"], "topics": []}}
    brief = _build_brief_text(state, style)
    assert "一段独特的测试语气XYZ" in brief
    assert "风格A" in brief
    assert "禁忌词：震惊" in brief


def test_brief_blacklist_array_form(tmp_path):
    """blacklist 数组形态也能进入 brief。"""
    from app.worker.executor import _build_brief_text

    state = {"topic": {"title": "t", "source": "", "framework_hint": "", "keywords": []}, "visual": {"mode": "none", "max_images": 0}}
    brief = _build_brief_text(state, {"blacklist": ["a", "b"]})
    assert "禁忌词：a, b" in brief
