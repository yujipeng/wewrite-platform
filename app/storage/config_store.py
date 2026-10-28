"""P8 配置投影：``config.yaml`` / ``style.yaml`` 的读取、白名单原子写与脱敏投影。

按 docs/s2-technical-design.md 第 2.5 / 3 节：

- 密钥（wechat.secret / image.api_key / writer api_key）只写不读，投影只给
  ``*_configured`` 布尔，绝不回显明文或末四位；
- 环境变量接管字段（WECHAT_* / WEWRITE_IMAGE_* / WEWRITE_THEME）写入返回
  ``409 ENV_MANAGED``，整包失败不局部提交；
- ``writer`` 环境配置整体只读，拒绝 PUT；
- 空值表示保持原值，显式 ``clear``（仅 wechat.secret / image.api_key）需
  ``clear_confirmed=true`` 二次确认；
- 落盘走 ``safe_write_root_file``（mkstemp 0600 → fsync → os.replace → 复验权限），
  日志统一 ``redact_secret``。
"""

from __future__ import annotations

import copy
import json
import os

import yaml

from app.domain import errors
from app.storage import paths
from app.storage.redact import secret_configured

# 环境变量 → 配置投影字段（设计 2.5 / config.py 与 issue 描述一致）
ENV_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "WECHAT_APPID": ("wechat", "appid"),
    "WECHAT_SECRET": ("wechat", "secret"),
    "WECHAT_AUTHOR": ("wechat", "author"),
    "WEWRITE_IMAGE_PROVIDER": ("image", "provider"),
    "WEWRITE_IMAGE_API_KEY": ("image", "api_key"),
    "WEWRITE_IMAGE_MODEL": ("image", "model"),
    "WEWRITE_THEME": ("theme",),
}

# writer 相关环境变量（整体只读，不接受 PUT）
WRITER_ENV_VARS = (
    "WEWRITE_WRITER_API_KEY",
    "WEWRITE_WRITER_PROVIDER",
    "WEWRITE_WRITER_BASE_URL",
    "WEWRITE_WRITER_MODEL",
    "WEWRITE_WRITER_TEMPERATURE",
    "WEWRITE_WRITER_MAX_TOKENS",
)

# PUT /config 可写的白名单字段（叶字段键），secret/api_key 只写不读。
WRITABLE_CONFIG_FIELDS = frozenset(
    {
        "theme",
        "image.provider",
        "image.model",
        "image.api_key",
        "image.base_url",
        "image.max_cost",
        "image.estimated_cost_per_image",
        "wechat.appid",
        "wechat.secret",
        "wechat.author",
    }
)

# PUT /config 请求体允许的顶层键；其余（含 permissions）一律 422 未知字段。
CONFIG_TOP_KEYS = frozenset({"theme", "image", "wechat", "clear", "clear_confirmed"})
CONFIG_IMAGE_KEYS = frozenset(
    {"provider", "model", "api_key", "base_url", "max_cost", "estimated_cost_per_image"}
)
CONFIG_WECHAT_KEYS = frozenset({"appid", "secret", "author"})

CLEARABLE_FIELDS = frozenset({"wechat.secret", "image.api_key"})

DEFAULT_CONFIG: dict = {
    "version": 1,
    "theme": "professional-clean",
    "image": {
        "provider": "",
        "model": "",
        "base_url": "",
        "estimated_cost_per_image": 0,
        "max_cost": 0,
        "api_key": "",
    },
    "wechat": {
        "appid": "",
        "secret": "",
        "author": "编辑",
    },
    "writer": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_key": "",
    },
    "permissions": {"publish": False},
}

