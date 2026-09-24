"""V2 round decisions and deterministic admission without changing V1 plans."""

from __future__ import annotations

import json
import re
from datetime import datetime
from collections.abc import Mapping
from typing import Literal, cast

from pydantic import BaseModel, Field, JsonValue, ValidationError, model_validator

from opspilot.agent.schemas import BudgetState, StrictSchema
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.evidence.models import Evidence, EvidenceId
from opspilot.routing.v2 import IntentV2
from opspilot.tools import ToolRegistry
from opspilot.tools.kubernetes.models import (
    DeploymentInput, PodEventsInput, PodLogsInput, PodStatusInput,
    PreviousPodLogsInput,
)
from opspilot.tools.kubernetes.v2_models import (
    EndpointsInputV2, IngressInputV2, ResourceUsageInputV2, ServiceInputV2,
    ServiceMembershipInputV2,
)
from opspilot.tools.prometheus import MetricInputV2, Service503InputV2
from opspilot.tools.release import CicdDeploymentInput, GitCommitInput, GitDiffInput
from opspilot.tools.models import TOOL_NAME_PATTERN, ToolDescriptor, ToolRiskLevel

_CALL_ID = r"^[a-z][a-z0-9_-]{2,127}$"
_TARGET_KEYS = frozenset({
    "pod_name", "workload_name", "deployment_name", "service_name", "ingress_name"
})
_SIGNAL_KEYS = frozenset({
    "restart_count", "container_state", "last_exit_code", "last_reason",
    "event_reason", "failure_count", "liveness_failure", "byte_count",
    "truncated", "startup_duration_seconds", "terminated_after_seconds",
    "initial_delay_seconds", "period_seconds", "failure_threshold",
    "startup_probe_configured", "startup_probe_period_seconds",
    "startup_probe_failure_threshold",
    "service_port", "service_port_name", "service_type", "target_port",
    "selector_count", "endpoint_count", "endpoint_slice_count",
    "ready_endpoint_count", "serving_endpoint_count", "endpoint_snapshot_truncated",
    "unknown_ready_endpoint_count", "terminating_serving_endpoint_count",
    "active_ready_pod_count", "matching_ready_pod_count", "membership_truncated",
    "backend_service", "backend_port", "route_path", "cpu_quantity",
    "memory_quantity", "deployment_generation", "rollout_ambiguous",
    "termination_reason", "memory_limit_bytes", "metric_value", "metric_unit",
    "sample_status",
    "pod_ready", "readiness_failure", "probe_kind", "probe_port",
    "release_id", "commit_sha", "parent_sha", "base_sha", "head_sha",
    "release_status", "environment", "deployment_history_truncated",
    "diff_file_count", "diff_additions", "diff_deletions",
    "diff_config_count", "diff_code_count", "diff_docs_count",
    "diff_manifests_count", "diff_sensitive_count", "diff_tests_count",
    "diff_other_count",
})
_CATEGORIES = frozenset({
    "OOMKilled", "CrashLoopBackOff", "Running", "Waiting", "Terminated",
    "Unhealthy", "Killing", "BackOff", "Error", "Completed",
})
_NUMERIC_FACTS = frozenset({
    "restart_count", "last_exit_code", "failure_count", "byte_count",
    "startup_duration_seconds", "terminated_after_seconds",
    "initial_delay_seconds", "period_seconds", "failure_threshold",
    "startup_probe_period_seconds", "startup_probe_failure_threshold",
    "service_port", "selector_count", "endpoint_count", "endpoint_slice_count",
    "ready_endpoint_count", "serving_endpoint_count", "deployment_generation",
    "unknown_ready_endpoint_count", "terminating_serving_endpoint_count",
    "active_ready_pod_count", "matching_ready_pod_count",
    "diff_file_count", "diff_additions", "diff_deletions",
    "diff_config_count", "diff_code_count", "diff_docs_count",
    "diff_manifests_count", "diff_sensitive_count", "diff_tests_count",
    "diff_other_count",
    "memory_limit_bytes",
})
_BOOLEAN_FACTS = frozenset({
    "liveness_failure", "truncated", "startup_probe_configured",
    "endpoint_snapshot_truncated", "rollout_ambiguous", "membership_truncated",
    "pod_ready", "readiness_failure",
    "deployment_history_truncated",
})
_NAMED_PORT = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_RESOURCE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_ROUTE_PATH = re.compile(r"^/[A-Za-z0-9._~/%-]{0,255}$")
_QUANTITY = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:n|u|m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei)?$")
_DEFAULT_INPUT_MODELS: Mapping[str, type[BaseModel]] = {
    "k8s.get_pod_status": PodStatusInput,
    "k8s.get_pod_events": PodEventsInput,
    "k8s.get_pod_logs": PodLogsInput,
    "k8s.get_previous_logs": PreviousPodLogsInput,
    "k8s.get_deployment": DeploymentInput,
    "k8s.get_service": ServiceInputV2,
    "k8s.get_service_membership": ServiceMembershipInputV2,
    "k8s.get_endpoints": EndpointsInputV2,
    "k8s.get_ingress": IngressInputV2,
    "k8s.get_resource_usage": ResourceUsageInputV2,
    "prometheus.query_cpu": MetricInputV2,
    "prometheus.query_memory": MetricInputV2,
    "prometheus.query_latency": MetricInputV2,
    "prometheus.query_error_rate": MetricInputV2,
    "prometheus.query_http_503_rate": Service503InputV2,
    "cicd.get_recent_deployment": CicdDeploymentInput,
    "git.get_recent_commit": GitCommitInput,
    "git.diff": GitDiffInput,
}


