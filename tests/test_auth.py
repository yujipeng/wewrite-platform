"""S8-c 公网域名白名单 + 账号密码登录鉴权（JEE-1934）。

覆盖：
1. ALLOWED_HOSTS 白名单：公网域名过 Host 校验，回环保持免登录，未知 Host 403；
2. 登录/登出：POST /auth/login 校验账号密码、发 HttpOnly+SameSite 会话 cookie；
3. 越权拦截：公网未登录访问页面 302 跳登录、API 401，登录后凭会话放行；
4. 安全协调：公网域名 Origin/Referer 按同源 scheme+host+端口适配，回环行为不变；
5. WEWRITE_USERS 账号解析（仅 bcrypt 哈希、拒绝明文/非法条目、缺失时 fail closed）；
6. 登录跳转 ``next`` 清洗（开放重定向 / javascript: 注入防护）。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    SESSION_COOKIE,
    UsernameInvalid,
    is_supported_password_hash,
    load_users,
    verify_password,
)
from app.main import create_app
from app.middleware import safe_next


# ---------- 账号配置口径 ----------

def test_load_users_rejects_plaintext():
    """非 bcrypt 哈希条目必须 fail closed：不回落明文比较。"""
    hashed = "$2b$12$Fwc.pE7voipqvxTlBTTUaOrUIJKonOYxEXD/M6SIk5deu0FdTBbxG"  # secret123
    users = load_users(f"alice:{hashed},bob:plain-pw")
    assert set(users) == {"alice"}  # bob 被拒绝
    assert verify_password(users, "alice", "secret123") is True
    assert verify_password(users, "alice", "wrong") is False
    # 明文账号不可登录（根本不在账号表里）
    assert "bob" not in users


def test_is_supported_password_hash():
    good = "$2b$12$Fwc.pE7voipqvxTlBTTUaOrUIJKonOYxEXD/M6SIk5deu0FdTBbxG"
    assert is_supported_password_hash(good) is True
    assert is_supported_password_hash("$2a$12$Fwc.pE7voipqvxTlBTTUaOrUIJKonOYxEXD/M6SIk5deu0FdTBbxG") is True
    assert is_supported_password_hash("$2y$12$Fwc.pE7voipqvxTlBTTUaOrUIJKonOYxEXD/M6SIk5deu0FdTBbxG") is True
    # 明文 / 其它哈希 / 损坏哈希 均拒绝
    assert is_supported_password_hash("plain-pw") is False
    assert is_supported_password_hash("$2b$12$tooshort") is False
    assert is_supported_password_hash("$6$rounds=656000$salt$hash") is False


def test_load_users_rejects_empty_username_and_malformed():
    with pytest.raises(UsernameInvalid):
        load_users(":pw")
    with pytest.raises(UsernameInvalid):
        load_users("alice")  # 无冒号
    with pytest.raises(UsernameInvalid):
        load_users("a:x,b:y,:z")


def test_verify_password_empty_fail_closed():
    assert verify_password({}, "any", "pw") is False


# ---------- 客户端构造 ----------

def _hashed() -> str:
    import bcrypt

    return bcrypt.hashpw(b"secret123", bcrypt.gensalt(rounds=4)).decode()


def make_client(monkeypatch, home, *, users="alice", allowed_hosts="temp.os-uat.tcredit.com"):
    monkeypatch.setenv("WEWRITE_HOME", home)
    monkeypatch.setenv("WEWRITE_PLATFORM_NO_WORKER", "1")
    monkeypatch.setenv("WEWRITE_USERS", users)
    monkeypatch.setenv("ALLOWED_HOSTS", allowed_hosts)
    app = create_app()
    c = TestClient(app, base_url="https://temp.os-uat.tcredit.com")
    c.headers.update({"host": "temp.os-uat.tcredit.com"})
    return c


# ---------- 白名单 ----------

def test_allowed_host_healthz_ok(monkeypatch, tmp_path):
    c = make_client(monkeypatch, str(tmp_path))
    r = c.get("/healthz", headers={"host": "temp.os-uat.tcredit.com"})
    assert r.status_code == 200


def test_unknown_host_still_rejected(monkeypatch, tmp_path):
    c = make_client(monkeypatch, str(tmp_path))
    r = c.get("/healthz", headers={"host": "evil.example"})
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"


def test_loopback_no_auth_required(monkeypatch, tmp_path):
    """回环访问保持现状：免登录，/api/v1/bootstrap 可直接拿 token。"""
    monkeypatch.setenv("WEWRITE_HOME", str(tmp_path))
    monkeypatch.setenv("WEWRITE_PLATFORM_NO_WORKER", "1")
    monkeypatch.setenv("ALLOWED_HOSTS", "temp.os-uat.tcredit.com")
    app = create_app()
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update({"host": "127.0.0.1:8765"})
    r = c.get("/api/v1/bootstrap")
    assert r.status_code == 200
    assert "csrf_token" in r.json()


# ---------- 越权拦截 ----------

def test_public_api_requires_auth(monkeypatch, tmp_path):
    c = make_client(monkeypatch, str(tmp_path))
    r = c.get("/api/v1/runs")
    assert r.status_code == 401
    assert r.json()["code"] == "AUTH_REQUIRED"


def test_public_page_redirects_to_login(monkeypatch, tmp_path):
    c = make_client(monkeypatch, str(tmp_path))
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.headers.get("location") or "")


def test_public_static_and_healthz_open(monkeypatch, tmp_path):
    """静态资源与健康检查免登录（登录页样式也要能加载）。"""
    c = make_client(monkeypatch, str(tmp_path))
    assert c.get("/healthz").status_code == 200
    assert c.get("/static/styles.css").status_code == 200
    assert c.get("/static/app.js").status_code == 200


def test_login_sets_session_cookie_and_allows_access(monkeypatch, tmp_path):
    c = make_client(monkeypatch, str(tmp_path), users=f"alice:{_hashed()}")
    r = c.post(
        "/auth/login",
        data={"username": "alice", "password": "secret123"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    # Set-Cookie 带会话 cookie（HttpOnly + SameSite）
    set_cookie = r.headers.get("set-cookie") or ""
    assert SESSION_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite" in set_cookie
    assert "Secure" in set_cookie  # 公网域名走 HTTPS 时应带 Secure

    cookie = r.cookies.get(SESSION_COOKIE)
    assert cookie
    r2 = c.get("/api/v1/runs", cookies={SESSION_COOKIE: cookie})
    assert r2.status_code == 200


def test_login_wrong_password_rejected(monkeypatch, tmp_path):
    c = make_client(monkeypatch, str(tmp_path), users=f"alice:{_hashed()}")
    r = c.post("/auth/login", data={"username": "alice", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["code"] == "AUTH_INVALID"


def test_logout_clears_session(monkeypatch, tmp_path):
    c = make_client(monkeypatch, str(tmp_path), users=f"alice:{_hashed()}")
    r = c.post(
        "/auth/login",
        data={"username": "alice", "password": "secret123"},
    )
    cookie = r.cookies.get(SESSION_COOKIE)
    r2 = c.post("/auth/logout", cookies={SESSION_COOKIE: cookie})
    assert r2.status_code == 200
    # 登出后会话失效
    r3 = c.get("/api/v1/runs", cookies={SESSION_COOKIE: cookie})
    assert r3.status_code == 401


def test_login_csrf_not_required_on_public_login(monkeypatch, tmp_path):
    """登录接口本身不能要求已登录会话的 CSRF token（否则永远登不上）。"""
    c = make_client(monkeypatch, str(tmp_path), users=f"alice:{_hashed()}")
    r = c.post("/auth/login", data={"username": "alice", "password": "secret123"})
    assert r.status_code == 200


def test_auth_required_does_not_break_healthz_host_check(monkeypatch, tmp_path):
    c = make_client(monkeypatch, str(tmp_path))
    # 未知 Host 在鉴权判断前就被 403（Host 校验优先级最高）
    r = c.get("/healthz", headers={"host": "evil.example"})
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"


# ---------- 公网 Origin/Referer 同源适配 ----------

def _login(c):
    r = c.post("/auth/login", data={"username": "alice", "password": "secret123"})
    assert r.status_code == 200, r.text
    return r.cookies.get(SESSION_COOKIE)


def test_public_origin_scheme_https_accepted(monkeypatch, tmp_path):
    c = make_client(monkeypatch, str(tmp_path), users=f"alice:{_hashed()}")
    cookie = _login(c)
    token = c.get("/api/v1/bootstrap", cookies={SESSION_COOKIE: cookie}).json()["csrf_token"]
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://temp.os-uat.tcredit.com",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": token,
    }
    r = c.post("/api/v1/preview", json={}, cookies={SESSION_COOKIE: cookie}, headers=headers)
    # 进入路由层即通过中间件（此处 preview 自身 400 RUN_ID_INVALID，说明 CSRF 已放行）
    assert r.status_code == 400
    assert r.json()["code"] == "RUN_ID_INVALID"


def test_public_origin_http_mismatch_rejected(monkeypatch, tmp_path):
    """公网 HTTPS 请求却带 http:// Origin → 不同源，应 403。"""
    c = make_client(monkeypatch, str(tmp_path), users=f"alice:{_hashed()}")
    cookie = _login(c)
    token = c.get("/api/v1/bootstrap", cookies={SESSION_COOKIE: cookie}).json()["csrf_token"]
    headers = {
        "Content-Type": "application/json",
        "Origin": "http://temp.os-uat.tcredit.com",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": token,
    }
    r = c.post("/api/v1/preview", json={}, cookies={SESSION_COOKIE: cookie}, headers=headers)
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"


