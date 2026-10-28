"""S8-c 登录鉴权与域名白名单核心（JEE-1934）。

口径：

- **域名白名单**：``ALLOWED_HOSTS``（逗号分隔）只增白名单，**不替代**回环白名单；
  ``127.0.0.1`` / ``localhost`` / ``[::1]`` 永远免登录。未配置 ``ALLOWED_HOSTS`` 时，
  公网域名照旧被 Host 校验拒绝（fail closed）。
- **账号**：``WEWRITE_USERS`` 逗号分隔 ``username:secret``，secret **必须是受支持的
  bcrypt 哈希**（``$2a$`` / ``$2b$`` / ``$2y$`` 前缀且可被 bcrypt 解析）。非哈希条目
  一律 fail closed（拒绝该账号，绝不回落明文比较）；未配置任何账号时登录一律失败。
- **会话**：登录成功签发随机 bearer token，存 ``app.state.sessions``（进程内存，重启
  失效），通过 HttpOnly + SameSite cookie 下发；公网 HTTPS 时追加 ``Secure``。

本模块只依赖 stdlib + ``bcrypt``（可选），不落盘、不打印密码内容。
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
    """账号配置里出现结构非法条目（空账号、缺冒号、重复账号）。"""


def is_supported_password_hash(secret: str) -> bool:
    """secret 是否为受支持的 bcrypt 哈希。

    仅接受 ``$2a$`` / ``$2b$`` / ``$2y$`` 前缀且可被 bcrypt 正常解析的字符串；
    其余（明文、其它哈希、损坏的哈希）一律返回 False（fail closed）。
    """
    if not _BCRYPT_AVAILABLE:
        return False
    if not (
        secret.startswith("$2a$")
        or secret.startswith("$2b$")
        or secret.startswith("$2y$")
    ):
        return False
    try:
        # 用一个空 probe 口令触发 bcrypt 解析盐；合法哈希返回 False 不抛异常，
        # 盐非法/长度错误抛 ValueError。这里只用于格式校验，不比较口令。
        bcrypt.checkpw(b"", secret.encode("utf-8"))
    except Exception:
        return False
    return True


class UserStore:
    """从 ``WEWRITE_USERS`` 解析的账号表：仅保留合法的 bcrypt 账号。"""

    def __init__(self, raw: str | None):
        self._users: dict[str, str] = {}
        # 被拒绝的账号（仅用户名，不含密码内容），供启动日志提示配置非法
        self.rejected: list[str] = []
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
            if username in self._users or username in self.rejected:
                raise UsernameInvalid(f"重复账号：{username!r}")
            if is_supported_password_hash(secret):
                self._users[username] = secret
            else:
                # 非 bcrypt 哈希：拒绝该账号，不回落明文比较
                self.rejected.append(username)

    def has_users(self) -> bool:
        return bool(self._users)

    def verify(self, username: str, password: str) -> bool:
        return verify_password(self._users, username, password)


# 预设假哈希（用于未知账号时做常量时间比较，避免明显时序差异）
_DUMMY_HASH = b"$2b$12$C6UzMDM.H6dfI/f/IKcEeO9N5q1B0G0k9K5p0K5p0K5p0K5p0K5p0K5p0e"


def verify_password(users: dict[str, str], username: str, password: str) -> bool:
    """校验口令；只接受 bcrypt 哈希账号，无明文兜底（fail closed）。"""
    secret = users.get(username)
    if secret is None:
        # 未知账号：用假哈希做常量时间比较，降低用户名枚举（best-effort）
        if _BCRYPT_AVAILABLE:
            try:
                bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
            except ValueError:
                pass
        return False
    if not _BCRYPT_AVAILABLE:
        return False
    try:
        return bool(bcrypt.checkpw(password.encode("utf-8"), secret.encode("utf-8")))
    except ValueError:
        return False


def load_users(raw: str | None) -> dict[str, str]:
    """解析 ``WEWRITE_USERS`` 并返回仅含合法 bcrypt 账号的映射。

    非哈希条目被静默拒绝（不在此函数抛异常）；结构性错误（空账号/缺冒号/重复）抛
    ``UsernameInvalid``。拒绝明细见 ``UserStore.rejected``。
    """
    return UserStore(raw)._users


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