class PlanCallV2(StrictSchema):
    call_id: str = Field(min_length=3, max_length=128, pattern=_CALL_ID)
    tool: str = Field(min_length=3, max_length=128, pattern=TOOL_NAME_PATTERN)
    arguments: dict[str, JsonValue]
    reason: str = Field(min_length=1, max_length=500)


class PlanDecisionV2(StrictSchema):
    schema_version: Literal[2] = 2
    round_no: int = Field(ge=1, le=4)
    action: Literal["continue", "finish", "partial"]
    based_on_evidence_ids: tuple[EvidenceId, ...] = Field(max_length=100)
    calls: tuple[PlanCallV2, ...] = Field(max_length=4)

    @model_validator(mode="after")
    def validate_action(self) -> PlanDecisionV2:
        if (self.action == "continue") != bool(self.calls):
            raise ValueError("continue needs calls; finish/partial forbid calls")
        if len(set(self.based_on_evidence_ids)) != len(self.based_on_evidence_ids):
            raise ValueError("based-on Evidence IDs must be unique")
        if len({call.call_id for call in self.calls}) != len(self.calls):
            raise ValueError("round call IDs must be unique")
        return self


class ObservationFactV2(StrictSchema):
    key: str = Field(min_length=1, max_length=64)
    value: bool | int | float | str


class ObservationSignalV2(StrictSchema):
    evidence_id: EvidenceId
    source: str = Field(min_length=1, max_length=64)
    resource: str = Field(min_length=1, max_length=512)
    observed_at: datetime
    keys: tuple[str, ...] = Field(max_length=20)
    categories: tuple[str, ...] = Field(max_length=10)
    facts: tuple[ObservationFactV2, ...] = Field(default=(), max_length=20)


class ObservationSummaryV2(StrictSchema):
    trace_id: str = Field(min_length=3, max_length=128)
    evidence_ids: tuple[EvidenceId, ...] = Field(max_length=100)
    signals: tuple[ObservationSignalV2, ...] = Field(max_length=100)
    tool_error_codes: tuple[ErrorCode, ...] = Field(max_length=100)

    @classmethod
    def from_persisted(
        cls, trace_id: str, evidence: tuple[Evidence, ...],
        error_codes: tuple[ErrorCode, ...] = (),
    ) -> ObservationSummaryV2:
        if any(item.trace_id != trace_id for item in evidence):
            raise ValueError("observation must use current Trace Evidence")
        signals = tuple(
            ObservationSignalV2(
                evidence_id=item.evidence_id, source=item.source,
                resource=item.resource, observed_at=item.observed_at,
                keys=tuple(sorted({a.key for a in item.attributes if a.key in _SIGNAL_KEYS})),
                categories=tuple(sorted({
                    a.value for a in item.attributes
                    if a.key in {"container_state", "last_reason", "event_reason"}
                    and isinstance(a.value, str) and a.value in _CATEGORIES
                })),
                facts=tuple(sorted(
                    (fact for attribute in item.attributes
                     if (fact := _safe_fact(attribute.key, attribute.value)) is not None),
                    key=lambda fact: fact.key,
                )),
            )
            for item in evidence
        )
        return cls(
            trace_id=trace_id,
            evidence_ids=tuple(item.evidence_id for item in evidence),
            signals=signals,
            tool_error_codes=error_codes,
        )


