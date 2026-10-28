"""图片供应商元数据读取：从上游 ``wewrite.toolkit.image_gen.PROVIDERS`` 动态反射。

P8 配置页的 provider 下拉依赖这份元数据（名称、默认 model/base_url、额外必填参数）。
平台侧不硬编码 provider 表——上游升级 provider 或改默认值时，这里通过
``inspect.signature(cls.__init__)`` 自动跟随：

- ``model`` / ``base_url`` 取 ``__init__`` 形参默认值；
- ``api_key`` 之外的其它形参（含带默认值的）视为该 provider 的 ``extra_fields``，
  用于前端提示「还需填写哪些参数」，避免用户误以为只填 api_key 就能跑。

若上游结构变化导致读取失败，抛 ``UPSTREAM_ERROR``（路由层转 502），不抛 500 栈。
"""

from __future__ import annotations

import inspect

from app.domain import errors

# 不视为「额外参数」的公共字段（api_key 通用必填，model/base_url 单独映射）
_COMMON_FIELDS = {"self", "api_key", "model", "base_url", "_kw"}


def list_image_providers() -> list[dict]:
    """返回内置 provider 元数据列表：``[{name, default_model, default_base_url, extra_fields}]``。

    顺序跟随 ``PROVIDERS`` 字典插入序（与上游一致）。
    """
    try:
        from wewrite.toolkit.image_gen import PROVIDERS
    except Exception as exc:
        raise errors.upstream_error(
            "wewrite 图片供应商元数据不可用", {"reason": "import_failed"}
        ) from exc

    providers: list[dict] = []
    try:
        for name, cls in PROVIDERS.items():
            providers.append(_provider_meta(name, cls))
    except Exception as exc:
        raise errors.upstream_error(
            "解析图片供应商元数据失败", {"reason": "provider_parse_failed"}
        ) from exc

    if not providers:
        raise errors.upstream_error(
            "上游未返回任何图片供应商", {"reason": "no_providers"}
        )
    return providers


def _provider_meta(name: str, cls: type) -> dict:
    """反射单个 provider 类的 ``__init__`` 签名，取默认 model/base_url 与额外参数。"""
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError) as exc:
        raise errors.upstream_error(
            "图片供应商签名无法解析", {"reason": "signature_unavailable", "provider": name}
        ) from exc

    params = sig.parameters
    default_model = ""
    default_base_url = ""
    if "model" in params and params["model"].default is not inspect.Parameter.empty:
        default_model = params["model"].default
    if "base_url" in params and params["base_url"].default is not inspect.Parameter.empty:
        default_base_url = params["base_url"].default

    extra_fields: list[str] = []
    for pname, param in params.items():
        if pname in _COMMON_FIELDS:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        # 非通用字段都视为该 provider 特有参数（有无默认值都提示）
        extra_fields.append(pname)

    return {
        "name": name,
        "default_model": default_model,
        "default_base_url": default_base_url,
        "extra_fields": extra_fields,
    }
