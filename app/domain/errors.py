"""统一错误码与异常类型。

设计基线（docs/s2-technical-design.md 第 3 节）约定的稳定错误码，M1 只启用
RUN_ID_INVALID / PATH_FORBIDDEN / NOT_FOUND / VALIDATION_ERROR / CSRF_INVALID，
其余错误码在后续里程碑按需启用。
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
