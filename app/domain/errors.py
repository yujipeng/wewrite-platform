"""统一错误码与异常类型。

设计基线（docs/s2-technical-design.md 第 3 节）约定的稳定错误码。M1 启用
RUN_ID_INVALID / PATH_FORBIDDEN / NOT_FOUND / VALIDATION_ERROR / CSRF_INVALID；
M2 增加 UPSTREAM_ERROR（热榜抓取失败）。其余错误码在后续里程碑按需启用。
"""

from __future__ import annotations


class ApiError(Exception):
    """携带稳定错误码的业务异常，路由层捕获后转成统一 JSON 响应。"""

    def __init__(self, code: str, message: str, status_code: int, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def body(self) -> dict:
        data: dict = {"code": self.code, "message": self.message}
        if self.details:
            data["details"] = self.details
        return data


def run_id_invalid(details: dict | None = None) -> ApiError:
    return ApiError("RUN_ID_INVALID", "无效的 run_id", 400, details)


def path_forbidden(details: dict | None = None) -> ApiError:
    return ApiError("PATH_FORBIDDEN", "路径访问被拒绝", 403, details)


def not_found(details: dict | None = None) -> ApiError:
    return ApiError("NOT_FOUND", "资源不存在", 404, details)


def csrf_invalid(details: dict | None = None) -> ApiError:
    return ApiError("CSRF_INVALID", "跨站请求被拒绝", 403, details)


def validation_error(message: str = "请求参数不合法", details: dict | None = None) -> ApiError:
    return ApiError("VALIDATION_ERROR", message, 422, details)


def upstream_error(message: str = "上游服务调用失败", details: dict | None = None) -> ApiError:
    return ApiError("UPSTREAM_ERROR", message, 502, details)


def conflict(message: str = "资源状态冲突", details: dict | None = None) -> ApiError:
    return ApiError("CONFLICT", message, 409, details)


def candidate_not_actionable(
    reason: str, message: str = "候选当前不可操作", details: dict | None = None
) -> ApiError:
    d = dict(details or {})
    d.setdefault("reason", reason)
    return ApiError("CANDIDATE_NOT_ACTIONABLE", message, 409, d)


def job_not_retractable(message: str = "该任务当前不可撤回", details: dict | None = None) -> ApiError:
    return ApiError("JOB_NOT_RETRACTABLE", message, 409, details)


def publish_disabled(message: str = "发布模式已禁用", details: dict | None = None) -> ApiError:
    return ApiError("PUBLISH_DISABLED", message, 403, details)
