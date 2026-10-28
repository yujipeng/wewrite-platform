"""S8-c 登录鉴权与域名白名单核心（JEE-1934）。

口径：

- **域名白名单**：``ALLOWED_HOSTS``（逗号分隔）只增白名单，**不替代**回环白名单；
  ``127.0.0.1`` / ``localhost`` / ``[::1]`` 永远免登录。未配置 ``ALLOWED_HOSTS`` 时，
  公网域名照旧被 Host 校验拒绝（fail closed）。
- **账号**：``WEWRITE_USERS`` 逗号分隔 ``username:secret``，secret 为 bcrypt 哈希或明文
  （只用于本地/内网临时配置，明文口令不落盘）。未配置任何账号时登录一律失败。
- **会话**：登录成功签发随机 bearer token，存 ``app.state.sessions``（进程内存，重启
  失效），通过 HttpOnly + SameSite cookie 下发；公网 HTTPS 时追加 ``Secure``。

本模块只依赖 stdlib + ``bcrypt``（可选），不落盘、不打日志，不碰 HTTP 路由。
"""

from __future__ import annotations

import secrets

try:
    import bcrypt

    _BCRYPT_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失降级
    bcrypt = None  # type: ignore
    _BCRYPT_AVAILABLE = False

# 会话 cookie 名
SESSION_COOKIE = "wewrite_session"

# 会话存活 token（进程内存）
SESSION_TTL_SECONDS = 12 * 60 * 60


class UsernameInvalid(ValueError):
    """账号配置里出现空账号等非法条目。"""


class UserStore:
    """从 ``WEWRITE_USERS`` 解析的账号表：``{username: secret}``。"""

    def __init__(self, raw: str | None):
        self._users: dict[str, str] = {}
        if raw is None:
            return
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" not in entry:
                raise UsernameInvalid(f"账号条目缺少 ':'：{entry!r}")
            username, secret = entry.split(":", 1)
            username = username.strip()
            secret = secret.strip()
            if not username or not secret:
                raise UsernameInvalid("账号或口令为空")
            if username in self._users:
                raise UsernameInvalid(f"重复账号：{username!r}")
            self._users[username] = secret

    def has_users(self) -> bool:
        return bool(self._users)

    def verify(self, username: str, password: str) -> bool:
        secret = self._users.get(username)
        if secret is None:
            # 用一次假哈希比较拉平时序，降低用户名枚举（best-effort）
            if _BCRYPT_AVAILABLE:
                bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
            return False
        return verify_password(self._users, username, password)


# 预设假哈希（用于无账号/未知账号时做常量时间比较，避免明显时序差异）
_DUMMY_HASH = b"$2b$12$C6UzMDM.H6dfI/f/IKcEeO9N5q1B0G0k9K5p0K5p0K5p0K5p0K5p0K5p0e"


def _looks_like_bcrypt(secret: str) -> bool:
    return secret.startswith("$2") and "$" in secret[3:]


def verify_password(users: dict[str, str], username: str, password: str) -> bool:
    """校验口令；secret 为 bcrypt 哈希时用 bcrypt 校验，否则按明文常量时间比较。"""
    secret = users.get(username)
    if secret is None:
        return False
    if _looks_like_bcrypt(secret):
        if not _BCRYPT_AVAILABLE:
            return False
        try:
            return bool(bcrypt.checkpw(password.encode("utf-8"), secret.encode("utf-8")))
        except ValueError:
            return False
    # 明文兜底：secrets.compare_digest 常量时间比较
    return secrets.compare_digest(password, secret)


def load_users(raw: str | None) -> dict[str, str]:
    """解析 ``WEWRITE_USERS`` 并返回账号表；非法条目抛 ``UsernameInvalid``。"""
    return UserStore(raw)._users


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
