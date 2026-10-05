"""领域错误类型，携带稳定的错误码与 HTTP 状态。"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """业务规则冲突或数据缺失时抛出，API 层据此生成统一错误响应。"""

    code = "DOMAIN_ERROR"
    http_status = 422

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}


class ValidationError(DomainError):
    code = "VALIDATION"
    http_status = 400


class ForbiddenError(DomainError):
    code = "FORBIDDEN"
    http_status = 403


class NotFoundError(DomainError):
    code = "NOT_FOUND"
    http_status = 404


class ConflictError(DomainError):
    code = "CONFLICT"
    http_status = 409
