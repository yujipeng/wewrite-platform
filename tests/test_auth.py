"""S8-c 公网域名白名单 + 账号密码登录鉴权（JEE-1934）。

覆盖：
1. ALLOWED_HOSTS 白名单：公网域名过 Host 校验，回环保持免登录，未知 Host 403；
2. 登录/登出：POST /auth/login 校验账号密码、发 HttpOnly+SameSite 会话 cookie；
3. 越权拦截：公网未登录访问页面 302 跳登录、API 401，登录后凭会话放行；
4. 安全协调：公网域名 Origin/Referer 按同源 scheme+host+端口适配，回环行为不变；
5. WEWRITE_USERS 账号解析（明文/哈希兼容、拒绝非法条目、缺失时登录 fail closed）。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    SESSION_COOKIE,
    UsernameInvalid,
    load_users,
    verify_password,
)
from app.main import create_app


# ---------- 账号配置口径 ----------

def test_load_users_bcrypt_and_plaintext():
    hashed = "$2b$12$Fwc.pE7voipqvxTlBTTUaOrUIJKonOYxEXD/M6SIk5deu0FdTBbxG"  # secret123
    users = load_users(f"alice:{hashed},bob:plain-pw")
    assert set(users) == {"alice", "bob"}
    assert verify_password(users, "alice", "secret123") is True
    assert verify_password(users, "alice", "wrong") is False
    assert verify_password(users, "bob", "plain-pw") is True


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
