"""Whitelist registry and bounded single-call tool execution."""

from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from opspilot.tools.models import (
    ToolDefinition,
    ToolDescriptor,
    ToolError,
    ToolErrorCode,
    ToolInvocation,
    ToolMetadata,
    ToolResponse,
    ToolRiskLevel,
)


class DuplicateToolError(ValueError):
    """Raised when a registry already contains the requested tool name."""


class ToolNotRegisteredError(LookupError):
    """Raised by direct lookups for tools outside the whitelist."""


class ToolRegistry:
    """In-memory whitelist of validated tool definitions."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition[Any, Any]] = {}

    def register(self, definition: ToolDefinition[Any, Any]) -> None:
        if definition.name in self._tools:
            raise DuplicateToolError(
                f"tool {definition.name!r} is already registered"
            )
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition[Any, Any]:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotRegisteredError("tool is not registered") from exc

    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return tuple(
            self._tools[name].descriptor for name in sorted(self._tools.keys())
        )

    async def invoke(self, invocation: ToolInvocation) -> ToolResponse:
        """Validate and execute one Risk 0 call, returning a safe response."""

        started_at = perf_counter()
        definition = self._tools.get(invocation.tool)
        if definition is None:
            return self._failure(
                invocation=invocation,
                started_at=started_at,
                code=ToolErrorCode.TOOL_NOT_FOUND,
                message="tool is not registered",
            )

        if definition.risk_level is not ToolRiskLevel.READ_ONLY:
            return self._failure(
                invocation=invocation,
                started_at=started_at,
                code=ToolErrorCode.POLICY_REJECTED,
                message="tool is not executable without policy authorization",
                definition=definition,
            )

        try:
            validated_input = definition.input_model.model_validate(
                invocation.arguments
            )
        except ValidationError:
            return self._failure(
                invocation=invocation,
                started_at=started_at,
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="tool arguments failed schema validation",
                definition=definition,
            )

        try:
            async with asyncio.timeout(definition.timeout_seconds):
                raw_output = await definition.handler(validated_input)
        except TimeoutError:
            return self._failure(
                invocation=invocation,
                started_at=started_at,
                code=ToolErrorCode.TOOL_TIMEOUT,
                message="tool execution timed out",
                definition=definition,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._failure(
                invocation=invocation,
                started_at=started_at,
                code=ToolErrorCode.TOOL_EXECUTION_FAILED,
                message="tool execution failed",
                definition=definition,
            )

        try:
            output = definition.output_model.model_validate(raw_output)
        except ValidationError:
            return self._failure(
                invocation=invocation,
                started_at=started_at,
                code=ToolErrorCode.TOOL_OUTPUT_INVALID,
                message="tool result failed schema validation",
                definition=definition,
            )

        return ToolResponse(
            success=True,
            data=output.model_dump(mode="json"),
            metadata=self._metadata(invocation, started_at, definition),
            error=None,
        )

    @staticmethod
    def _metadata(
        invocation: ToolInvocation,
        started_at: float,
        definition: ToolDefinition[Any, Any] | None,
    ) -> ToolMetadata:
        return ToolMetadata(
            call_id=invocation.call_id,
            tool_name=invocation.tool,
            source=definition.source if definition is not None else "registry",
            duration_ms=max(0, round((perf_counter() - started_at) * 1_000)),
            tool_version=definition.version if definition is not None else "unknown",
        )

    @classmethod
    def _failure(
        cls,
        *,
        invocation: ToolInvocation,
        started_at: float,
        code: ToolErrorCode,
        message: str,
        definition: ToolDefinition[Any, Any] | None = None,
    ) -> ToolResponse:
        retryable = (
            definition.retry_policy.permits(code) if definition is not None else False
        )
        return ToolResponse(
            success=False,
            data=None,
            metadata=cls._metadata(invocation, started_at, definition),
            error=ToolError(code=code, message=message, retryable=retryable),
        )

