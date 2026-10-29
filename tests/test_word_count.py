"""JEE-1963 目标字数解析与续写相关测试。

覆盖验收标准第 1/2/3/8 条（解析纯整数 / 解析区间 / 解析非法 / prompt 加强），
以及 run 投影 length_warning 的透出。续写两条调用与失败不毁稿见 test_worker.py。
"""

from __future__ import annotations

from app.domain import word_count
from app.worker.executor import _build_brief_text


def test_parse_plain_int(tmp_path):
    """验收第 1 条：word_count=2000 → {min_chars:1600, max_chars:2400}。"""
    assert word_count.parse_word_count(2000) == {"min_chars": 1600, "max_chars": 2400}


def test_parse_range_string(tmp_path):
    """验收第 2 条：word_count="1800-2600" → {min_chars:1440, max_chars:3120}。"""
    assert word_count.parse_word_count("1800-2600") == {"min_chars": 1440, "max_chars": 3120}


def test_parse_invalid_returns_none(tmp_path):
    """验收第 3 条：word_count="随便" → None，不报错。"""
    assert word_count.parse_word_count("随便") is None


def test_parse_int_string(tmp_path):
    """字符串整数同样按单值解析。"""
    assert word_count.parse_word_count("2000") == {"min_chars": 1600, "max_chars": 2400}


def test_parse_circa_prefix(tmp_path):
    """「约2000」剥掉修饰前缀后按单值解析。"""
    assert word_count.parse_word_count("约2000") == {"min_chars": 1600, "max_chars": 2400}


def test_parse_suffix_unit(tmp_path):
    """「2000字」剥掉单位后缀后按单值解析。"""
    assert word_count.parse_word_count("2000字") == {"min_chars": 1600, "max_chars": 2400}


def test_parse_zero_and_empty_are_none(tmp_path):
    """0 与空串表示「无目标」，解析为 None，保持现状只校验 >=200。"""
    assert word_count.parse_word_count(0) is None
    assert word_count.parse_word_count("") is None
    assert word_count.parse_word_count(None) is None


def test_parse_range_swapped_order(tmp_path):
    """区间写反（2600-1800）时交换后仍可解析。"""
    assert word_count.parse_word_count("2600-1800") == {"min_chars": 1440, "max_chars": 3120}


def test_count_chars_removes_all_whitespace(tmp_path):
    """需求 §3.1：去掉空白后的字符数（空格/换行/制表符都去掉）。"""
    assert word_count.count_chars("字 " * 1000) == 1000
    assert word_count.count_chars("字\n\t 字") == 2
    assert word_count.count_chars("   ") == 0
    assert word_count.count_chars("") == 0


def test_parse_huge_values_do_not_overflow(tmp_path):
    """JEE-1966 兜底：超大数/超长字符串不抛异常，返回 None。"""
    assert word_count.parse_word_count("9" * 400) is None
    assert word_count.parse_word_count(10 ** 400) is None
    assert word_count.parse_word_count("99999999999") is None  # 超过 10 位上限


def test_format_word_count_range(tmp_path):
    """验收第 8 条：区间字符串 brief 含「务必达到」「低于 1800」。"""
    state = {
        "topic": {"title": "标题", "source": "", "framework_hint": "", "keywords": []},
        "visual": {"mode": "none", "max_images": 0},
    }
    brief = _build_brief_text(state, {"word_count": "1800-2600"})
    assert "务必达到" in brief
    assert "低于 1800" in brief


def test_format_word_count_int(tmp_path):
    """整数目标同样给出硬要求文案。"""
    state = {
        "topic": {"title": "标题", "source": "", "framework_hint": "", "keywords": []},
        "visual": {"mode": "none", "max_images": 0},
    }
    brief = _build_brief_text(state, {"word_count": 2000})
    assert "务必达到" in brief
    assert "低于 2000" in brief


def test_run_detail_projects_length_warning(tmp_path):
    """run 投影透出 length_warning，前端 P2/P3 据此渲染提示条。"""
    import os

    import yaml

    from app.storage import projections

    home = str(tmp_path)
    run_id = "20261026-120000-abcdef"
    run_dir = os.path.join(home, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    state = {
        "version": 4,
        "run_id": run_id,
        "status": "completed",
        "mode": "complete",
        "permissions": {"publish": False},
        "visual": {"mode": "none", "max_images": 0, "max_cost": None},
        "topic": {"title": "测试", "keywords": [], "source": "", "framework_hint": ""},
        "artifacts": {},
        "steps": {},
        "last_error": None,
        "length_warning": {"actual": 300, "target_min": 1600, "target_max": 2400},
    }
    with open(os.path.join(run_dir, "state.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)

    detail = projections.get_run_detail(home, run_id)
    assert detail is not None
    assert detail["length_warning"] == {"actual": 300, "target_min": 1600, "target_max": 2400}