def test_public_https_origin_via_forwarded_proto(monkeypatch, tmp_path):
    """反代落地形态：后端 scope=http 但 X-Forwarded-Proto=https，Origin=https 应同源放行。"""
    monkeypatch.setenv("WEWRITE_HOME", str(tmp_path))
    monkeypatch.setenv("WEWRITE_PLATFORM_NO_WORKER", "1")
    monkeypatch.setenv("WEWRITE_USERS", f"alice:{_hashed()}")
    monkeypatch.setenv("ALLOWED_HOSTS", "temp.os-uat.tcredit.com")
    app = create_app()
    # 后端实际看到的是 http 请求，靠 X-Forwarded-Proto 判断对外是 https
    c = TestClient(app, base_url="http://temp.os-uat.tcredit.com")
    c.headers.update({"host": "temp.os-uat.tcredit.com"})
    cookie = _login(c)
    token = c.get("/api/v1/bootstrap", cookies={SESSION_COOKIE: cookie}).json()["csrf_token"]
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://temp.os-uat.tcredit.com",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": token,
        "X-Forwarded-Proto": "https",
    }
    r = c.post("/api/v1/preview", json={}, cookies={SESSION_COOKIE: cookie}, headers=headers)
    # 通过中间件即进入路由层（preview 自身 400 RUN_ID_INVALID）
    assert r.status_code == 400
    assert r.json()["code"] == "RUN_ID_INVALID"


