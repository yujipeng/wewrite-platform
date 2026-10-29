"""JEE-1955 写作模型配置「env 优先 → config.yaml 兜底」修复测试。

覆盖验收标准第 1/2/3/4/5/6 条：
- 1 env 未配 + config 未配 → 503 WRITER_NOT_CONFIGURED + 中文可操作提示；
- 2 仅 config.yaml 有 api_key → 不再报未配置，实际发起 /chat/completions 请求；
- 3 仅 config 有 base_url/model → 请求打到 config 值；
- 4 env 优先 → 使用 env 值（config 不越权）；
- 5 全字段合并 → key 用 env、model/base_url 用 config；
- 6 密钥不回显 → GET /config 只给 api_key_configured 布尔。
"""

from __future__ import annotations

import json
import os

import yaml

from app.domain import errors


def _write_config(home, writer: dict) -> None:
    """写 config.yaml 的 writer 段（模拟网页保存后的落盘结果）。"""
    from app.storage.config_store import ConfigStore

    store = ConfigStore(home)
    cfg = store.load_config()
    cfg["writer"].update(writer)
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def _clear_writer_env(monkeypatch) -> None:
    for var in (
        "WEWRITE_WRITER_API_KEY",
        "WEWRITE_WRITER_PROVIDER",
        "WEWRITE_WRITER_BASE_URL",
        "WEWRITE_WRITER_MODEL",
        "WEWRITE_WRITER_TEMPERATURE",
        "WEWRITE_WRITER_MAX_TOKENS",
    ):
        monkeypatch.delenv(var, raising=False)


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload or {"choices": [{"message": {"content": "name: 测试风格\n"}}]}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _capture_request(monkeypatch, payload=None):
    """替换 requests.post，捕获调用参数并返回 OpenAI 兼容响应。"""
    import requests

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["json"] = kwargs.get("json")
        captured["timeout"] = kwargs.get("timeout")
        return _FakeResp(payload)

    monkeypatch.setattr(requests, "post", fake_post)
    return captured


def _generate(monkeypatch, home):
    from app.adapters.writer import StyleGenerateBackend

    monkeypatch.setenv("WEWRITE_HOME", home)
    return StyleGenerateBackend().generate("科技", "工程师", "专业")


# ---------- 验收第 1 条：未配置 503 ----------

def test_writer_not_configured_when_neither_env_nor_config(monkeypatch, tmp_path):
    _clear_writer_env(monkeypatch)
    home = str(tmp_path)
    monkeypatch.setenv("WEWRITE_HOME", home)

    from app.adapters import writer as writer_adapter

    assert writer_adapter.writer_configured() is False
    try:
        writer_adapter.StyleGenerateBackend().generate("科技", "工程师", "专业")
    except errors.ApiError as exc:
        assert exc.code == "WRITER_NOT_CONFIGURED"
        assert exc.status_code == 503
        assert "写作模型" in exc.message
        assert "API Key" in exc.message
    else:
        raise AssertionError("应当抛 503 WRITER_NOT_CONFIGURED")


# ---------- 验收第 2 条：仅 config.yaml 有 api_key ----------

def test_writer_config_only_in_yaml_key_issues_request(monkeypatch, tmp_path):
    _clear_writer_env(monkeypatch)
    home = str(tmp_path)
    monkeypatch.setenv("WEWRITE_HOME", home)
    _write_config(home, {"api_key": "sk-from-config"})
    captured = _capture_request(monkeypatch)

    from app.adapters import writer as writer_adapter

    assert writer_adapter.writer_configured() is True
    text = _generate(monkeypatch, home)
    assert "测试风格" in text

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-from-config"
    assert captured["json"]["model"] == "deepseek-chat"
    assert captured["json"]["temperature"] == 1.0
    assert captured["json"]["max_tokens"] == 4000


# ---------- 验收第 3 条：仅 config 有 base_url/model ----------

def test_writer_config_base_url_model_from_yaml(monkeypatch, tmp_path):
    _clear_writer_env(monkeypatch)
    home = str(tmp_path)
    _write_config(home, {
        "api_key": "sk-config",
        "base_url": "https://gw.example.com/v1",
        "model": "my-model",
        "temperature": 0.7,
        "max_tokens": 2048,
    })
    captured = _capture_request(monkeypatch)

    _generate(monkeypatch, home)

    assert captured["url"] == "https://gw.example.com/v1/chat/completions"
    assert captured["json"]["model"] == "my-model"
    assert captured["json"]["temperature"] == 0.7
    assert captured["json"]["max_tokens"] == 2048


