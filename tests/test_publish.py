"""M4 P7/P8 测试：预检各失败项（含摘要三条边界）、token 生命周期、密钥不回显、
ENV_MANAGED 整包失败、路径穿越、授权撤销废 token。"""

from __future__ import annotations

import json
import os

import yaml
from fastapi.testclient import TestClient

from app.domain import preflight as preflight_mod
from app.main import create_app


def write_artifact(home, run_id, rel, content: bytes | str):
    d = os.path.join(home, "runs", run_id)
    os.makedirs(d, exist_ok=True)
    full = os.path.join(d, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(full, mode) as f:
        f.write(content)


def write_state(home, run_id, state: dict):
    d = os.path.join(home, "runs", run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "state.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)


BODY = "这是正文。" * 60  # 300 字符


def base_state(run_id, *, title="发布标题", digest="摘要", publish=True, publishable=True, seo_digest="摘要"):
    return {
        "version": 4,
        "run_id": run_id,
        "status": "completed",
        "mode": "complete",
        "permissions": {"publish": publish},
        "editorial": {"decision": "pass" if publishable else "fail", "publishable": publishable},
        "seo": {"title": title, "digest": seo_digest},
        "visual": {"mode": "full", "max_images": 0, "max_cost": None},
        "images": {"cover": "images/cover.png"},
        "artifacts": {
            "article": f"runs/{run_id}/article.md",
            "illustrated_article": f"runs/{run_id}/article-illustrated.md",
        },
        "topic": {"title": title},
    }


def make_publishable_run(home, run_id="20261026-100000-abcdef", **kw):
    state = base_state(run_id, **kw)
    write_state(home, run_id, state)
    write_artifact(home, run_id, "article.md", "# 发布标题\n\n" + BODY)
    write_artifact(home, run_id, "images/cover.png", b"\x89PNG\r\n\x1a\nfakepng")
    return state


def make_client(monkeypatch, home, adapter=None):
    monkeypatch.setenv("WEWRITE_HOME", home)
    monkeypatch.setenv("WEWRITE_PLATFORM_NO_WORKER", "1")
    app = create_app()
    if adapter is not None:
        app.state.wechat_adapter = adapter
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update({"host": "127.0.0.1:8765"})
    return c


def post_headers(client, extra=None):
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    h = {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:8765",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": token,
    }
    h.update(extra or {})
    return h


class FakeWeChatAdapter:
    """假微信写入：authorize 写 state，commit 返回固定 media_id，get_draft 返回传入正文。"""

    def __init__(self, home):
        self.home = home
        self.commits = 0

    def set_publish_permission(self, allow, run_id):
        state = yaml.safe_load(open(os.path.join(self.home, "runs", run_id, "state.yaml"), encoding="utf-8"))
        state.setdefault("permissions", {})["publish"] = bool(allow)
        write_state(self.home, run_id, state)
        return state

    def commit(self, run_id, proj, title):
        self.commits += 1
        self.last_proj = proj
        return {"media_id": "media-fake-1"}

    def get_draft(self, media_id):
        # 与本地正文一致，供 reconcile 判一致
        from wewrite.toolkit.publisher import html_to_plaintext

        return html_to_plaintext("")


# ---------- 预检 ----------

def test_preflight_all_ok(monkeypatch, tmp_path):
    home = str(tmp_path)
    make_publishable_run(home)
    c = make_client(monkeypatch, home)
    r = c.get("/api/v1/runs/20261026-100000-abcdef/publish/preflight")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["revision"].startswith("sha256:")
    checks = {ch["name"]: ch for ch in body["checks"]}
    assert all(ch["ok"] for ch in body["checks"])


def test_preflight_fails_when_not_publishable(monkeypatch, tmp_path):
    home = str(tmp_path)
    make_publishable_run(home, publish=False, publishable=False)
    c = make_client(monkeypatch, home)
    r = c.get("/api/v1/runs/20261026-100000-abcdef/publish/preflight")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    checks = {ch["name"]: ch for ch in body["checks"]}
    assert checks["permissions"]["code"] == "PUBLISH_NOT_ALLOWED"
    assert checks["editorial"]["code"] == "NOT_PUBLISHABLE"


def test_preflight_title_empty(monkeypatch, tmp_path):
    home = str(tmp_path)
    make_publishable_run(home, title="", seo_digest="摘要")
    c = make_client(monkeypatch, home)
    r = c.get("/api/v1/runs/20261026-100000-abcdef/publish/preflight")
    body = r.json()
    checks = {ch["name"]: ch for ch in body["checks"]}
    assert checks["title"]["code"] == "TITLE_EMPTY"


def test_preflight_cover_missing(monkeypatch, tmp_path):
    home = str(tmp_path)
    make_publishable_run(home)
    os.remove(os.path.join(home, "runs", "20261026-100000-abcdef", "images", "cover.png"))
    c = make_client(monkeypatch, home)
    r = c.get("/api/v1/runs/20261026-100000-abcdef/publish/preflight")
    checks = {ch["name"]: ch for ch in r.json()["checks"]}
    assert checks["cover"]["code"] == "COVER_MISSING"


def test_preflight_body_too_short(monkeypatch, tmp_path):
    home = str(tmp_path)
    make_publishable_run(home)
    write_artifact(home, "20261026-100000-abcdef", "article.md", "# 标题\n\n短正文")
    c = make_client(monkeypatch, home)
    r = c.get("/api/v1/runs/20261026-100000-abcdef/publish/preflight")
    checks = {ch["name"]: ch for ch in r.json()["checks"]}
    assert checks["body"]["code"] == "BODY_LENGTH"


def test_preflight_digest_boundary_bytes(monkeypatch, tmp_path):
    """摘要三条边界：ASCII 120 通过/121 拒；汉字 40 字通过/41 拒；emoji 30 通过/31 拒。"""
    home = str(tmp_path)
    run_id = "20261026-100000-abcdef"
    cases = [
        ("a" * 120, True),
        ("a" * 121, False),
        ("汉" * 40, True),
        ("汉" * 41, False),
        ("😀" * 30, True),
        ("😀" * 31, False),
    ]
    for digest, expected_ok in cases:
        make_publishable_run(home, run_id, seo_digest=digest)
        result = preflight_mod.check(home, run_id)
        digest_check = next(ch for ch in result["checks"] if ch["name"] == "digest")
        assert digest_check["ok"] is expected_ok, (digest, digest_check)


def test_preflight_digest_manual_empty_string_preferred(monkeypatch, tmp_path):
    """手填摘要字段存在（空串）也优先 → digest 为空串，字节数 0 通过。"""
    home = str(tmp_path)
    make_publishable_run(home, seo_digest="")
    result = preflight_mod.check(home, "20261026-100000-abcdef")
    digest_check = next(ch for ch in result["checks"] if ch["name"] == "digest")
    assert digest_check["ok"] is True


# ---------- token 生命周期 ----------

def _preflight_revision(client, run_id):
    return client.get(f"/api/v1/runs/{run_id}/publish/preflight").json()["revision"]


def test_publish_token_and_commit_flow(monkeypatch, tmp_path):
    home = str(tmp_path)
    run_id = "20261026-100000-abcdef"
    make_publishable_run(home, run_id)
    adapter = FakeWeChatAdapter(home)
    c = make_client(monkeypatch, home, adapter)

    # 授权
    r = c.put(f"/api/v1/runs/{run_id}/publish/permission", json={"allow": True, "reason": "测试"}, headers=post_headers(c))
    assert r.status_code == 200
    assert r.json()["permissions"]["publish"] is True

    rev = _preflight_revision(c, run_id)
    title = "发布标题"
    r = c.post(f"/api/v1/runs/{run_id}/publish/token", json={"revision": rev, "checked": True, "title": title}, headers=post_headers(c))
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert token

    # 磁盘只存 hash，不明文
    publish_dir = os.path.join(home, "publish")
    files = [f for f in os.listdir(publish_dir) if f.endswith(".json")]
    assert len(files) == 1
    raw = open(os.path.join(publish_dir, files[0]), encoding="utf-8").read()
    assert token not in raw
    assert "token_hash" in raw

    # 提交
    r = c.post(f"/api/v1/runs/{run_id}/publish", json={"token": token, "title": title, "checked": True}, headers=post_headers(c))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"
    assert r.json()["media_id"] == "media-fake-1"
    assert adapter.commits == 1


def test_publish_token_replay_rejected(monkeypatch, tmp_path):
    home = str(tmp_path)
    run_id = "20261026-100000-abcdef"
    make_publishable_run(home, run_id)
    adapter = FakeWeChatAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    c.put(f"/api/v1/runs/{run_id}/publish/permission", json={"allow": True}, headers=post_headers(c))
    rev = _preflight_revision(c, run_id)
    token = c.post(f"/api/v1/runs/{run_id}/publish/token", json={"revision": rev, "checked": True, "title": "发布标题"}, headers=post_headers(c)).json()["token"]
    assert c.post(f"/api/v1/runs/{run_id}/publish", json={"token": token, "title": "发布标题", "checked": True}, headers=post_headers(c)).status_code == 200
    # 重放（同 token）
    r = c.post(f"/api/v1/runs/{run_id}/publish", json={"token": token, "title": "发布标题", "checked": True}, headers=post_headers(c))
    assert r.status_code == 409
    assert r.json()["code"] == "TOKEN_INVALID"
    assert adapter.commits == 1


def test_publish_token_revoked_after_permission_off(monkeypatch, tmp_path):
    home = str(tmp_path)
    run_id = "20261026-100000-1a2b3c"
    make_publishable_run(home, run_id)
    adapter = FakeWeChatAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    c.put(f"/api/v1/runs/{run_id}/publish/permission", json={"allow": True}, headers=post_headers(c))
    rev = _preflight_revision(c, run_id)
    token = c.post(f"/api/v1/runs/{run_id}/publish/token", json={"revision": rev, "checked": True, "title": "发布标题"}, headers=post_headers(c)).json()["token"]
    # 撤销授权 → token 作废（失效）
    c.put(f"/api/v1/runs/{run_id}/publish/permission", json={"allow": False}, headers=post_headers(c))
    r = c.post(f"/api/v1/runs/{run_id}/publish", json={"token": token, "title": "发布标题", "checked": True}, headers=post_headers(c))
    assert r.status_code == 409
    assert r.json()["code"] == "TOKEN_INVALID"
    assert adapter.commits == 0


def test_publish_title_mismatch_rejected(monkeypatch, tmp_path):
    home = str(tmp_path)
    run_id = "20261026-100000-abcdef"
    make_publishable_run(home, run_id)
    adapter = FakeWeChatAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    c.put(f"/api/v1/runs/{run_id}/publish/permission", json={"allow": True}, headers=post_headers(c))
    rev = _preflight_revision(c, run_id)
    r = c.post(f"/api/v1/runs/{run_id}/publish/token", json={"revision": rev, "checked": True, "title": "错误标题"}, headers=post_headers(c))
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"


def test_publish_permission_false_rejected_403_not_422(monkeypatch, tmp_path):
    home = str(tmp_path)
    run_id = "20261026-100000-abcdef"
    make_publishable_run(home, run_id, publish=False)
    adapter = FakeWeChatAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    rev = _preflight_revision(c, run_id)
    r = c.post(f"/api/v1/runs/{run_id}/publish/token", json={"revision": rev, "checked": True, "title": "发布标题"}, headers=post_headers(c))
    assert r.status_code == 403
    assert r.json()["code"] == "PUBLISH_DISABLED"


def test_publish_token_revision_mismatch_conflict(monkeypatch, tmp_path):
    home = str(tmp_path)
    run_id = "20261026-100000-abcdef"
    make_publishable_run(home, run_id)
    adapter = FakeWeChatAdapter(home)
    c = make_client(monkeypatch, home, adapter)
    c.put(f"/api/v1/runs/{run_id}/publish/permission", json={"allow": True}, headers=post_headers(c))
    r = c.post(f"/api/v1/runs/{run_id}/publish/token", json={"revision": "sha256:deadbeef", "checked": True, "title": "发布标题"}, headers=post_headers(c))
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"


# ---------- 密钥不回显 ----------

def test_config_secret_never_echoed(monkeypatch, tmp_path):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    # 写密钥
    r = c.put("/api/v1/config", json={"wechat": {"secret": "SECRET-PLAIN-1234567890", "appid": "wx123"}}, headers=post_headers(c))
    assert r.status_code == 200
    # 读投影
    r = c.get("/api/v1/config")
    body = r.json()
    assert body["wechat"]["secret_configured"] is True
    assert "SECRET-PLAIN" not in json.dumps(body)
    assert "1234567890" not in json.dumps(body)
    # 磁盘 0600 且含明文（只写不读，磁盘存明文本身是设计允许的）
    mode = os.stat(os.path.join(home, "config.yaml")).st_mode & 0o777
    assert mode == 0o600


def test_config_env_managed_whole_package_fails(monkeypatch, tmp_path):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    monkeypatch.setenv("WECHAT_SECRET", "env-secret")
    r = c.put("/api/v1/config", json={"wechat": {"secret": "new", "appid": "wx-new"}}, headers=post_headers(c))
    assert r.status_code == 409
    assert r.json()["code"] == "ENV_MANAGED"
    # 整包失败：appid 也不得提交
    r = c.get("/api/v1/config")
    assert r.json()["wechat"]["appid"] == ""


def test_config_clear_requires_confirm(monkeypatch, tmp_path):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    c.put("/api/v1/config", json={"wechat": {"secret": "SECRET-VALUE"}}, headers=post_headers(c))
    r = c.put("/api/v1/config", json={"clear": ["wechat.secret"]}, headers=post_headers(c))
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
    # 二次确认后才清除
    r = c.put("/api/v1/config", json={"clear": ["wechat.secret"], "clear_confirmed": True}, headers=post_headers(c))
    assert r.status_code == 200
    assert c.get("/api/v1/config").json()["wechat"]["secret_configured"] is False


# ---------- 路径穿越 ----------

def test_config_unknown_field_rejected(monkeypatch, tmp_path):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put("/api/v1/config", json={"wechat": {"secret_path": "../../etc"}}, headers=post_headers(c))
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert r.json()["details"]["field"] == "wechat.secret_path"
    # 未知字段整包拒绝，config.yaml 不得产生任何写入
    assert not os.path.exists(os.path.join(home, "config.yaml"))


def test_config_unknown_field_rejects_whole_package_no_partial_write(monkeypatch, tmp_path):
    """同包里的合法字段与未知字段混在一起时，整包 422 且合法字段也不落盘。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put(
        "/api/v1/config",
        json={"permissions": {"publish": True}, "wechat": {"appid": "wx1", "bogus": 1}},
        headers=post_headers(c),
    )
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
    # permissions 是未知顶层键 → 显式拒绝；wechat.bogus 是未知嵌套键
    assert r.json()["details"]["field"] in {"permissions", "wechat.bogus"}
    assert not os.path.exists(os.path.join(home, "config.yaml"))


def test_config_permissions_rejected(monkeypatch, tmp_path):
    """permissions 显式拒绝：不允许通过全局配置开启任何 run 的发布权限。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put("/api/v1/config", json={"permissions": {"publish": True}}, headers=post_headers(c))
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert r.json()["details"]["field"] == "permissions"
    assert not os.path.exists(os.path.join(home, "config.yaml"))


def test_style_unknown_field_rejected(monkeypatch, tmp_path):
    """PUT /style 同源缺陷：secret / bogus 等未知键一律 422，不落盘。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put(
        "/api/v1/style",
        json={"name": "x", "secret": "LEAK", "bogus": 1},
        headers=post_headers(c),
    )
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert r.json()["details"]["field"] in {"secret", "bogus"}
    assert not os.path.exists(os.path.join(home, "style.yaml"))


def test_publish_permission_bad_run_id(monkeypatch, tmp_path):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put("/api/v1/runs/../../etc/publish/permission", json={"allow": True}, headers=post_headers(c))
    assert r.status_code in {400, 403, 404}


# ---------- 对账 ----------

def test_publish_reconcile_without_media_id_manual_review(monkeypatch, tmp_path):
    home = str(tmp_path)
    run_id = "20261026-100000-abcdef"
    make_publishable_run(home, run_id)
    c = make_client(monkeypatch, home)
    r = c.post(f"/api/v1/runs/{run_id}/publish/reconcile", json={}, headers=post_headers(c))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "manual_review"
    assert "人工" in body["note"]


def test_config_writer_now_writable(monkeypatch, tmp_path):
    """JEE-1946：writer 从只读放开为可写白名单；改 model 保存后回读一致。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put(
        "/api/v1/config",
        json={"writer": {"provider": "openai", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"}},
        headers=post_headers(c),
    )
    assert r.status_code == 200
    body = c.get("/api/v1/config").json()
    assert body["writer"]["provider"] == "openai"
    assert body["writer"]["model"] == "gpt-4o"
    assert body["writer"]["base_url"] == "https://api.openai.com/v1"


def test_config_writer_custom_provider_roundtrip(monkeypatch, tmp_path):
    """验收第 3 条：选 custom 填自建网关地址与模型名，保存后回读一致。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put(
        "/api/v1/config",
        json={"writer": {"provider": "custom", "model": "my-model", "base_url": "https://gw.example.com/v1"}},
        headers=post_headers(c),
    )
    assert r.status_code == 200
    body = c.get("/api/v1/config").json()
    assert body["writer"]["provider"] == "custom"
    assert body["writer"]["model"] == "my-model"
    assert body["writer"]["base_url"] == "https://gw.example.com/v1"


def test_config_writer_api_key_never_echoed(monkeypatch, tmp_path):
    """writer.api_key 只写不读：响应与回读均无明文、无末四位，仅 api_key_configured。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    secret = "SK-WRITER-PLAIN-9876543210"
    r = c.put(
        "/api/v1/config",
        json={"writer": {"api_key": secret, "model": "my-model"}},
        headers=post_headers(c),
    )
    assert r.status_code == 200
    assert secret not in json.dumps(r.json())
    assert "9876543210" not in json.dumps(r.json())

    body = c.get("/api/v1/config").json()
    assert body["writer"]["api_key_configured"] is True
    assert "api_key" not in body["writer"]
    assert secret not in json.dumps(body)
    assert "9876543210" not in json.dumps(body)


def test_config_writer_env_managed_409(monkeypatch, tmp_path):
    """writer 字段被环境变量接管时写入返回 409 ENV_MANAGED，整包不局部提交。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    monkeypatch.setenv("WEWRITE_WRITER_MODEL", "env-model")
    r = c.put(
        "/api/v1/config",
        json={"writer": {"model": "web-model", "provider": "openai"}},
        headers=post_headers(c),
    )
    assert r.status_code == 409
    assert r.json()["code"] == "ENV_MANAGED"
    # env_managed 错误体沿用 image 既有结构（接管字段与变量名随 message 返回）
    assert r.json()["message"]["field"] == "writer.model"
    assert r.json()["message"]["env"] == "WEWRITE_WRITER_MODEL"
    # 整包失败：provider 也不得提交
    assert c.get("/api/v1/config").json()["writer"]["provider"] == "deepseek"


def test_config_writer_temperature_max_tokens_validation(monkeypatch, tmp_path):
    """temperature / max_tokens 数值校验：非法值整包 422，合法值回读一致。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    # max_tokens 非正整数拒绝
    r = c.put("/api/v1/config", json={"writer": {"max_tokens": -1}}, headers=post_headers(c))
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"
    # 合法值保存回读
    r = c.put(
        "/api/v1/config",
        json={"writer": {"temperature": 0.7, "max_tokens": 2048}},
        headers=post_headers(c),
    )
    assert r.status_code == 200
    body = c.get("/api/v1/config").json()
    assert body["writer"]["temperature"] == 0.7
    assert body["writer"]["max_tokens"] == 2048


def test_config_writer_temperature_boundaries(monkeypatch, tmp_path):
    """temperature 边界：0/2 通过，-1/2.1/"Infinity"/"NaN" 均 422 且不落盘。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)

    # 边界通过
    for ok_val in (0, 2):
        r = c.put("/api/v1/config", json={"writer": {"temperature": ok_val}}, headers=post_headers(c))
        assert r.status_code == 200, f"temperature={ok_val} 应通过"

    # 越界 / 非有限值拒绝
    for bad in (-1, 2.1, "Infinity", "NaN", "-Infinity"):
        r = c.put("/api/v1/config", json={"writer": {"temperature": bad}}, headers=post_headers(c))
        assert r.status_code == 422, f"temperature={bad} 应 422"
        assert r.json()["code"] == "VALIDATION_ERROR"
    # 失败后 config.yaml 未被改写（仍保持上一次合法保存的 temperature=2）
    assert c.get("/api/v1/config").json()["writer"]["temperature"] == 2


def test_config_writer_max_tokens_boundaries(monkeypatch, tmp_path):
    """max_tokens 边界：1/4000 通过，0/-5/1.5/"Infinity"/"NaN"/"1e999" 均 422 不落盘。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)

    for ok_val in (1, 4000):
        r = c.put("/api/v1/config", json={"writer": {"max_tokens": ok_val}}, headers=post_headers(c))
        assert r.status_code == 200, f"max_tokens={ok_val} 应通过"

    for bad in (0, -5, 1.5, "Infinity", "NaN", "1e999"):
        r = c.put("/api/v1/config", json={"writer": {"max_tokens": bad}}, headers=post_headers(c))
        assert r.status_code == 422, f"max_tokens={bad!r} 应 422"
        assert r.json()["code"] == "VALIDATION_ERROR"
    assert c.get("/api/v1/config").json()["writer"]["max_tokens"] == 4000


def test_config_writer_api_key_env_only_configured(monkeypatch, tmp_path):
    """env-only：只设 WEWRITE_WRITER_API_KEY、config.yaml 无 key → api_key_configured True。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    monkeypatch.setenv("WEWRITE_WRITER_API_KEY", "sk-env-only")
    body = c.get("/api/v1/config").json()
    assert body["writer"]["api_key_configured"] is True
    # 投影/响应无明文、无末四位
    assert "sk-env-only" not in json.dumps(body)
    assert "api_key" not in body["writer"]
    # env_managed 标记保持（字段仍标为 env 接管）
    managed = {m["field"]: m["env"] for m in body["env_managed"]}
    assert managed.get("writer.api_key") == "WEWRITE_WRITER_API_KEY"


def test_config_writer_api_key_env_empty_string_wins_over_file(monkeypatch, tmp_path):
    """空串 env 接管：文件里有密钥 + WEWRITE_WRITER_API_KEY="" → api_key_configured False。

    投影必须与 worker `_writer_env()` 一致：env 存在即接管（空串表示未设置），
    不得回落到文件密钥，否则页面提示与真实运行结果相反。
    """
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    # 先写文件密钥（投影曾为 true）
    c.put("/api/v1/config", json={"writer": {"api_key": "SK-FILE-SECRET"}}, headers=post_headers(c))
    assert c.get("/api/v1/config").json()["writer"]["api_key_configured"] is True

    monkeypatch.setenv("WEWRITE_WRITER_API_KEY", "")
    body = c.get("/api/v1/config").json()
    assert body["writer"]["api_key_configured"] is False
    # 投影与 worker _writer_env() 注入结果一致：空串 env → worker 不注入该字段
    from app.worker.executor import _writer_env

    injected = _writer_env({"writer": {"api_key": "SK-FILE-SECRET"}})
    assert "WEWRITE_WRITER_API_KEY" not in injected
    # env_managed 仍列出（存在即接管）
    managed = {m["field"]: m["env"] for m in body["env_managed"]}
    assert managed.get("writer.api_key") == "WEWRITE_WRITER_API_KEY"


def test_config_writer_scalar_env_empty_string_wins_over_file(monkeypatch, tmp_path):
    """model 等标量字段同口径：WEWRITE_WRITER_MODEL="" + 文件 model=file-model → 投影为空串。

    与 worker `_writer_env()` 一致：env 存在即以其为准（空串表示未设置），
    不回落到文件值；worker 也不注入该字段（子进程读到空串，走上游默认）。
    """
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    c.put("/api/v1/config", json={"writer": {"model": "file-model"}}, headers=post_headers(c))
    assert c.get("/api/v1/config").json()["writer"]["model"] == "file-model"

    monkeypatch.setenv("WEWRITE_WRITER_MODEL", "")
    body = c.get("/api/v1/config").json()
    assert body["writer"]["model"] == ""
    # worker 也不注入该字段（env 存在即接管）
    from app.worker.executor import _writer_env

    injected = _writer_env({"writer": {"model": "file-model"}})
    assert "WEWRITE_WRITER_MODEL" not in injected


def test_publish_reconcile_text_only_consistent(monkeypatch, tmp_path):
    """有 media_id 时只读 get_draft，双方归一化后比较文本 hash（仅文本核对）。"""
    home = str(tmp_path)
    run_id = "20261026-100000-abcdef"
    make_publishable_run(home, run_id)

    from app.domain import preflight as preflight_mod

    proj = preflight_mod.content_projection(home, run_id)
    from wewrite.toolkit.publisher import html_to_plaintext
    local_text = html_to_plaintext(proj["body_html"])

    class Adapter:
        def set_publish_permission(self, allow, run_id):
            st = yaml.safe_load(open(os.path.join(home, "runs", run_id, "state.yaml"), encoding="utf-8"))
            st.setdefault("permissions", {})["publish"] = allow
            write_state(home, run_id, st)
            return st

        def get_draft(self, media_id):
            # 远端正文与本地一致
            return f"<p>{local_text}</p>"

    c = make_client(monkeypatch, home, Adapter())
    r = c.post(f"/api/v1/runs/{run_id}/publish/reconcile", json={"media_id": "media-x"}, headers=post_headers(c))
    assert r.status_code == 200
    body = r.json()
    assert body["consistent"] is True
    assert "仅文本核对" in body["note"]
