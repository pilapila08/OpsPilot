"""Sanitized failures raised by the Kubernetes integration boundary."""

from __future__ import annotations

from opspilot.errors import ErrorCode


class KubernetesIntegrationError(RuntimeError):
    """Base class carrying a stable OpsPilot error code."""

    code: ErrorCode = ErrorCode.TOOL_EXECUTION_FAILED


class KubernetesConfigurationError(KubernetesIntegrationError):
    code = ErrorCode.INVALID_ARGUMENT


class KubernetesNotFoundError(KubernetesIntegrationError):
    code = ErrorCode.INVALID_ARGUMENT


class KubernetesAmbiguousTargetError(KubernetesIntegrationError):
    code = ErrorCode.INVALID_ARGUMENT


class KubernetesPermissionError(KubernetesIntegrationError):
    code = ErrorCode.PERMISSION_DENIED


class KubernetesTimeoutError(KubernetesIntegrationError):
    code = ErrorCode.TOOL_TIMEOUT


class KubernetesTransientError(KubernetesIntegrationError):
    code = ErrorCode.EXTERNAL_SERVICE_ERROR


class KubernetesDataError(KubernetesIntegrationError):
    code = ErrorCode.TOOL_EXECUTION_FAILED
