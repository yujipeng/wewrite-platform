"""Host / CSRF / Content-Type / 登录鉴权中间件。

设计第 3 节 + S8-c（JEE-1934）扩展：

- Host 白名单 = 回环精确白名单（``127.0.0.1`` / ``[::1]`` / ``localhost``）∪
  环境变量 ``ALLOWED_HOSTS``（逗号分隔，精确匹配、大小写不敏感）。回环地址保持
  免登录；公网域名（白名单内非回环）必须先过登录鉴权。
- 变更方法（POST/PUT/DELETE）强制 ``Content-Type: application/json``、同源 ``Origin``、
  ``Sec-Fetch-Site: same-origin``、``X-CSRF-Token``；``/auth/login`` 与
  ``/auth/logout`` 例外（登录无会话、登出幂等，不做 CSRF 与 JSON Content-Type 要求）。
- 普通 GET 与 ``/healthz`` 只做 Host 校验；公网域名下 ``/static/*``、``/healthz``、
  ``/login``、``/auth/login``、``/auth/logout`` 免登录，其余页面 302 跳登录、API 401。
- Origin/Referer 同源校验：公网域名按 scheme + host + 端口比对（http 默认 80、
  https 默认 443），回环保持原有行为。无 CORS。
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from app import auth
from app.domain import errors

# 同源 Origin 的**回环**白名单主机（不含端口，端口随请求动态校验）
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

# 登录鉴权免检路径（精确匹配）
_AUTH_EXEMPT_PATHS = frozenset({"/healthz", "/login", "/auth/login", "/auth/logout"})

# 会话 cookie 名（与 app.auth.SESSION_COOKIE 一致）
SESSION_COOKIE = auth.SESSION_COOKIE


class CsrfStore:
    """进程内存里的 CSRF token 集合（重启即失效）。"""

    def __init__(self):
        self._tokens: set[str] = set()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        return token in self._tokens


class SessionStore:
    """进程内存会话表（重启即失效）：``{token: 过期单调时钟}``。"""

    def __init__(self):
        self._sessions: dict[str, float] = {}

    def create(self) -> str:
        token = auth.new_session_token()
        self._sessions[token] = time.monotonic() + auth.SESSION_TTL_SECONDS
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        expires = self._sessions.get(token)
        if expires is None:
            return False
        if time.monotonic() > expires:
            self._sessions.pop(token, None)
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)


def normalize_host_port(host_header: str | None) -> tuple[str, int | None]:
    """拆出 host 与 port；Host 头可能带端口或裸主机名。"""
    if not host_header:
        return "", None
    host = host_header.strip()
    port = None
    # IPv6 方括号 [::1]:8765
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return host, None
        bare = host[1:end]
        rest = host[end + 1:]
        if rest.startswith(":"):
            try:
                port = int(rest[1:]) if rest[1:] else None
            except ValueError:
                port = None
        return bare, port
    # 普通 host 或 host:port
    if ":" in host:
        bare, _, p = host.rpartition(":")
        try:
            port = int(p) if p else None
        except ValueError:
            port = None
        return bare, port
    return host, port


def is_loopback_bare(bare: str) -> bool:
    """裸主机名是否为精确回环白名单主机（127.0.0.1 / ::1 / localhost）。"""
    if bare == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    return ip.is_loopback and (
        ip == ipaddress.ip_address("127.0.0.1") or ip == ipaddress.ip_address("::1")
    )


def _allowed_hosts_from_env() -> frozenset[str]:
    """读取 ``ALLOWED_HOSTS``（逗号分隔），归一化为裸主机名集合（小写）。"""
    raw = os.environ.get("ALLOWED_HOSTS", "")
    hosts: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        bare, _port = normalize_host_port(entry)
        if bare:
            hosts.add(bare.lower())
    return frozenset(hosts)


def host_is_allowed(bare: str) -> bool:
    """主机是否在回环白名单或 ``ALLOWED_HOSTS`` 白名单内。"""
    if is_loopback_bare(bare):
        return True
    return bare.lower() in _allowed_hosts_from_env()


def validate_host(host_header: str | None) -> bool:
    """校验 Host 头只允许回环白名单或 ``ALLOWED_HOSTS`` 白名单主机。

    端口不参与白名单校验，但主机必须精确匹配，不放行整个 127/8（如 127.0.0.2 拒绝）。
    """
    bare, _port = normalize_host_port(host_header)
    if not bare:
        return False
    return host_is_allowed(bare)


def _default_port_for_scheme(scheme: str) -> int:
    return 80 if scheme == "http" else 443


def effective_scheme(scope: dict, headers) -> str:
    """请求的有效 scheme：优先 ``X-Forwarded-Proto``（反代 HTTPS 落地），否则 scope scheme。

    公网域名经 nginx/反代终止 TLS 后，后端 uvicorn 默认看到 http；但浏览器 Origin 是
    https。读取 ``X-Forwarded-Proto`` 才能正确把 Host 无端口时补成 443，避免误判 403。
    仅接受 ``http``/``https`` 两个合法值，其余回退 scope scheme。
    """
    xfp = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if xfp in {"http", "https"}:
        return xfp
    return scope.get("scheme", "http")


def _parse_origin_like(value: str) -> tuple[str, str, int] | None:
    """解析 Origin/Referer 为 ``(scheme, host_lower, effective_port)`` 三元组。

    scheme 只接受 http/https；端口缺失时按 scheme 补默认端口（http=80、https=443），
    显式端口与默认端口归一化到同一数值。解析失败返回 None。两个头部共用此函数，
    避免同源判定逻辑重复。
    """
    if not value:
        return None
    from urllib.parse import urlparse

    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    port = parsed.port
    if port is None:
        port = _default_port_for_scheme(scheme)
    return scheme, host, port


def _host_tuple(scheme: str, host_bare: str, host_port: int | None) -> tuple[str, str, int] | None:
    """Host 头的三元组 ``(scheme, host_lower, effective_port)``。

    Host 省略端口时按请求 scheme 补默认端口；显式端口与默认端口等价处理。host 为空返回 None。
    """
    bare = (host_bare or "").lower()
    if not bare:
        return None
    port = host_port
    if port is None:
        port = _default_port_for_scheme(scheme)
    return scheme.lower(), bare, port


def origin_host_matches(
    origin: str | None, host_bare: str, host_port: int | None, scheme: str = "http"
) -> bool:
    """校验 Origin 与 Host 头同源（scheme + host + 端口三元组全等），且 host 在白名单内。

    scheme 走 ``effective_scheme`` 传入；端口缺失时按各自 scheme 补默认端口再比较，
    显式端口与默认端口等价。公网域名走 HTTPS 时 Origin 必须同为 https，否则不同源。
    """
    origin_tuple = _parse_origin_like(origin or "")
    host_tuple = _host_tuple(scheme, host_bare, host_port)
    if origin_tuple is None or host_tuple is None:
        return False
    # 三元组全等：scheme + host + port 都一致
    if origin_tuple != host_tuple:
        return False
    return host_is_allowed(origin_tuple[1])


def referer_host_matches(
    referer: str | None, host_bare: str, host_port: int | None, scheme: str = "http"
) -> bool:
    """校验 Referer 的 scheme+host+端口 与 Host 一致（同源 GET 无 Origin，用 Referer 兜底）。

    与 ``origin_host_matches`` 共用同一套三元组解析与比较；仅校验 scheme、host、端口
    三项，忽略路径。返回 False 表示缺失或不同源。
    """
    referer_tuple = _parse_origin_like(referer or "")
    host_tuple = _host_tuple(scheme, host_bare, host_port)
    if referer_tuple is None or host_tuple is None:
        return False
    if referer_tuple != host_tuple:
        return False
    return host_is_allowed(referer_tuple[1])


def _path_exempt_from_auth(path: str) -> bool:
    """登录鉴权免检路径：静态资源、健康检查、登录页与登录/登出接口。"""
    if path.startswith("/static/"):
        return True
    return path in _AUTH_EXEMPT_PATHS


def _is_api_path(path: str) -> bool:
    return path.startswith("/api/")


def safe_next(raw: str | None, default: str = "/") -> str:
    """清洗登录跳转目标，只接受站内相对路径，其余回落到 ``default``。

    规则（防御开放重定向与 ``javascript:`` 等注入）：
    - 必须以单个 ``/`` 开头；
    - 不能以 ``//`` 开头（协议相对地址）；
    - 不得含反斜杠、控制字符（含 ``\r\n`` 换行注入）、空白；
    - 空值/缺失回落到默认落地页。
    """
    if not raw:
        return default
    if not raw.startswith("/") or raw.startswith("//"):
        return default
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return default
    if "\\" in raw or any(ch.isspace() for ch in raw):
        return default
    return raw


class HostCsrfMiddleware:
    """FastAPI 中间件：Host 校验 + 登录鉴权 + 变更方法 CSRF/Content-Type 校验。

    交由 ``BaseHTTPMiddleware`` 包装；普通 GET 只做 Host 校验（公网域名额外做鉴权）。
    """

    def __init__(self, app, csrf: CsrfStore, sessions: SessionStore, users: auth.UserStore):
        self.app = app
        self.csrf = csrf
        self.sessions = sessions
        self.users = users

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        scheme = effective_scheme(scope, request.headers)
        path = request.url.path

        # 1) Host 校验（回环白名单 ∪ ALLOWED_HOSTS，未知 Host 403）
        host_header = request.headers.get("host")
        if not validate_host(host_header):
            await _respond_json(send, errors.ApiError("CSRF_INVALID", "Host 不被允许", 403).body(), 403)
            return

        # 2) 登录鉴权：仅公网域名（非回环）需要；回环保持免登录
        bare_host, _host_port = normalize_host_port(host_header)
        if not is_loopback_bare(bare_host) and not _path_exempt_from_auth(path):
            token = request.cookies.get(SESSION_COOKIE)
            if not self.sessions.valid(token):
                if _is_api_path(path):
                    await _respond_json(
                        send,
                        {"code": "AUTH_REQUIRED", "message": "请先登录"},
                        401,
                    )
                else:
                    await _respond_redirect(send, "/login")
                return

        # 3) 变更方法 CSRF / Content-Type（登录/登出例外）
        method = request.method
        if method in {"POST", "PUT", "DELETE", "PATCH"} and path not in {
            "/auth/login",
            "/auth/logout",
        }:
            # Content-Type 必须 application/json
            content_type = request.headers.get("content-type", "")
            if not content_type.lower().startswith("application/json"):
                await _respond_json(
                    send,
                    {"code": "VALIDATION_ERROR", "message": "Content-Type 必须为 application/json"},
                    422,
                )
                return

            # 同源 Origin（scheme + host + 端口都一致）
            bare_host, host_port = normalize_host_port(host_header)
            origin = request.headers.get("origin")
            if not origin_host_matches(origin, bare_host, host_port, scheme):
                await _respond_json(send, errors.csrf_invalid({"reason": "origin_mismatch"}).body(), 403)
                return

            # Sec-Fetch-Site: same-origin
            sec_fetch_site = request.headers.get("sec-fetch-site")
            if sec_fetch_site != "same-origin":
                await _respond_json(send, errors.csrf_invalid({"reason": "sec_fetch_site"}).body(), 403)
                return

            # X-CSRF-Token 校验（/api/v1/bootstrap 拿 token）
            token = request.headers.get("x-csrf-token")
            if not self.csrf.valid(token):
                await _respond_json(send, errors.csrf_invalid().body(), 403)
                return

        await self.app(scope, receive, send)


async def _respond_json(send, body: dict, status_code: int):
    import json

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


async def _respond_redirect(send, location: str):
    payload = b""
    await send({
        "type": "http.response.start",
        "status": 302,
        "headers": [
            (b"location", location.encode("latin-1")),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload})
