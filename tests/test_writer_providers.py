"""P8 写作模型供应商元数据端点测试（JEE-1946）。

- ``GET /api/v1/writer-providers`` 返回内置写作供应商（deepseek/openai/moonshot/
  dashscope）的名称、默认 model/base_url，与任务给定默认值一致。
"""

from __future__ import annotations

from test_publish import make_client

# 独立于实现的断言基准（任务验收表给定的各厂商默认值）。
EXPECTED = {
    "deepseek": ("deepseek-chat", "https://api.deepseek.com"),
    "openai": ("gpt-4o", "https://api.openai.com/v1"),
    "moonshot": ("moonshot-v1-8k", "https://api.moonshot.cn/v1"),
    "dashscope": ("qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
}


def test_writer_providers_returns_defaults(monkeypatch, tmp_path):
    home = str(tmp_path)
    c = make_client(monkeypatch, home)
    r = c.get("/api/v1/writer-providers")
    assert r.status_code == 200
    body = r.json()
    assert body["supports_custom"] is True
    providers = body["providers"]
    assert len(providers) == 4
    assert {p["name"] for p in providers} == set(EXPECTED.keys())
    for p in providers:
        model, base_url = EXPECTED[p["name"]]
        assert p["default_model"] == model
        assert p["default_base_url"] == base_url