# 缺 style.yaml 时的可编辑默认模板（整文件编辑器初始内容，含中文注释）。
DEFAULT_STYLE_YAML = """# WeWrite 风格配置
# 由 WeWrite 生成 —— 直接改这个文件即可，下次写作自动生效

name: ""
industry: ""

target_audience: ""

topics: []

# 写作风格
tone: ""
voice: ""
word_count: 2000

content_style: ""
writing_persona: "midnight-friend"

# 禁忌（可写成数组，或对象 {words: [], topics: []}）
blacklist: []

reference_accounts: []

# 排版主题（写作主题不是排版主题）
theme: ""

# 封面
cover_style: ""

# 署名
author: ""
"""


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env_managed_map() -> dict[str, str]:
    """返回被环境变量接管的字段映射：``{field: env_var}``。

    接管以变量存在为准（含空串），字段名用点分路径（wechat.secret 等）。
    """
    managed: dict[str, str] = {}
    for env_var, keys in ENV_FIELD_MAP.items():
        if env_var in os.environ:
            managed[".".join(keys)] = env_var
    return managed


def _get_in(cfg: dict, dotted: str):
    cur: object = cfg
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _set_in(cfg: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    cur = cfg
    for key in keys[:-1]:
        cur = cur.setdefault(key, {})
    cur[keys[-1]] = value


class ConfigStore:
    """config.yaml / style.yaml 的读写（全部走 paths 安全层 + 全局锁）。"""

    def __init__(self, home: str):
        self.home = home

    # ---------- 读 ----------

    def load_config(self) -> dict:
        text = paths.safe_read_root_file(self.home, "config.yaml")
        if text is None:
            return copy.deepcopy(DEFAULT_CONFIG)
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            return copy.deepcopy(DEFAULT_CONFIG)
        if not isinstance(data, dict):
            return copy.deepcopy(DEFAULT_CONFIG)
        return _merge_default(data)

    def load_style(self) -> dict:
        text = paths.safe_read_root_file(self.home, "style.yaml")
        if text is None:
            return {"version": 1}
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            return {"version": 1}
        if not isinstance(data, dict):
            return {"version": 1}
        data.setdefault("version", 1)
        return data

    def project_config(self) -> dict:
        """只读配置投影：密钥只给 *_configured 布尔，附 env_managed 与权限。"""
        cfg = self.load_config()
        env_managed = env_managed_map()
        image = cfg.get("image") or {}
        wechat = cfg.get("wechat") or {}
        writer = cfg.get("writer") or {}

        def _field_value(dotted: str):
            return _get_in(cfg, dotted)

        return {
            "version": 1,
            "theme": _field_value("theme"),
            "image": {
                "provider": image.get("provider", ""),
                "model": image.get("model", ""),
                "base_url": image.get("base_url", ""),
                "estimated_cost_per_image": image.get("estimated_cost_per_image", 0),
                "max_cost": image.get("max_cost", 0),
                "api_key_configured": secret_configured(image.get("api_key") or None),
            },
            "wechat": {
                "appid": wechat.get("appid", ""),
                "appid_configured": secret_configured(wechat.get("appid") or None),
                "secret_configured": secret_configured(wechat.get("secret") or None),
                "author": wechat.get("author", "编辑"),
            },
            "writer": {
                "provider": writer.get("provider", "deepseek"),
                "model": writer.get("model", "deepseek-chat"),
                "api_key_configured": secret_configured(writer.get("api_key") or None),
                "writer_configured": bool(os.environ.get("WEWRITE_WRITER_API_KEY", "").strip()),
            },
            "permissions": {"publish": bool((cfg.get("permissions") or {}).get("publish", False))},
            "env_managed": [
                {"field": field, "env": env_var} for field, env_var in sorted(env_managed.items())
            ],
        }

    def project_style(self) -> dict:
        """非密钥 style 投影 + revision + raw（style.yaml 原文）。

        blacklist 两种形态（数组 / 对象）都原样投影，不做归一化改写；
        raw 供前端整文件编辑器「加载当前配置」使用。
        """
        style = self.load_style()
        projected = {
            k: v for k, v in style.items() if k not in {"api_key", "secret"}
        }
        projected["revision"] = "sha256:" + self._style_hash(style)
        raw = self.load_style_raw()
        projected["raw"] = raw if raw is not None else DEFAULT_STYLE_YAML
        return projected

    @staticmethod
    def _style_hash(style: dict) -> str:
        import hashlib

        payload = json.dumps(style, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---------- 写 ----------

    def put_config(self, body: dict) -> dict:
        """白名单字段原子写 config.yaml；env 接管字段 409；空值保持；clear 二次确认。"""
        managed = env_managed_map()
        # 先校验整包，任一 env 接管字段被写 → 409 ENV_MANAGED，不局部提交
        dirty = self._collect_config_dirty(body)
        for field in dirty:
            if field in managed:
                raise errors.env_managed(
                    {"field": field, "env": managed[field]}
                )

        cfg = self.load_config()
        applied: list[str] = []
        for field, value in dirty.items():
            self._apply_config_value(cfg, field, value)
            applied.append(field)

        # 显式 clear（需二次确认）
        clear_list = body.get("clear")
        if clear_list is not None:
            if not isinstance(clear_list, list):
                raise errors.validation_error("clear 必须为数组", {"field": "clear"})
            if body.get("clear_confirmed") is not True:
                raise errors.validation_error(
                    "清除密钥需 clear_confirmed=true 二次确认", {"field": "clear_confirmed"}
                )
            for field in clear_list:
                if field not in CLEARABLE_FIELDS:
                    raise errors.validation_error(
                        "只允许清除 wechat.secret | image.api_key", {"field": "clear"}
                    )
                if field in managed:
                    raise errors.env_managed({"field": field, "env": managed[field]})
                _set_in(cfg, field, "")
                applied.append(f"clear:{field}")

        paths.safe_write_root_file(
            self.home,
            "config.yaml",
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        )
        return {"applied": applied, "config": self.project_config()}

    def _collect_config_dirty(self, body: dict) -> dict[str, object]:
        """把 PUT body 归一化为 {点分字段: 值}；未知字段拒绝；writer 只读拒绝。"""
        dirty: dict[str, object] = {}
        if not isinstance(body, dict):
            raise errors.validation_error("请求体必须为 JSON 对象")

        # 顶层未知键拒绝（permissions 显式拒绝：不允许通过全局配置开启发布权限）
        for key in body:
            if key not in CONFIG_TOP_KEYS:
                raise errors.validation_error("未知字段", {"field": key})

        # writer 相关环境配置整体只读，拒绝写入（设计 2.5）
        if isinstance(body.get("writer"), dict) and body.get("writer"):
            raise errors.validation_error("writer 相关环境配置整体只读，不接受写入", {"field": "writer"})

        # 嵌套键未知拒绝（image/wechat 下白名单外的键一律 422）
        _reject_unknown_nested(body, "image", CONFIG_IMAGE_KEYS)
        _reject_unknown_nested(body, "wechat", CONFIG_WECHAT_KEYS)

        for field in WRITABLE_CONFIG_FIELDS:
            # 只处理出现在 body 里的字段；缺失不改
            present, value = _pick_dotted(body, field)
            if not present:
                continue
            if field in {"image.api_key", "wechat.secret"}:
                # 只写不读：接受非空字符串；空串表示保持原值
                if value is not None and not isinstance(value, str):
                    raise errors.validation_error("密钥字段必须为字符串", {"field": field})
                if value == "":
                    continue  # 空值保持原值
                dirty[field] = value
            elif field == "theme":
                if not isinstance(value, str) or not value:
                    raise errors.validation_error("theme 必须为非空字符串", {"field": field})
                if any(sep in value for sep in ("/", "\\", "..")):
                    raise errors.validation_error("theme 不合法", {"field": field})
                dirty[field] = value
            elif field in {"image.provider", "image.model", "image.base_url"}:
                if value is not None and not isinstance(value, str):
                    raise errors.validation_error("字段必须为字符串", {"field": field})
                if value is not None:
                    dirty[field] = value
            elif field in {"image.max_cost", "image.estimated_cost_per_image"}:
                if value is None:
                    continue
                try:
                    dirty[field] = float(value)
                except (TypeError, ValueError):
                    raise errors.validation_error("字段必须为数字", {"field": field})
            elif field in {"wechat.appid", "wechat.author"}:
                if value is not None and not isinstance(value, str):
                    raise errors.validation_error("字段必须为字符串", {"field": field})
                if value is not None:
                    dirty[field] = value
        return dirty

    def _apply_config_value(self, cfg: dict, field: str, value) -> None:
        if value is None:
            return  # 空值保持原值
        _set_in(cfg, field, value)

    # ---------- style ----------

    STYLE_FIELDS = (
        "name",
        "industry",
        "target_audience",
        "topics",
        "tone",
        "voice",
        "word_count",
        "content_style",
        "writing_persona",
        "theme",
        "blacklist",
        "reference_accounts",
        "cover_style",
        "author",
    )

    # 模型生成/整文件编辑可接受的字段白名单；blacklist 兼容数组与对象两种形态。
    STYLE_DRAFT_FIELDS = frozenset(STYLE_FIELDS)

    # YAML 整文件大小上限（字节，建议 64KB）。
    STYLE_YAML_MAX_BYTES = 64 * 1024

    # ---------- 校验 / 归一化 ----------

    def _style_field_error(self, field: str, message: str) -> errors.ApiError:
        return errors.validation_error(message, {"field": field})

    def validate_style_data(self, data: dict) -> dict:
        """校验一份 style dict（结构化 JSON 或 YAML 解析后）并返回白名单收敛结果。

        - 未知字段返回 ``warnings`` 列表（被忽略字段），不静默丢弃也不拒绝；
        - ``blacklist`` 同时接受数组与对象 ``{words, topics}``；
        - ``word_count`` 接受整数或字符串（如 ``"1800-2600"``）；
        - ``topics`` / ``reference_accounts`` 强制为字符串数组；
        - 返回 ``(style, warnings)``，style 只含白名单字段。
        """
        warnings: list[str] = []
        style: dict = {}
        for key in data:
            if key in {"version", "api_key", "secret"}:
                # 内部字段：version 由平台维护，密钥不回显；均不当作未知字段
                continue
            if key not in self.STYLE_FIELDS:
                warnings.append(key)
                continue
        for field in self.STYLE_FIELDS:
            if field not in data:
                continue
            value = data[field]
            if field in {"topics", "reference_accounts"}:
                if value is None:
                    style[field] = []
                elif isinstance(value, list):
                    style[field] = [str(v).strip() for v in value if str(v).strip()]
                elif isinstance(value, str) and value.strip():
                    style[field] = [value.strip()]
                else:
                    raise self._style_field_error(field, f"{field} 必须为数组")
            elif field == "blacklist":
                style[field] = self._normalize_blacklist(value)
            elif field == "word_count":
                style[field] = self._normalize_word_count(value)
            else:
                if value is None:
                    style[field] = ""
                elif isinstance(value, str):
                    style[field] = value
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    style[field] = str(value)
                else:
                    raise self._style_field_error(field, f"{field} 必须为字符串")
        return style, warnings

    def _normalize_blacklist(self, value):
        """blacklist 归一化：数组 → 字符串数组；对象 → ``{words, topics}``。

        对象形态只保留 words/topics 两个键，其它键忽略（不拒绝、不落盘）。
        """
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, dict):
            words = value.get("words")
            topics = value.get("topics")
            out: dict = {}
            if isinstance(words, list):
                out["words"] = [str(v).strip() for v in words if str(v).strip()]
            elif words is not None:
                raise self._style_field_error("blacklist", "blacklist.words 必须为数组")
            if isinstance(topics, list):
                out["topics"] = [str(v).strip() for v in topics if str(v).strip()]
            elif topics is not None:
                raise self._style_field_error("blacklist", "blacklist.topics 必须为数组")
            return out
        raise self._style_field_error("blacklist", "blacklist 必须为对象或数组")

    def _normalize_word_count(self, value):
        """word_count 接受整数或字符串（含区间字符串），统一保留原始语义。"""
        if value is None:
            return ""
        if isinstance(value, bool):
            raise self._style_field_error("word_count", "word_count 必须为整数或字符串")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            return value
        raise self._style_field_error("word_count", "word_count 必须为整数或字符串")

    # ---------- 读 ----------

    def load_style_raw(self) -> str | None:
        """返回 style.yaml 原始文本；缺失返回 None（安全读，路径穿越仍 403）。"""
        return paths.safe_read_root_file(self.home, "style.yaml")

    def style_yaml_or_default(self) -> str:
        """返回 style.yaml 原始文本；缺失时返回一份带注释的可编辑默认模板。"""
        raw = self.load_style_raw()
        if raw is not None:
            return raw
        return DEFAULT_STYLE_YAML

    # ---------- 写 ----------

    def put_style(self, body: dict) -> dict:
        """写 style.yaml：支持旧的结构化 JSON 与新的 ``{yaml: "<全文>"}``。

        - 结构化 JSON（顶层为 STYLE_FIELDS 白名单）保持向后兼容；
        - ``{yaml: ...}`` 走整文件解析→校验→原子写，未知字段进入 warnings；
        - 大小超限 / YAML 解析失败 / 校验失败 → 422 VALIDATION_ERROR。
        """
        if not isinstance(body, dict):
            raise errors.validation_error("请求体必须为 JSON 对象")

        if "yaml" in body:
            if not isinstance(body["yaml"], str):
                raise errors.validation_error("yaml 必须为字符串", {"field": "yaml"})
            text = body["yaml"]
            if len(text.encode("utf-8")) > self.STYLE_YAML_MAX_BYTES:
                raise errors.validation_error(
                    "style.yaml 超过 64KB 大小上限", {"field": "yaml"}
                )
            data = self.parse_style_yaml(text)
            style, warnings = self.validate_style_data(data)
            style.setdefault("version", 1)
            self._write_style_data(style)
            result = self.project_style()
            if warnings:
                result["warnings"] = warnings
            return result

        # 旧的结构化 JSON 路径：只允许白名单字段
        for key in body:
            if key not in self.STYLE_FIELDS:
                raise errors.validation_error("未知字段", {"field": key})

        style = self.load_style()
        style.setdefault("version", 1)
        for field in self.STYLE_FIELDS:
            value = body.get(field)
            if field in {"topics", "reference_accounts"}:
                if value is not None and not isinstance(value, list):
                    raise errors.validation_error(f"{field} 必须为数组", {"field": field})
                style[field] = value if value is not None else []
            elif field == "blacklist":
                if value is not None:
                    style[field] = self._normalize_blacklist(value)
                else:
                    style.setdefault(field, [])
            elif field == "word_count":
                if value is not None:
                    style[field] = self._normalize_word_count(value)
                else:
                    style.setdefault(field, 2000)
            else:
                if value is not None and not isinstance(value, str):
                    raise errors.validation_error(f"{field} 必须为字符串", {"field": field})
                if value is not None:
                    style[field] = value
                elif field not in style:
                    style[field] = ""  # 缺失字段显式保存默认值
        self._write_style_data(style)
        return self.project_style()

    def _write_style_data(self, style: dict) -> None:
        """style dict 原子写 style.yaml 0600（走 paths 安全层 + 全局锁）。"""
        paths.safe_write_root_file(
            self.home,
            "style.yaml",
            yaml.safe_dump(style, allow_unicode=True, sort_keys=False),
        )

    def parse_style_yaml(self, text: str) -> dict:
        """解析 style.yaml 文本；失败抛 422 并带行号 + 中文原因。"""
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            line = self._yaml_error_line(exc)
            detail = {"reason": "yaml_parse_error"}
            if line:
                detail["line"] = line
                message = f"第 {line} 行：YAML 语法错误"
            else:
                message = "YAML 语法错误"
            raise errors.validation_error(message, detail) from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise errors.validation_error("style.yaml 必须是 YAML 对象（顶层为键值对）", {"field": "style"})
        return data

    @staticmethod
    def _yaml_error_line(exc: yaml.YAMLError) -> int | None:
        """从 PyYAML 异常中尽力提取出错行号。"""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            return mark.line + 1
        # MarkedYAMLError 的 context 也可能带位置
        context = getattr(exc, "context_mark", None)
        if context is not None:
            return context.line + 1
        return None

    def validate_style_yaml(self, text: str) -> dict:
        """校验 style.yaml 文本（POST /style/validate）。

        返回 ``{valid, warnings, errors: [{line, field, message}]}``；解析失败
        抛 422（带行号），字段问题以 errors 列表返回，不抛异常。
        """
        if not isinstance(text, str):
            raise errors.validation_error("yaml 必须为字符串", {"field": "yaml"})
        if len(text.encode("utf-8")) > self.STYLE_YAML_MAX_BYTES:
            raise errors.validation_error("style.yaml 超过 64KB 大小上限", {"field": "yaml"})

        data = self.parse_style_yaml(text)
        try:
            style, warnings = self.validate_style_data(data)
        except errors.ApiError as exc:
            return {
                "valid": False,
                "warnings": [],
                "errors": [{"field": exc.details.get("field"), "message": exc.message}],
            }
        return {"valid": True, "warnings": warnings, "errors": [], "fields": list(style.keys())}

    def generate_style_yaml(self, body: dict, backend) -> dict:
        """生成整份 style.yaml（POST /style/generate 逻辑层）。

        校验入参（industry/audience/tone_hint 至少一项非空）→ 调写作模型 →
        服务端解析→白名单裁剪补默认→重新序列化，绝不透出模型原文。不落盘。
        """
        if not isinstance(body, dict):
            raise errors.validation_error("请求体必须为 JSON 对象")

        industry = body.get("industry")
        audience = body.get("audience")
        tone_hint = body.get("tone_hint")
        for field, value in (("industry", industry), ("audience", audience), ("tone_hint", tone_hint)):
            if value is not None and not isinstance(value, str):
                raise errors.validation_error(f"{field} 必须为字符串", {"field": field})

        def _nonempty(value) -> bool:
            return isinstance(value, str) and bool(value.strip())

        if not (_nonempty(industry) or _nonempty(audience) or _nonempty(tone_hint)):
            raise errors.validation_error(
                "industry / audience / tone_hint 至少填写一项", {"field": "industry"}
            )

        raw_text = backend.generate(industry or "", audience or "", tone_hint or "")
        data = self.parse_style_yaml(raw_text)
        style, warnings = self.validate_style_data(data)
        style.setdefault("version", 1)
        # 补齐白名单字段（缺省补默认，保证结构完整）
        for field in self.STYLE_FIELDS:
            if field not in style:
                if field in {"topics", "reference_accounts", "blacklist"}:
                    style[field] = []
                else:
                    style[field] = ""
        serialized = yaml.safe_dump(style, allow_unicode=True, sort_keys=False)
        return {"yaml": serialized, "source": "llm", "warnings": warnings}


def _pick_dotted(body: dict, dotted: str):
    """从嵌套 dict 取点分路径字段；返回 (是否出现, 值)。"""
    keys = dotted.split(".")
    cur: object = body
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _reject_unknown_nested(body: dict, parent: str, allowed: frozenset) -> None:
    """拒绝 parent 下的未知嵌套键；parent 缺失或非 dict 时由后续校验处理。

    ``parent`` 出现在 body 里且不是 dict → 由类型校验拒绝；是 dict 则任一
    不在 ``allowed`` 的键 → 422 VALIDATION_ERROR，``details.field`` 用点分路径。
    """
    if parent not in body:
        return
    node = body[parent]
    if not isinstance(node, dict):
        raise errors.validation_error(f"{parent} 必须为对象", {"field": parent})
    for key in node:
        if key not in allowed:
            raise errors.validation_error("未知字段", {"field": f"{parent}.{key}"})


def _merge_default(data: dict) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    _deep_merge(cfg, data)
    return cfg


def _deep_merge(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base
