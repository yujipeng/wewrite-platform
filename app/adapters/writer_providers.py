"""写作模型供应商元数据：内置 OpenAI 兼容端点的名称/默认 model/base_url。

P8「写作模型（文章生成）」区块的 provider 下拉依赖这份元数据，交互与图片供应商
对齐（下拉 + 联动默认值 + 自定义）。写作模型走 OpenAI 兼容协议（llm-write 固定打
``{base_url}/chat/completions``），因此这里的默认值取各厂商公开的 OpenAI 兼容端点，
集中在一处方便以后加厂商，前端不硬编码、不随页面散落。

``custom`` 不是真实厂商，前端作为「自定义…」选项渲染：选中后清空 model/base_url
供用户自由填写。
"""

from __future__ import annotations

# 内置写作供应商清单（插入序即前端下拉顺序）。
WRITER_PROVIDERS: list[dict] = [
    {"name": "deepseek", "default_model": "deepseek-chat", "default_base_url": "https://api.deepseek.com"},
    {"name": "openai", "default_model": "gpt-4o", "default_base_url": "https://api.openai.com/v1"},
    {"name": "moonshot", "default_model": "moonshot-v1-8k", "default_base_url": "https://api.moonshot.cn/v1"},
    {"name": "dashscope", "default_model": "qwen-plus", "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
]


def list_writer_providers() -> list[dict]:
    """返回内置写作供应商元数据：``[{name, default_model, default_base_url}]``。"""
    return [dict(p) for p in WRITER_PROVIDERS]
