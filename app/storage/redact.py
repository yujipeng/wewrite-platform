"""密钥脱敏工具：日志与错误正文不得出现明文或末四位。"""

from __future__ import annotations

import re

# 常见密钥/令牌形态：Bearer token、key=value、长 hex/base64。
_BEARER_RE = re.compile(r"(bearer\s+)([A-Za-z0-9._~+/=-]{9,})", re.IGNORECASE)
_KEY_VALUE_RE = re.compile(r"(api[_-]?key|secret|token|access_token)([\"'\\s:=]+)([A-Za-z0-9._~+/=-]{9,})", re.IGNORECASE)


def redact_secret(text: str) -> str:
    """把疑似密钥/令牌整体替换为 ``[REDACTED]``，不保留任何前缀或末四位。

    只处理明显的长令牌形态，不误伤普通中文正文。返回脱敏后的字符串。
    """
    if not isinstance(text, str):
        return text
    out = _BEARER_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    out = _KEY_VALUE_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", out)
    return out


def secret_configured(value: str | None) -> bool:
    """仅返回是否已配置布尔，绝不回显明文/末四位。"""
    return bool(value)
