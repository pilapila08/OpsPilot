"""Sanitized failures for provider-neutral structured model calls."""

from __future__ import annotations

from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.llm.models import ModelUsage


class ModelGatewayError(RuntimeError):
    """A stable model failure safe for traces and API responses."""

    code: ErrorCode = ErrorCode.EXTERNAL_SERVICE_ERROR

    def __init__(
        self,
        safe_message: str,
        *,
        usage: ModelUsage | None = None,
        latency_ms: int | None = None,
        model_version: str | None = None,
    ) -> None:
        normalized = safe_message.strip() or "model call failed"
        self.safe_message = normalized[:500]
        self.usage = usage
        self.latency_ms = latency_ms
        self.model_version = model_version
        super().__init__(self.safe_message)

    def to_error_info(self, *, retryable: bool | None = None) -> ErrorInfo:
        return ErrorInfo.from_code(
            self.code,
            self.safe_message,
            retryable=retryable,
        )


class ModelConfigurationError(ModelGatewayError):
    code = ErrorCode.INVALID_ARGUMENT


class ModelRateLimitError(ModelGatewayError):
    code = ErrorCode.LLM_RATE_LIMIT


class ModelTimeoutError(ModelGatewayError):
    code = ErrorCode.LLM_TIMEOUT


class ModelContextError(ModelGatewayError):
    code = ErrorCode.CONTEXT_TOO_LONG


class ModelSchemaError(ModelGatewayError):
    code = ErrorCode.SCHEMA_VALIDATION


class ModelExternalError(ModelGatewayError):
    code = ErrorCode.EXTERNAL_SERVICE_ERROR


class ModelPermissionError(ModelGatewayError):
    code = ErrorCode.PERMISSION_DENIED


class ModelBudgetError(ModelGatewayError):
    code = ErrorCode.BUDGET_EXCEEDED


class RouterScopeError(ModelGatewayError):
    code = ErrorCode.INVALID_ARGUMENT