def _safe_fact(key: str, value: object) -> ObservationFactV2 | None:
    if key == "metric_value" and type(value) in (int, float):
        return ObservationFactV2(key=key, value=cast(float, value))
    if key in _NUMERIC_FACTS and type(value) is int:
        return ObservationFactV2(key=key, value=value)
    if key in _BOOLEAN_FACTS and type(value) is bool:
        return ObservationFactV2(key=key, value=value)
    if key in {"target_port", "backend_port"}:
        if type(value) is int and 1 <= value <= 65_535:
            return ObservationFactV2(key=key, value=value)
        if isinstance(value, str) and _NAMED_PORT.fullmatch(value):
            return ObservationFactV2(key=key, value=value)
    if isinstance(value, str):
        pattern = {
            "backend_service": _RESOURCE_NAME,
            "service_port_name": _NAMED_PORT,
            "service_type": re.compile(r"^(ClusterIP|NodePort|LoadBalancer|ExternalName)$"),
            "route_path": _ROUTE_PATH,
            "cpu_quantity": _QUANTITY,
            "memory_quantity": _QUANTITY,
            "metric_unit": re.compile(r"^(cores|bytes|seconds|ratio)$"),
            "sample_status": re.compile(r"^(missing|stale)$"),
            "probe_kind": re.compile(r"^(http|tcp|exec)$"),
            "release_id": re.compile(r"^[1-9][0-9]{0,19}$"),
            "commit_sha": re.compile(r"^[0-9a-f]{40}$"),
            "parent_sha": re.compile(r"^[0-9a-f]{40}$"),
            "base_sha": re.compile(r"^[0-9a-f]{40}$"),
            "head_sha": re.compile(r"^[0-9a-f]{40}$"),
            "release_status": re.compile(r"^(success|failure|error|inactive|in_progress|queued|pending|unknown)$"),
            "environment": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"),
        }.get(key)
        if pattern is not None and pattern.fullmatch(value):
            return ObservationFactV2(key=key, value=value)
    return None


class AdmittedDecisionV2(StrictSchema):
    decision_json: str = Field(min_length=1, max_length=50_000)
    tool_versions: tuple[tuple[str, str], ...]

    @model_validator(mode="after")
    def match_calls(self) -> AdmittedDecisionV2:
        decision = self.decision
        if len(self.tool_versions) != len(decision.calls) or any(
            name != call.tool
            for (name, _), call in zip(self.tool_versions, decision.calls, strict=True)
        ):
            raise ValueError("admitted Tool versions must match decision calls")
        return self

    @property
    def decision(self) -> PlanDecisionV2:
        return PlanDecisionV2.model_validate_json(self.decision_json, strict=True)