def test_public_https_origin_without_forwarded_proto_rejected(monkeypatch, tmp_path):
    """后端 http 且未带 X-Forwarded-Proto，Origin=https 应不同源 403（fail closed）。"""
    monkeypatch.setenv("WEWRITE_HOME", str(tmp_path))
    monkeypatch.setenv("WEWRITE_PLATFORM_NO_WORKER", "1")
    monkeypatch.setenv("WEWRITE_USERS", f"alice:{_hashed()}")
    monkeypatch.setenv("ALLOWED_HOSTS", "temp.os-uat.tcredit.com")
    app = create_app()
    c = TestClient(app, base_url="http://temp.os-uat.tcredit.com")
    c.headers.update({"host": "temp.os-uat.tcredit.com"})
    cookie = _login(c)
    token = c.get("/api/v1/bootstrap", cookies={SESSION_COOKIE: cookie}).json()["csrf_token"]
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://temp.os-uat.tcredit.com",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": token,
    }
    r = c.post("/api/v1/preview", json={}, cookies={SESSION_COOKIE: cookie}, headers=headers)
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_INVALID"


# ---------- 明文口令 fail closed（返修第 4 项） ----------

def test_plaintext_user_cannot_login(monkeypatch, tmp_path):
    """只配明文口令时整体拒绝登录（fail closed）。"""
    c = make_client(monkeypatch, str(tmp_path), users="alice:plain-pw")
    r = c.post("/auth/login", data={"username": "alice", "password": "plain-pw"})
    assert r.status_code == 401
    assert r.json()["code"] == "AUTH_INVALID"


