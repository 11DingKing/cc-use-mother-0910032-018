"""服务端统一异常。

所有异常携带稳定的 ``code``，HTTP 层据此映射状态码，业务层据此断言，
避免靠异常类名或中文消息做分支判断。
"""
from __future__ import annotations


class ServiceError(Exception):
    """所有可预期业务错误的基类。"""

    code = "service_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class NotFoundError(ServiceError):
    code = "not_found"
    http_status = 404


class ConflictError(ServiceError):
    """规则重叠、版本冲突、批次状态冲突等。"""

    code = "conflict"
    http_status = 409


class ImmutablePlanError(ServiceError):
    """对已冻结/已完成计划做禁止的修改。"""

    code = "plan_immutable"
    http_status = 409


class ValidationError(ServiceError):
    code = "validation_error"
    http_status = 422


class UnauthorizedError(ServiceError):
    code = "unauthorized"
    http_status = 401
