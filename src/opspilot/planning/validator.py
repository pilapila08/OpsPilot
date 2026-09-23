"""Deterministic admission gate for V1 model-proposed tool plans."""

from __future__ import annotations

import json

from pydantic import ValidationError

from opspilot.agent.schemas import BudgetState, ExecutionPlanV1, IntentOutput
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.planning.models import ValidatedPlanV1
from opspilot.tools.kubernetes.models import (
    DeploymentInput,
    PodEventsInput,
    PodLogsInput,
    PodStatusInput,
    PreviousPodLogsInput,
)
from opspilot.tools.models import ToolDescriptor, ToolRiskLevel
from opspilot.tools.registry import ToolRegistry

V1_TOOL_NAMES = frozenset(
    {
        "k8s.get_pod_status",
        "k8s.get_pod_events",
        "k8s.get_pod_logs",
        "k8s.get_previous_logs",
        "k8s.get_deployment",
    }
)
_V1_INPUT_MODELS = {
    "k8s.get_pod_status": PodStatusInput,
    "k8s.get_pod_events": PodEventsInput,
    "k8s.get_pod_logs": PodLogsInput,
    "k8s.get_previous_logs": PreviousPodLogsInput,
    "k8s.get_deployment": DeploymentInput,
}


class PlanValidator:
    """Validate against current registry definitions without executing handlers."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return self._registry.descriptors()

    def validate(
        self,
        plan: ExecutionPlanV1,
        *,
        intent: IntentOutput,
        budget: BudgetState,
    ) -> ValidatedPlanV1 | ErrorInfo:
        if (
            intent.intent != "diagnose"
            or intent.domain != "kubernetes"
            or intent.problem_type != "pod_restart"
        ):
            return _error(ErrorCode.POLICY_REJECTED, "intent is outside the V1 diagnosis scope")
        if budget.steps_used + len(plan.steps) > budget.limits.max_steps:
            return _error(ErrorCode.BUDGET_EXCEEDED, "plan exceeds step budget")
        if budget.tool_calls_used + len(plan.steps) > budget.limits.max_tool_calls:
            return _error(ErrorCode.BUDGET_EXCEEDED, "plan exceeds tool call budget")
        if set(budget.exhausted_dimensions) & {"tokens", "cost", "time"}:
            return _error(ErrorCode.BUDGET_EXCEEDED, "model budget is exhausted")

        descriptors = {item.name: item for item in self.descriptors}
        fingerprints: set[tuple[str, str]] = set()
        versions: list[tuple[str, str]] = []
        timeouts: list[float] = []
        retry_timeouts: list[float] = []

        for step in plan.steps:
            descriptor = descriptors.get(step.tool)
            if descriptor is None:
                return _error(ErrorCode.TOOL_NOT_FOUND, "plan names an unregistered tool")
            if step.tool not in V1_TOOL_NAMES or descriptor.risk_level is not ToolRiskLevel.READ_ONLY:
                return _error(ErrorCode.POLICY_REJECTED, "plan names a tool outside read-only V1 policy")

            definition = self._registry.get(step.tool)
            if definition.input_model is not _V1_INPUT_MODELS[step.tool]:
                return _error(ErrorCode.POLICY_REJECTED, "tool input model differs from the V1 contract")
            try:
                arguments = definition.input_model.model_validate(step.arguments, strict=True)
            except ValidationError:
                return _error(ErrorCode.INVALID_ARGUMENT, "plan tool arguments failed schema validation")

            normalized = arguments.model_dump(mode="json")
            if normalized["namespace"] != intent.target.namespace:
                return _error(ErrorCode.POLICY_REJECTED, "plan namespace exceeds the authorized scope")
            target_field = "deployment_name" if step.tool == "k8s.get_deployment" else (
                "pod_name" if normalized.get("pod_name") is not None else "workload_name"
            )
            if normalized[target_field] != intent.target.resource:
                return _error(ErrorCode.POLICY_REJECTED, "plan target differs from the routed resource")

            fingerprint = (step.tool, json.dumps(normalized, sort_keys=True, separators=(",", ":")))
            if fingerprint in fingerprints:
                return _error(ErrorCode.INVALID_ARGUMENT, "plan repeats a tool call with identical arguments")
            fingerprints.add(fingerprint)
            versions.append((step.tool, descriptor.version))
            timeouts.append(descriptor.timeout_seconds)
            retry_timeouts.extend([descriptor.timeout_seconds] * descriptor.retry_policy.max_retries)

        retries_available = max(0, budget.limits.max_retries - budget.retries_used)
        retry_allowance = min(len(retry_timeouts), retries_available)
        projected_calls = len(plan.steps) + retry_allowance
        if budget.tool_calls_used + projected_calls > budget.limits.max_tool_calls:
            return _error(ErrorCode.BUDGET_EXCEEDED, "plan exceeds tool call budget")
        projected_seconds = sum(timeouts) + sum(sorted(retry_timeouts, reverse=True)[:retry_allowance])
        if budget.elapsed_seconds + projected_seconds > budget.limits.timeout_seconds:
            return _error(ErrorCode.BUDGET_EXCEEDED, "plan exceeds runtime time budget")
        return ValidatedPlanV1.from_plan(plan, tuple(versions))


def _error(code: ErrorCode, message: str) -> ErrorInfo:
    return ErrorInfo.from_code(code, message, retryable=False)
