"""目标字数解析：把 ``style.word_count`` 解析成可判定的字数区间。

JEE-1963：``word_count`` 此前只是拼进 prompt 的软提示，平台无法判断实际字数是否
达标。本模块给出统一口径：把常见写法（整数 / 字符串整数 / 区间字符串 / 带「约」等
修饰前缀 / 带「字」等后缀）解析为 ``{min_chars, max_chars}``，供 executor 在生成后
校验并自动续写。

口径：
- 纯整数 ``N`` → ``{min_chars: int(N*0.8), max_chars: int(N*1.2)}``（允许 ±20% 浮动）；
- 区间 ``"A-B"`` → ``{min_chars: int(A*0.8), max_chars: int(B*1.2)}``（区间写反时交换）；
- 无法解析（或 0 / 空）→ ``None``（不校验，保持现状只查 >=200）；
- 字数按「去掉空白后的字符数」计，不数英文单词（与现有 ``len(text.strip())`` 一致）。
"""

from __future__ import annotations

import re

# 单个数字：剥掉「约/大约/左右/字」等前后缀后匹配。
_NUMBER_RE = re.compile(r"^[\d]+$")
# 区间：形如 A-B（允许两端带空白），再单独剥「字」后缀。
_RANGE_RE = re.compile(r"^\s*([\d]+)\s*[-—~]\s*([\d]+)\s*$")


def _strip_decorations(text: str) -> str:
    """去掉「约 / 大约 / 左右 / 字 / 字符」等常见修饰前后缀。"""
    stripped = text.strip()
    # 前缀修饰
    stripped = re.sub(r"^(约|大约|大概|字数|word_count[:：])", "", stripped)
    # 后缀修饰
    stripped = re.sub(r"(字|字符|左右|以内|以上)$", "", stripped)
    return stripped.strip()


def parse_word_count(value) -> dict | None:
    """解析 ``word_count``，返回 ``{"min_chars", "max_chars"}`` 或 ``None``。

    不抛异常：任何无法解析的写法一律返回 ``None``，让调用方保持「只校验 >=200」
    的现状，绝不因字数配置写错而阻断写作。
    """
    if value is None:
        return None

    # 布尔不是数字，直接视为无法解析。
    if isinstance(value, bool):
        return None

    # 整数（含 numpy 等整数子类语义）按单值解析；0 表示「无目标」。
    if isinstance(value, int):
        if value <= 0:
            return None
        return _targets(float(value), float(value))

    if isinstance(value, float):
        if not value.is_integer() or value <= 0:
            return None
        return _targets(value, value)

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # 区间：A-B（剥掉「字」等后缀前先按数字区间匹配）
    m = _RANGE_RE.match(text)
    if m:
        low, high = float(m.group(1)), float(m.group(2))
        if low <= 0 or high <= 0:
            return None
        if low > high:
            low, high = high, low
        return _targets(low, high)

    cleaned = _strip_decorations(text)
    if not cleaned:
        return None

    if _NUMBER_RE.match(cleaned):
        n = float(cleaned)
        if n <= 0:
            return None
        return _targets(n, n)

    # 形如「约1800-2600字」这类带修饰的区间：剥修饰后再按区间匹配。
    m2 = _RANGE_RE.match(cleaned)
    if m2:
        low, high = float(m2.group(1)), float(m2.group(2))
        if low <= 0 or high <= 0:
            return None
        if low > high:
            low, high = high, low
        return _targets(low, high)

    return None


def _targets(low: float, high: float) -> dict:
    """由目标区间计算可判定区间（下限放宽 20%、上限放宽 20%）。"""
    return {
        "min_chars": int(low * 0.8),
        "max_chars": int(high * 1.2),
    }


def _literal_lower(value) -> int | None:
    """返回「目标下限」原文（区间取 A，单值取 N）；无法解析返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if not value.is_integer() or value <= 0:
            return None
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    m = _RANGE_RE.match(text)
    if m:
        low, high = int(m.group(1)), int(m.group(2))
        if low > 0 and high > 0:
            return min(low, high)
        return None
    cleaned = _strip_decorations(text)
    m2 = _RANGE_RE.match(cleaned)
    if m2:
        low, high = int(m2.group(1)), int(m2.group(2))
        if low > 0 and high > 0:
            return min(low, high)
        return None
    if _NUMBER_RE.match(cleaned):
        n = int(cleaned)
        return n if n > 0 else None
    return None


def format_word_count_hint(value) -> str | None:
    """把 ``word_count`` 格式化为写进 brief 的硬要求文案（含结尾换行）。

    无法解析返回 ``None``（调用方回退旧软提示）。示例：
    - ``"1800-2600"`` → ``目标字数：1800-2600 字（务必达到，低于 1800 字视为不合格）``
    - ``2000`` → ``目标字数：2000 字（务必达到，低于 2000 字视为不合格）``
    """
    lower = _literal_lower(value)
    if lower is None:
        return None
    if isinstance(value, str) and value.strip():
        display = value.strip()
    else:
        display = str(lower)
    return f"目标字数：{display} 字（务必达到，低于 {lower} 字视为不合格）\n"


def target_lower_bound(value) -> int | None:
    """返回「目标下限」原文（区间取较小的 A，单值取 N）；无法解析返回 None。

    供 executor 生成扩写指令「扩写到约 N 字」使用，让续写瞄准用户真实目标，
    而不是可判定阈值（threshold = N*0.8）。
    """
    return _literal_lower(value)