def test_mixed_users_only_valid_bcrypt_can_login(monkeypatch, tmp_path):
    """混合配置（一条合法 bcrypt + 一条明文）：只有合法账号可登录，明文账号被拒。"""
    c = make_client(monkeypatch, str(tmp_path), users=f"alice:{_hashed()},bob:plain-pw")
    # 合法账号可登录
    r = c.post("/auth/login", data={"username": "alice", "password": "secret123"})
    assert r.status_code == 200
    # 明文账号不可登录
    r2 = c.post("/auth/login", data={"username": "bob", "password": "plain-pw"})
    assert r2.status_code == 401
    assert r2.json()["code"] == "AUTH_INVALID"


# ---------- 登录跳转 next 清洗（返修第 1 项） ----------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("javascript:alert(1)", "/"),
        ("//evil.example", "/"),
        ("https://evil.example", "/"),
        ("/\\evil.example", "/"),
        ("", "/"),
        ("/runs/abc", "/runs/abc"),  # 合法站内相对路径应保留
    ],
)
def test_safe_next_sanitizes(raw, expected):
    assert safe_next(raw) == expected


def test_login_page_next_javascript_not_injected(monkeypatch, tmp_path):
    """登录页对 ?next=javascript:... 不执行脚本、不回落到外站。"""
    monkeypatch.setenv("WEWRITE_HOME", str(tmp_path))
    monkeypatch.setenv("WEWRITE_PLATFORM_NO_WORKER", "1")
    monkeypatch.setenv("ALLOWED_HOSTS", "temp.os-uat.tcredit.com")
    app = create_app()
    c = TestClient(app, base_url="https://temp.os-uat.tcredit.com")
    c.headers.update({"host": "temp.os-uat.tcredit.com"})
    r = c.get("/login?next=javascript:alert(1)")
    assert r.status_code == 200
    html = r.text
    # 页面内嵌了 safeNext 清洗函数，且不会把原始 next 原样赋给 location.href
    assert "safeNext" in html
    assert 'location.href = next' in html  # 只赋清洗后的值


# ---------- Origin/Referer 同源 scheme + 端口（返修第 2 项） ----------

# 直接测 origin_host_matches / referer_host_matches 纯函数，覆盖两个头部各三类场景，
# 避免正例触发真实热榜抓取出网。


def _pub_ctx(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "temp.os-uat.tcredit.com")
    return "temp.os-uat.tcredit.com"


def test_origin_explicit_default_port_https_accepted(monkeypatch):
    """HTTPS 请求 + Origin=https://host:443（显式默认端口）应等价同源。"""
    from app.middleware import origin_host_matches

    host = _pub_ctx(monkeypatch)
    assert origin_host_matches("https://temp.os-uat.tcredit.com:443", host, None, "https") is True
    # 省略端口的 Origin 等价于 443
    assert origin_host_matches("https://temp.os-uat.tcredit.com", host, None, "https") is True


def test_origin_http_vs_https_scheme_mismatch_rejected(monkeypatch):
    """HTTPS 请求 + Origin=http://host:443 → scheme 不符，应不同源。"""
    from app.middleware import origin_host_matches

    host = _pub_ctx(monkeypatch)
    assert origin_host_matches("http://temp.os-uat.tcredit.com:443", host, None, "https") is False


def test_origin_port_mismatch_rejected(monkeypatch):
    """HTTPS 请求 + Origin=https://host:444 → 端口不符，应不同源。"""
    from app.middleware import origin_host_matches

    host = _pub_ctx(monkeypatch)
    assert origin_host_matches("https://temp.os-uat.tcredit.com:444", host, None, "https") is False


def test_referer_explicit_default_port_https_accepted(monkeypatch):
    """HTTPS 请求 + Referer=https://host:443/path → 等价同源放行。"""
    from app.middleware import referer_host_matches

    host = _pub_ctx(monkeypatch)
    assert referer_host_matches("https://temp.os-uat.tcredit.com:443/path", host, None, "https") is True


def test_referer_http_vs_https_scheme_mismatch_rejected(monkeypatch):
    """HTTPS 请求 + Referer=http://host:443/path → scheme 不符，应不同源。"""
    from app.middleware import referer_host_matches

    host = _pub_ctx(monkeypatch)
    assert referer_host_matches("http://temp.os-uat.tcredit.com:443/path", host, None, "https") is False


def test_referer_port_mismatch_rejected(monkeypatch):
    """HTTPS 请求 + Referer=https://host:444/path → 端口不符，应不同源。"""
    from app.middleware import referer_host_matches

    host = _pub_ctx(monkeypatch)
    assert referer_host_matches("https://temp.os-uat.tcredit.com:444/path", host, None, "https") is False
