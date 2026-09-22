"""Trusted, sanitized failures that may cross the Tool Gateway boundary."""

from __future__ import annotations

from opspilot.errors import ErrorCode


class ToolExecutionError(RuntimeError):
    """A failure with a stable code and a message safe for ToolResponse."""

    code: ErrorCode = ErrorCode.TOOL_EXECUTION_FAILED

    def __init__(self, safe_message: str) -> None:
        normalized = safe_message.strip() or "tool execution failed"
        self.safe_message = normalized[:500]
        super().__init__(self.safe_message)
