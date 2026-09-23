"""Strict V0 Case replay through the real read-only Kubernetes Tool handlers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import cast

from pydantic import JsonValue, ValidationError

from opspilot.cases import LoadedCase, ReplayStep, load_case
from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes import JsonObject, KubernetesNotFoundError
from opspilot.runtime.models import DiagnosisRequest
from opspilot.tools import ToolInvocation, ToolResponse, ToolRegistry
from opspilot.tools.kubernetes import build_kubernetes_registry


class ReplayConfigurationError(ValueError):
    """A replay case does not match the requested V1 target or Tool contract."""


class ReplayKubernetesReader:
    """Expose V0 raw case data at the same SDK-independent Reader boundary as live mode."""

    def __init__(self, case: LoadedCase) -> None:
        self._namespace = case.definition.target.namespace
        self._deployment_name = case.definition.target.resource
        fault_steps = [step for step in case.definition.replay_steps if step.phase == "fault"]
        by_tool = {step.invocation.tool: step for step in fault_steps}
        required = {
            "k8s.get_pod_status", "k8s.get_pod_events",
            "k8s.get_previous_logs", "k8s.get_deployment",
        }
        if not required.issubset(by_tool):
            raise ReplayConfigurationError("case lacks a required fault response")

        self._pod = _response_data(case, by_tool["k8s.get_pod_status"])
        pod_metadata = cast(JsonObject, self._pod["metadata"])
        self._pod_name = cast(str, pod_metadata["name"])
        pod_metadata["uid"] = "replay-pod-uid"
        self._pod["spec"] = {"containers": [{"name": self._deployment_name}]}

        event_data = _response_data(case, by_tool["k8s.get_pod_events"])
        items = cast(list[JsonValue], event_data["items"])
        events: list[JsonObject] = []
        for index, raw_item in enumerate(items):
            item = cast(JsonObject, raw_item)
            item["metadata"] = {"name": f"replay-event-{index}"}
            involved = cast(JsonObject, item["involvedObject"])
            involved["uid"] = "replay-pod-uid"
            events.append(item)
        self._events = tuple(events)

        log_data = _response_data(case, by_tool["k8s.get_previous_logs"])
        self._previous_logs = cast(str, log_data["content"])

        self._deployment = cast(JsonObject, deepcopy(case.read_json(case.definition.broken_manifest)))
        deployment_metadata = cast(JsonObject, self._deployment["metadata"])
        deployment_metadata["generation"] = 1
        self._deployment["status"] = {
            "replicas": 1, "readyReplicas": 0, "unavailableReplicas": 1,
        }

    async def read_pod(self, *, namespace: str, pod_name: str) -> JsonObject:
        if namespace != self._namespace or pod_name != self._pod_name:
            raise KubernetesNotFoundError("replay Pod is unavailable")
        return deepcopy(self._pod)

    async def list_pods(self, *, namespace: str, label_selector: str) -> tuple[JsonObject, ...]:
        if namespace != self._namespace or not label_selector:
            raise KubernetesNotFoundError("replay Pod is unavailable")
        return (deepcopy(self._pod),)

    async def list_events(self, *, namespace: str, field_selector: str, limit: int) -> tuple[JsonObject, ...]:
        if namespace != self._namespace or self._pod_name not in field_selector or limit < 1:
            raise KubernetesNotFoundError("replay Events are unavailable")
        return tuple(deepcopy(item) for item in self._events)

    async def read_pod_log(
        self, *, namespace: str, pod_name: str, container_name: str | None,
        previous: bool, tail_lines: int, since_seconds: int | None, max_bytes: int,
    ) -> str:
        del tail_lines, since_seconds, max_bytes
        if (
            namespace != self._namespace or pod_name != self._pod_name
            or container_name not in {None, self._deployment_name} or not previous
        ):
            raise KubernetesNotFoundError("replay log stream is unavailable")
        return self._previous_logs

    async def read_deployment(self, *, namespace: str, deployment_name: str) -> JsonObject:
        if namespace != self._namespace or deployment_name != self._deployment_name:
            raise KubernetesNotFoundError("replay Deployment is unavailable")
        return deepcopy(self._deployment)


class ReplayToolRegistry(ToolRegistry):
    """Enforce the fault-phase invocation sequence before normal Registry execution."""

    def __init__(self, case: LoadedCase) -> None:
        super().__init__()
        source = build_kubernetes_registry(ReplayKubernetesReader(case))
        for descriptor in source.descriptors():
            self.register(source.get(descriptor.name))
        self._expected = tuple(
            _expected_invocation(case, step)
            for step in case.definition.replay_steps if step.phase == "fault"
        )
        self._position = 0

    @property
    def consumed_calls(self) -> int:
        return self._position

    async def invoke(self, invocation: ToolInvocation) -> ToolResponse:
        if self._position >= len(self._expected):
            return self._mismatch(invocation)
        expected = self._expected[self._position]
        if invocation.call_id != expected.call_id or invocation.tool != expected.tool:
            return self._mismatch(invocation)
        definition = self.get(invocation.tool)
        try:
            actual_args = definition.input_model.model_validate(invocation.arguments, strict=True)
            expected_args = definition.input_model.model_validate(expected.arguments, strict=True)
        except ValidationError:
            return self._mismatch(invocation)
        if actual_args.model_dump(mode="json") != expected_args.model_dump(mode="json"):
            return self._mismatch(invocation)
        response = await super().invoke(invocation)
        if response.success:
            self._position += 1
        return response

    def _mismatch(self, invocation: ToolInvocation) -> ToolResponse:
        return self._failure(
            invocation=invocation, started_at=perf_counter(),
            code=ErrorCode.POLICY_REJECTED,
            message="replay invocation does not match the fault case",
            definition=self._tools.get(invocation.tool),
        )


class ReplayRegistryFactory:
    """Load a fresh Case and Registry for each run; never reuse replay cursor state."""

    def __init__(
        self, cases: Mapping[str, Path],
        *, loader: Callable[[Path], LoadedCase] = load_case,
    ) -> None:
        self._cases = dict(cases)
        self._loader = loader

    def __call__(self, request: DiagnosisRequest) -> ToolRegistry:
        if request.mode != "replay" or request.case_id is None:
            raise ReplayConfigurationError("replay Registry requires a case request")
        path = self._cases.get(request.case_id)
        if path is None:
            raise ReplayConfigurationError("case ID is not registered")
        case = self._loader(path)
        if case.definition.case_id != request.case_id or case.definition.target.namespace != request.namespace:
            raise ReplayConfigurationError("case identity or namespace differs from request")
        return ReplayToolRegistry(case)


def _response_data(case: LoadedCase, step: ReplayStep) -> JsonObject:
    response = case.response_for(step.invocation.call_id)
    if not response.success or response.data is None:
        raise ReplayConfigurationError("case fault response must be successful")
    return cast(JsonObject, deepcopy(response.data))


def _expected_invocation(case: LoadedCase, step: ReplayStep) -> ToolInvocation:
    target = case.definition.target
    source_args = step.invocation.arguments
    if source_args.get("namespace") != target.namespace:
        raise ReplayConfigurationError("case Tool namespace differs from target")
    if step.invocation.tool == "k8s.get_deployment":
        if source_args.get("deployment") != target.resource:
            raise ReplayConfigurationError("case Deployment differs from target")
        arguments: dict[str, JsonValue] = {
            "namespace": target.namespace, "deployment_name": target.resource,
        }
    else:
        pod_name = source_args.get("pod")
        status_step = next(
            item for item in case.definition.replay_steps
            if item.phase == "fault" and item.invocation.tool == "k8s.get_pod_status"
        )
        status_data = _response_data(case, status_step)
        metadata = status_data.get("metadata")
        expected_pod = metadata.get("name") if isinstance(metadata, dict) else None
        if (
            not isinstance(pod_name, str)
            or not pod_name.startswith(f"{target.resource}-")
            or pod_name != expected_pod
        ):
            raise ReplayConfigurationError("case Pod differs from target")
        arguments = {"namespace": target.namespace, "workload_name": target.resource}
        if step.invocation.tool in {"k8s.get_previous_logs", "k8s.get_pod_logs"}:
            container = source_args.get("container")
            if not isinstance(container, str):
                raise ReplayConfigurationError("case log container is missing")
            arguments["container_name"] = container
    return ToolInvocation(
        call_id=step.invocation.call_id,
        tool=step.invocation.tool,
        arguments=arguments,
    )
