"""写作模型适配器：为「智能生成风格」调用写作大模型。

按 JEE-1947：复用 ``WEWRITE_WRITER_*`` 配置（与上游 ``llm-write`` 同一套环境变量，
不新增配置面）。模型未配置（缺 ``WEWRITE_WRITER_API_KEY``）时返回
``503 WRITER_NOT_CONFIGURED`` 并给中文提示，不得抛 500、不得返回空草稿；调用失败
返回 ``502 UPSTREAM_ERROR``。

调用协议为 OpenAI 兼容的 ``/chat/completions``（DeepSeek / OpenAI 同构），与上游
``wewrite.commands.llm_write.call_writer`` 一致；此处要求模型返回整份 ``style.yaml``
文本，所以单独走超时更短的 30s，不依赖上游子进程的 180s 长超时。
"""

from __future__ import annotations

import os

from app.domain import errors

# 与 config_store.WRITER_ENV_VARS 一致的环境变量（写作模型配置唯一来源）
_WRITER_API_KEY = "WEWRITE_WRITER_API_KEY"
_WRITER_PROVIDER = "WEWRITE_WRITER_PROVIDER"
_WRITER_BASE_URL = "WEWRITE_WRITER_BASE_URL"
_WRITER_MODEL = "WEWRITE_WRITER_MODEL"
_WRITER_TEMPERATURE = "WEWRITE_WRITER_TEMPERATURE"
_WRITER_MAX_TOKENS = "WEWRITE_WRITER_MAX_TOKENS"

# 生成端点调用超时（秒），按 issue 建议 30s。
_REQUEST_TIMEOUT = 30

# 生成风格时固定系统提示：只回整份 style.yaml 文本。
_STYLE_SYSTEM_PROMPT = (
    "你是公众号写作风格助手。根据用户给出的行业/受众/调性，输出一份完整的 style.yaml 内容。"
    "只输出 YAML 文本，不要任何前后说明，不要用代码块包裹。"
    "字段固定为：name(风格名称)、industry(行业)、target_audience(目标受众)、"
    "topics(话题数组)、tone(语气)、voice(声音)、word_count(字数，可为整数或如 1800-2600 的区间字符串)、"
    "content_style(内容风格)、writing_persona(写作人格)、theme(排版主题)、"
    "blacklist(对象，含 words 与 topics 两个数组)、reference_accounts(参考账号数组)、"
    "cover_style(封面风格)、author(署名作者)。不知道的字段可留空，不要编造。"
)


def writer_configured() -> bool:
    """写作模型是否已配置（以 WEWRITE_WRITER_API_KEY 是否存在为准）。"""
    return bool(os.environ.get(_WRITER_API_KEY, "").strip())


def _writer_config() -> dict:
    """读取写作模型配置；未配置抛 503 WRITER_NOT_CONFIGURED。"""
    key = os.environ.get(_WRITER_API_KEY, "").strip()
    if not key:
        raise errors.writer_not_configured()
    try:
        temperature = float(os.environ.get(_WRITER_TEMPERATURE, "1.0"))
    except (TypeError, ValueError):
        temperature = 1.0
    try:
        max_tokens = int(os.environ.get(_WRITER_MAX_TOKENS, "4000"))
    except (TypeError, ValueError):
        max_tokens = 4000
    return {
        "provider": os.environ.get(_WRITER_PROVIDER, "deepseek").strip() or "deepseek",
        "key": key,
        "base_url": os.environ.get(_WRITER_BASE_URL, "https://api.deepseek.com").rstrip("/"),
        "model": os.environ.get(_WRITER_MODEL, "deepseek-chat").strip() or "deepseek-chat",
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


def _strip_code_fence(text: str) -> str:
    """容错去除模型返回的代码围栏。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()
    return cleaned


class StyleGenerateBackend:
    """风格生成后端（可注入 fake）。``generate(industry, audience, tone_hint)`` 返回模型原始文本。"""

    def __init__(self, timeout: int = _REQUEST_TIMEOUT):
        self.timeout = timeout

    def generate(self, industry: str, audience: str, tone_hint: str) -> str:
        """调用写作模型生成 style.yaml 的原始文本。

        失败语义：未配置 → ``WRITER_NOT_CONFIGURED``(503)；出网失败 / 响应异常 →
        ``UPSTREAM_ERROR``(502)。
        """
        cfg = _writer_config()  # 未配置在此抛 503
        user_prompt = (
            f"行业/领域：{industry or '未指定'}\n"
            f"写给谁看：{audience or '未指定'}\n"
            f"希望调性：{tone_hint or '未指定'}"
        )
        try:
            import requests

            resp = requests.post(
                cfg["base_url"] + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg['key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg["model"],
                    "messages": [
                        {"role": "system", "content": _STYLE_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": cfg["temperature"],
                    "max_tokens": cfg["max_tokens"],
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except errors.ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - 出网/结构异常统一 502
            raise errors.upstream_error(
                "写作模型调用失败", {"reason": "writer_call_failed"}
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise errors.upstream_error(
                "写作模型返回空内容", {"reason": "empty_response"}
            )
        return _strip_code_fence(content)
