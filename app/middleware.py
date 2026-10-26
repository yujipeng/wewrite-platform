"""Host / CSRF / Content-Type 中间件基线。

设计第 3 节：
- Host 只允许 ``127.0.0.1:<port>``、``[::1]:<port>``、``localhost:<port>``（防 DNS rebinding）。
- 变更方法（POST/PUT/DELETE）强制 ``Content-Type: application/json``、同源 ``Origin``、
  ``Sec-Fetch-Site: same-origin``、``X-CSRF-Token``。
- 普通 GET 与 ``/healthz`` 只做 Host 校验。
- 无 CORS。M1 落地这层，后续里程碑复用。
"""

from __future__ import annotations

import ipaddress
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from app.domain import errors

# 同源 Origin 的白名单主机（不含端口，端口随请求动态校验）
_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


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


def validate_host(host_header: str | None) -> bool:
    """校验 Host 头只允许回环白名单主机：127.0.0.1 / [::1] / localhost。

    端口不参与白名单校验（本机可用任意回环端口），但主机必须精确匹配，
    不放行整个 127/8（如 127.0.0.2 拒绝）。
    """
    bare, _port = normalize_host_port(host_header)
    if not bare:
        return False
    if bare == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    # 只放行回环且必须是 127.0.0.1 或 ::1（拒绝 127.0.0.2 等回环别名）
    return (ip.is_loopback) and (ip == ipaddress.ip_address("127.0.0.1") or ip == ipaddress.ip_address("::1"))


def origin_host_matches(origin: str | None, host_bare: str, host_port: int | None) -> bool:
    """校验 Origin 与 Host 头同源（host 与端口都一致），且 host 为回环白名单。

    Origin 形如 ``http://127.0.0.1:<port>``；端口须等于 Host 头端口（若 Origin
    省略默认端口，http=80 / https=443，与本机监听端口不一致即拒绝）。
    """
    if not origin:
        return False
    from urllib.parse import urlparse

    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    origin_host = parsed.hostname or ""
    if origin_host != host_bare:
        return False

    # 端口比对：Origin 缺省端口时按 scheme 补默认端口
    origin_port = parsed.port
    if origin_port is None:
        origin_port = 80 if parsed.scheme == "http" else 443
    if host_port is None:
        host_port = 80  # Host 无端口视为 80（http）
    if origin_port != host_port:
        return False

    # host 白名单（与 validate_host 一致，只允许精确回环地址或 localhost）
    if origin_host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(origin_host)
    except ValueError:
        return False
    return ip.is_loopback and (ip == ipaddress.ip_address("127.0.0.1") or ip == ipaddress.ip_address("::1"))


def referer_host_matches(referer: str | None, host_bare: str, host_port: int | None) -> bool:
    """校验 Referer 的 scheme+host+端口 与 Host 一致（同源 GET 无 Origin，用 Referer 兜底）。

    Referer 形如 ``http://127.0.0.1:<port>/path``；仅校验 scheme、host、端口三项，
    忽略路径。返回 False 表示缺失或不同源。
    """
    if not referer:
        return False
    from urllib.parse import urlparse

    try:
        parsed = urlparse(referer)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    referer_host = parsed.hostname or ""
    if referer_host != host_bare:
        return False
    referer_port = parsed.port
    if referer_port is None:
        referer_port = 80 if parsed.scheme == "http" else 443
    if host_port is None:
        host_port = 80
    if referer_port != host_port:
        return False
    if referer_host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(referer_host)
    except ValueError:
        return False
    return ip.is_loopback and (ip == ipaddress.ip_address("127.0.0.1") or ip == ipaddress.ip_address("::1"))


class HostCsrfMiddleware:
    """FastAPI 中间件：Host 校验 + 变更方法 CSRF/Content-Type 校验。

    交由 ``BaseHTTPMiddleware`` 包装；普通 GET 只做 Host 校验。
    """

    def __init__(self, app, csrf: CsrfStore):
        self.app = app
        self.csrf = csrf

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        host_header = request.headers.get("host")
        if not validate_host(host_header):
            await _respond_json(send, errors.ApiError("CSRF_INVALID", "Host 不被允许", 403).body(), 403)
            return

        method = request.method
        if method in {"POST", "PUT", "DELETE", "PATCH"}:
            # Content-Type 必须 application/json
            content_type = request.headers.get("content-type", "")
            if not content_type.lower().startswith("application/json"):
                await _respond_json(
                    send,
                    {"code": "VALIDATION_ERROR", "message": "Content-Type 必须为 application/json"},
                    422,
                )
                return

            # 同源 Origin（host + 端口都一致）
            bare_host, host_port = normalize_host_port(host_header)
            origin = request.headers.get("origin")
            if not origin_host_matches(origin, bare_host, host_port):
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
