"""P8 配置页图片供应商改造测试（JEE-1935）：

- ``GET /api/v1/image-providers`` 动态读 ``wewrite.toolkit.image_gen.PROVIDERS``，
  返回 10 项且默认值与上游一致（不硬编码 provider 表）。
- provider 保存后能回读；自定义 provider 可保存；api_key 不回显。
- 主题不在内置列表时保留（后端只读投影逻辑，配置写入主题即可回读）。
"""

from __future__ import annotations

import json

from wewrite.toolkit.image_gen import PROVIDERS

from test_publish import make_client, post_headers

# 上游实测默认值（独立于实现，作为断言基准，防止端点硬编码与上游漂移）
EXPECTED = {
    "doubao": ("doubao-seedream-5-0-260128", "https://ark.cn-beijing.volces.com/api/v3", []),
    "openai": ("dall-e-3", "https://api.openai.com/v1", []),
    "gemini": ("gemini-3.1-flash-image-preview", "https://generativelanguage.googleapis.com/v1beta", []),
    "dashscope": ("qwen-image-2.0-pro", "https://dashscope.aliyuncs.com/api/v1", []),
    "minimax": ("image-01", "https://api.minimax.io/v1", []),
    "replicate": ("google/nano-banana-pro", "https://api.replicate.com/v1", []),
    "azure_openai": ("dall-e-3", "", ["deployment"]),
    "openrouter": ("google/gemini-3.1-flash-image-preview", "https://openrouter.ai/api/v1", []),
    "jimeng": ("jimeng_t2i_v40", "https://visual.volcengineapi.com", ["secret_key"]),
    "sub2api": ("gpt-image-2", "https://relay.upthos.com", ["resolution", "poll_interval", "timeout"]),
}


def test_image_providers_returns_10_with_upstream_defaults(monkeypatch, tmp_path):
    """端点返回 10 项，名称集合与上游 PROVIDERS 一致，默认值与第四节表格一致。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.get("/api/v1/image-providers")
    assert r.status_code == 200
    body = r.json()
    assert body["supports_custom"] is True

    providers = body["providers"]
    assert len(providers) == 10
    assert {p["name"] for p in providers} == set(PROVIDERS.keys())

    for p in providers:
        model, base_url, extra = EXPECTED[p["name"]]
        assert p["default_model"] == model
        assert p["default_base_url"] == base_url
        assert p["extra_fields"] == extra


def test_image_providers_reflects_upstream_not_hardcoded(monkeypatch, tmp_path):
    """端点实现确实读 PROVIDERS：名称集合与上游字典键一一对应。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    providers = c.get("/api/v1/image-providers").json()["providers"]
    assert [p["name"] for p in providers] == list(PROVIDERS.keys())


def test_config_save_provider_roundtrip(monkeypatch, tmp_path):
    """选 gemini 保存后回读 image.provider == gemini（当前实现曾丢 provider）。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put(
        "/api/v1/config",
        json={
            "image": {
                "provider": "gemini",
                "model": "gemini-3.1-flash-image-preview",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
            }
        },
        headers=post_headers(c),
    )
    assert r.status_code == 200
    body = c.get("/api/v1/config").json()
    assert body["image"]["provider"] == "gemini"
    assert body["image"]["model"] == "gemini-3.1-flash-image-preview"
    assert body["image"]["base_url"] == "https://generativelanguage.googleapis.com/v1beta"


def test_config_save_custom_provider_roundtrip(monkeypatch, tmp_path):
    """自定义 provider（不在内置清单）三项都可保存并回读一致。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put(
        "/api/v1/config",
        json={
            "image": {
                "provider": "my-provider",
                "model": "my-model",
                "base_url": "https://x.example/v1",
            }
        },
        headers=post_headers(c),
    )
    assert r.status_code == 200
    body = c.get("/api/v1/config").json()
    assert body["image"]["provider"] == "my-provider"
    assert body["image"]["model"] == "my-model"
    assert body["image"]["base_url"] == "https://x.example/v1"


def test_config_switch_to_empty_base_url_not_residue(monkeypatch, tmp_path):
    """切换到 azure_openai（默认 base_url 为空串）保存后，base_url 不残留旧 provider 地址。

    对应前端空地址缺陷：先存 doubao 带 base_url，再以 azure_openai + base_url:"" 保存，
    回读 image.base_url 必须为 ""（后端显式空串语义），不得残留 doubao 的地址。
    """
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put(
        "/api/v1/config",
        json={
            "image": {
                "provider": "doubao",
                "model": "doubao-seedream-5-0-260128",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            }
        },
        headers=post_headers(c),
    )
    assert r.status_code == 200
    assert c.get("/api/v1/config").json()["image"]["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"

    r = c.put(
        "/api/v1/config",
        json={"image": {"provider": "azure_openai", "model": "dall-e-3", "base_url": ""}},
        headers=post_headers(c),
    )
    assert r.status_code == 200
    body = c.get("/api/v1/config").json()
    assert body["image"]["provider"] == "azure_openai"
    assert body["image"]["base_url"] == ""


def test_config_theme_not_in_list_is_preserved(monkeypatch, tmp_path):
    """存一个不在内置清单里的旧主题名，回读仍为该值（后端不校验主题枚举）。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.put("/api/v1/config", json={"theme": "legacy-old-theme"}, headers=post_headers(c))
    assert r.status_code == 200
    assert c.get("/api/v1/config").json()["theme"] == "legacy-old-theme"


def test_config_image_api_key_never_echoed(monkeypatch, tmp_path):
    """填 api_key 保存后，响应与后续 GET /config 均无明文、无末四位，仅 api_key_configured。"""
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    secret = "SK-SECRET-PLAIN-1234567890"
    r = c.put(
        "/api/v1/config",
        json={"image": {"api_key": secret, "provider": "openai"}},
        headers=post_headers(c),
    )
    assert r.status_code == 200
    # 写响应体也不得回显
    assert secret not in json.dumps(r.json())
    assert "1234567890" not in json.dumps(r.json())

    body = c.get("/api/v1/config").json()
    assert body["image"]["api_key_configured"] is True
    assert "api_key" not in body["image"]
    assert secret not in json.dumps(body)
    assert "1234567890" not in json.dumps(body)