# ---------- 验收第 4 条：env 优先，config 不越权 ----------

def test_writer_config_env_wins_over_yaml(monkeypatch, tmp_path):
    _clear_writer_env(monkeypatch)
    home = str(tmp_path)
    _write_config(home, {
        "api_key": "sk-from-config",
        "base_url": "https://config.example.com/v1",
        "model": "config-model",
    })
    monkeypatch.setenv("WEWRITE_WRITER_API_KEY", "sk-from-env")
    monkeypatch.setenv("WEWRITE_WRITER_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("WEWRITE_WRITER_MODEL", "env-model")
    captured = _capture_request(monkeypatch)

    _generate(monkeypatch, home)

    assert captured["url"] == "https://env.example.com/v1/chat/completions"
    assert captured["json"]["model"] == "env-model"
    assert captured["headers"]["Authorization"] == "Bearer sk-from-env"


# ---------- 验收第 5 条：全字段合并 ----------

def test_writer_config_field_level_merge(monkeypatch, tmp_path):
    """env 只设 api_key，config 设 model/base_url → key 用 env、model/base_url 用 config。"""
    _clear_writer_env(monkeypatch)
    home = str(tmp_path)
    _write_config(home, {
        "api_key": "sk-from-config",
        "base_url": "https://config.example.com/v1",
        "model": "config-model",
        "temperature": 0.5,
        "max_tokens": 1000,
    })
    monkeypatch.setenv("WEWRITE_WRITER_API_KEY", "sk-from-env")
    captured = _capture_request(monkeypatch)

    _generate(monkeypatch, home)

    # key 用 env
    assert captured["headers"]["Authorization"] == "Bearer sk-from-env"
    # 其余字段用 config
    assert captured["url"] == "https://config.example.com/v1/chat/completions"
    assert captured["json"]["model"] == "config-model"
    assert captured["json"]["temperature"] == 0.5
    assert captured["json"]["max_tokens"] == 1000


# ---------- 验收第 6 条：密钥不回显 ----------

def test_writer_api_key_not_echoed_in_projection(monkeypatch, tmp_path):
    """填 api_key 保存后 GET /config 无明文、无末四位，仅 api_key_configured。"""
    _clear_writer_env(monkeypatch)
    home = str(tmp_path)
    _write_config(home, {"api_key": "SK-PLAIN-9876543210", "model": "m"})

    from app.storage.config_store import ConfigStore

    body = ConfigStore(home).project_config()
    assert body["writer"]["api_key_configured"] is True
    assert "api_key" not in body["writer"]
    assert "SK-PLAIN-9876543210" not in json.dumps(body)
    assert "9876543210" not in json.dumps(body)


def test_writer_effective_values_uses_upstream_defaults(monkeypatch, tmp_path):
    """两者都没有时回落上游默认值（provider/model/base_url/temperature/max_tokens）。"""
    _clear_writer_env(monkeypatch)
    home = str(tmp_path)

    from app.storage import config_store

    config = config_store.ConfigStore(home).load_config()
    merged = config_store.writer_effective_values(config)
    assert merged["provider"] == "deepseek"
    assert merged["model"] == "deepseek-chat"
    assert merged["base_url"] == "https://api.deepseek.com"
    assert merged["temperature"] == 1.0
    assert merged["max_tokens"] == 4000
    assert merged["api_key"] == ""


# ---------- API 集成：仅 config.yaml 有 api_key 时，/style/generate 走通 ----------

def test_style_generate_endpoint_uses_config_yaml_only(monkeypatch, tmp_path):
    """验收第 2 条端到端：仅 config.yaml 有 api_key，POST /style/generate 返回 200 并发起请求。"""
    _clear_writer_env(monkeypatch)
    home = str(tmp_path)
    monkeypatch.setenv("WEWRITE_HOME", home)
    monkeypatch.setenv("WEWRITE_PLATFORM_NO_WORKER", "1")
    _write_config(home, {"api_key": "sk-config", "base_url": "https://gw.example.com/v1", "model": "m1"})

    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.headers.update({"host": "127.0.0.1:8765"})
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]

    captured = _capture_request(monkeypatch)
    r = client.post(
        "/api/v1/style/generate",
        json={"industry": "科技"},
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": token,
        },
    )
    assert r.status_code == 200
    assert r.json()["source"] == "llm"
    # 请求确实发出，且打到 config 里的 base_url/model/key
    assert captured["url"] == "https://gw.example.com/v1/chat/completions"
    assert captured["json"]["model"] == "m1"
    assert captured["headers"]["Authorization"] == "Bearer sk-config"