class V2PlanValidator:
    """Admit a complete round against live registry, scope, history and budget."""

    def __init__(
        self, registry: ToolRegistry,
        allowed_input_models: Mapping[str, type[BaseModel]] | None = None,
    ) -> None:
        self._registry = registry
        self._allowed_input_models = dict(allowed_input_models or _DEFAULT_INPUT_MODELS)

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return tuple(self._registry.descriptors())

    def validate(
        self, decision: PlanDecisionV2, *, intent: IntentV2,
        round_no: int, evidence_ids: tuple[str, ...],
        used_call_ids: frozenset[str],
        used_requests: frozenset[tuple[str, str]],
        allowed_resources: frozenset[str], budget: BudgetState,
    ) -> AdmittedDecisionV2 | ErrorInfo:
        if decision.round_no != round_no or round_no > 4:
            return _error(ErrorCode.INVALID_ARGUMENT, "decision round does not match Runtime")
        if not set(decision.based_on_evidence_ids).issubset(evidence_ids):
            return _error(ErrorCode.POLICY_REJECTED, "decision cites foreign Evidence")
        if round_no == 1 and decision.based_on_evidence_ids:
            return _error(ErrorCode.POLICY_REJECTED, "initial round cannot cite Evidence")
        if any(call.call_id in used_call_ids for call in decision.calls):
            return _error(ErrorCode.INVALID_ARGUMENT, "logical Tool call ID was already used")
        if decision.action != "continue":
            return AdmittedDecisionV2(
                decision_json=decision.model_dump_json(), tool_versions=()
            )
        if budget.steps_used + len(decision.calls) > budget.limits.max_steps:
            return _error(ErrorCode.BUDGET_EXCEEDED, "round exceeds step budget")
        if budget.tool_calls_used + len(decision.calls) > budget.limits.max_tool_calls:
            return _error(ErrorCode.BUDGET_EXCEEDED, "round exceeds Tool call budget")
        if set(budget.exhausted_dimensions) & {"tokens", "cost", "time"}:
            return _error(ErrorCode.BUDGET_EXCEEDED, "round exceeds model budget")
        versions: list[tuple[str, str]] = []
        timeouts: list[float] = []
        retry_timeouts: list[float] = []
        fingerprints: set[tuple[str, str]] = set()
        for call in decision.calls:
            try:
                definition = self._registry.get(call.tool)
            except LookupError:
                return _error(ErrorCode.TOOL_NOT_FOUND, "decision names an unregistered Tool")
            if definition.risk_level is not ToolRiskLevel.READ_ONLY:
                return _error(ErrorCode.POLICY_REJECTED, "decision names a non-read-only Tool")
            if definition.input_model is not self._allowed_input_models.get(call.tool):
                return _error(ErrorCode.POLICY_REJECTED, "Tool input differs from V2 policy")
            try:
                arguments = definition.input_model.model_validate(call.arguments, strict=True)
            except ValidationError:
                return _error(ErrorCode.INVALID_ARGUMENT, "Tool arguments failed strict schema")
            normalized = arguments.model_dump(mode="json")
            if normalized.get("namespace") != intent.target.namespace:
                return _error(ErrorCode.POLICY_REJECTED, "Tool namespace exceeds scope")
            if not any(normalized.get(key) is not None for key in _TARGET_KEYS):
                return _error(ErrorCode.POLICY_REJECTED, "Tool request lacks a scoped target")
            for key in _TARGET_KEYS:
                value = normalized.get(key)
                if value is not None and value not in allowed_resources:
                    return _error(ErrorCode.POLICY_REJECTED, "Tool target exceeds scope")
            fingerprint = (
                call.tool, json.dumps(normalized, sort_keys=True, separators=(",", ":"))
            )
            if fingerprint in fingerprints or fingerprint in used_requests:
                return _error(ErrorCode.INVALID_ARGUMENT, "Tool request repeats without change")
            fingerprints.add(fingerprint)
            versions.append((call.tool, definition.version))
            timeouts.append(definition.timeout_seconds)
            retry_timeouts.extend(
                [definition.timeout_seconds] * definition.retry_policy.max_retries
            )
        retries_left = max(0, budget.limits.max_retries - budget.retries_used)
        retry_allowance = min(len(retry_timeouts), retries_left)
        if budget.tool_calls_used + len(decision.calls) + retry_allowance > budget.limits.max_tool_calls:
            return _error(ErrorCode.BUDGET_EXCEEDED, "round exceeds Tool retry budget")
        projected = sum(timeouts) + sum(sorted(retry_timeouts, reverse=True)[:retry_allowance])
        if budget.elapsed_seconds + projected > budget.limits.timeout_seconds:
            return _error(ErrorCode.BUDGET_EXCEEDED, "round exceeds time budget")
        return AdmittedDecisionV2(
            decision_json=decision.model_dump_json(), tool_versions=tuple(versions)
        )


def _error(code: ErrorCode, message: str) -> ErrorInfo:
    return ErrorInfo.from_code(code, message, retryable=False)
