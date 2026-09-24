"""V2 fixture responses through the same registered read-only Tool descriptors."""

from __future__ import annotations

from typing import cast

from opspilot.cases.v2 import LoadedCaseV2, ReplayMismatchV2
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.integrations.kubernetes import KubernetesReaderV2
from opspilot.integrations.prometheus import PrometheusReader
from opspilot.tools import ToolInvocation, ToolRegistry
from opspilot.tools.kubernetes import build_kubernetes_registry_v2
from opspilot.tools.prometheus import prometheus_tool_definitions_v2
from opspilot.tools.models import ToolMetadata, ToolResponse


class ReplayV2Registry(ToolRegistry):
    def __init__(self, case: LoadedCaseV2, branch_id: str) -> None:
        super().__init__()
        definitions = build_kubernetes_registry_v2(cast(KubernetesReaderV2, object()))
        for definition in prometheus_tool_definitions_v2(cast(PrometheusReader, object())):
            definitions.register(definition)
        for descriptor in definitions.descriptors():
            if descriptor.name in case.definition.allowed_tools:
                self.register(definitions.get(descriptor.name))
        self._session = case.open_branch(branch_id)
        self._consumed = 0

    @property
    def consumed_calls(self) -> int:
        return self._consumed

    def assert_complete(self) -> None:
        self._session.assert_complete()

    async def invoke(self, invocation: ToolInvocation) -> ToolResponse:
        try:
            response = self._session.next_response(invocation)
        except ReplayMismatchV2:
            return ToolResponse(
                success=False, data=None,
                metadata=ToolMetadata(
                    call_id=invocation.call_id, tool_name=invocation.tool,
                    source="replay", duration_ms=0, tool_version="v1",
                ),
                error=ErrorInfo.from_code(
                    ErrorCode.POLICY_REJECTED,
                    "replay call differs from declared branch", retryable=False,
                ),
            )
        self._consumed += 1
        return response
