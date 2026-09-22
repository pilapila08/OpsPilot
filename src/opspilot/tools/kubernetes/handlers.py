"""Read-only Kubernetes Tool handlers over the SDK-independent reader."""

from __future__ import annotations

import re
from typing import Literal, cast

from pydantic import JsonValue

from opspilot.integrations.kubernetes import (
    JsonObject,
    KubernetesAmbiguousTargetError,
    KubernetesDataError,
    KubernetesReader,
    PodTargetResolver,
    ResolvedPod,
)
from opspilot.tools.kubernetes.models import (
    MAX_EVENT_MESSAGE_CHARS,
    ContainerStatusOutput,
    DeploymentContainerOutput,
    DeploymentInput,
    DeploymentOutput,
    EnvironmentOutput,
    KubernetesEventOutput,
    PodConditionOutput,
    PodEventsInput,
    PodEventsOutput,
    PodLogsInput,
    PodLogsOutput,
    PodStatusInput,
    PodStatusOutput,
    PreviousPodLogsInput,
    ProbeOutput,
    ResourceRequirementsOutput,
)

_SENSITIVE_ENV_TOKENS = frozenset(
    {
        "AUTH",
        "CREDENTIAL",
        "CREDENTIALS",
        "PASSWORD",
        "PASSWD",
        "PRIVATE",
        "SECRET",
        "TOKEN",
    }
)


class KubernetesToolHandlers:
    """Normalize Kubernetes reads into the five stable V1 Tool outputs."""

    def __init__(self, reader: KubernetesReader) -> None:
        self._reader = reader
        self._resolver = PodTargetResolver(reader)

    async def get_pod_status(self, arguments: PodStatusInput) -> PodStatusOutput:
        resolved, pod = await self._resolver.resolve_with_pod(
            arguments.to_target()
        )
        status = _required_object(pod.get("status"), "pod status")
        phase = _required_text(status.get("phase"), "pod phase")
        conditions = tuple(
            _pod_condition(item)
            for item in _object_list(
                status.get("conditions"),
                "pod conditions",
                required=False,
                limit=100,
            )
        )
        containers = tuple(
            _container_status(item)
            for item in _object_list(
                status.get("containerStatuses"),
                "pod container statuses",
                required=False,
                limit=100,
            )
        )
        return PodStatusOutput(
            namespace=resolved.namespace,
            pod_name=resolved.pod_name,
            phase=phase,
            conditions=conditions,
            containers=containers,
        )

    async def get_pod_events(self, arguments: PodEventsInput) -> PodEventsOutput:
        resolved, _ = await self._resolver.resolve_with_pod(
            arguments.to_target()
        )
        events = await self._reader.list_events(
            namespace=resolved.namespace,
            field_selector=(
                f"involvedObject.uid={resolved.pod_uid},"
                f"involvedObject.name={resolved.pod_name}"
            ),
            limit=100,
        )
        normalized = [
            _event(item, resolved)
            for item in events[:100]
        ]
        normalized.sort(key=lambda item: item.name)
        normalized.sort(
            key=lambda item: item.last_timestamp or "",
            reverse=True,
        )
        return PodEventsOutput(
            namespace=resolved.namespace,
            pod_name=resolved.pod_name,
            events=tuple(normalized),
        )

    async def get_pod_logs(self, arguments: PodLogsInput) -> PodLogsOutput:
        return await self._get_logs(arguments, previous=False)

    async def get_previous_logs(
        self,
        arguments: PreviousPodLogsInput,
    ) -> PodLogsOutput:
        return await self._get_logs(arguments, previous=True)

    async def get_deployment(
        self,
        arguments: DeploymentInput,
    ) -> DeploymentOutput:
        deployment = await self._reader.read_deployment(
            namespace=arguments.namespace,
            deployment_name=arguments.deployment_name,
        )
        return _deployment_output(deployment, arguments)

    async def _get_logs(
        self,
        arguments: PodLogsInput | PreviousPodLogsInput,
        *,
        previous: bool,
    ) -> PodLogsOutput:
        resolved, _ = await self._resolver.resolve_with_pod(arguments.target())
        container = _select_container(resolved)
        query = arguments.query()
        content = await self._reader.read_pod_log(
            namespace=resolved.namespace,
            pod_name=resolved.pod_name,
            container_name=container,
            previous=previous,
            tail_lines=query.tail_lines,
            since_seconds=query.since_seconds,
            max_bytes=query.max_bytes,
        )
        bounded, byte_count, truncated = _truncate_utf8(
            content,
            query.max_bytes,
        )
        return PodLogsOutput(
            namespace=resolved.namespace,
            pod_name=resolved.pod_name,
            container=container,
            previous=previous,
            content=bounded,
            truncated=truncated,
            byte_count=byte_count,
        )


def _pod_condition(value: JsonObject) -> PodConditionOutput:
    return PodConditionOutput(
        type=_required_text(value.get("type"), "pod condition type"),
        status=_required_text(value.get("status"), "pod condition status"),
        reason=_optional_text(value.get("reason"), "pod condition reason"),
        message=_optional_message(value.get("message"), "pod condition message"),
        last_transition_time=_optional_timestamp(
            value.get("lastTransitionTime"),
            "pod condition transition time",
        ),
    )


def _container_status(value: JsonObject) -> ContainerStatusOutput:
    state_name, state = _container_state(value.get("state"))
    last_state = _optional_object(value.get("lastState"), "container last state")
    terminated = (
        _optional_object(last_state.get("terminated"), "last terminated state")
        if last_state is not None
        else None
    )
    return ContainerStatusOutput(
        name=_required_text(value.get("name"), "container status name"),
        ready=_required_bool(value.get("ready"), "container ready"),
        restart_count=_nonnegative_int(
            value.get("restartCount"),
            "container restart count",
        ),
        state=state_name,
        reason=(
            _optional_text(state.get("reason"), "container state reason")
            if state is not None
            else None
        ),
        last_exit_code=(
            _optional_int(terminated.get("exitCode"), "last exit code")
            if terminated is not None
            else None
        ),
        last_reason=(
            _optional_text(terminated.get("reason"), "last termination reason")
            if terminated is not None
            else None
        ),
        last_started_at=(
            _optional_timestamp(
                terminated.get("startedAt"),
                "last termination start time",
            )
            if terminated is not None
            else None
        ),
        last_finished_at=(
            _optional_timestamp(
                terminated.get("finishedAt"),
                "last termination finish time",
            )
            if terminated is not None
            else None
        ),
    )


def _container_state(
    value: JsonValue | None,
) -> tuple[
    Literal["waiting", "running", "terminated", "unknown"],
    JsonObject | None,
]:
    state = _optional_object(value, "container state")
    if state is None:
        return "unknown", None
    active = [
        (name, _required_object(state[name], f"container {name} state"))
        for name in ("waiting", "running", "terminated")
        if state.get(name) is not None
    ]
    if not active:
        return "unknown", None
    if len(active) != 1:
        raise KubernetesDataError(
            "Kubernetes container has multiple active states"
        )
    name, details = active[0]
    if name == "waiting":
        return "waiting", details
    if name == "running":
        return "running", details
    return "terminated", details


def _event(value: JsonObject, pod: ResolvedPod) -> KubernetesEventOutput:
    metadata = _required_object(value.get("metadata"), "event metadata")
    involved = _optional_object(
        value.get("involvedObject"),
        "event involved object",
    )
    if involved is not None:
        namespace = involved.get("namespace")
        name = involved.get("name")
        uid = involved.get("uid")
        if namespace is not None and namespace != pod.namespace:
            raise KubernetesDataError(
                "Kubernetes event namespace did not match the target"
            )
        if name is not None and name != pod.pod_name:
            raise KubernetesDataError(
                "Kubernetes event name did not match the target"
            )
        if uid is not None and uid != pod.pod_uid:
            raise KubernetesDataError(
                "Kubernetes event UID did not match the target"
            )

    first_timestamp = _optional_timestamp(
        value.get("firstTimestamp"),
        "event first timestamp",
    )
    last_timestamp = _optional_timestamp(
        value.get("lastTimestamp"),
        "event last timestamp",
    )
    if last_timestamp is None:
        last_timestamp = _optional_timestamp(
            value.get("eventTime"),
            "event time",
        )
    return KubernetesEventOutput(
        name=_required_text(metadata.get("name"), "event name"),
        type=_required_text(value.get("type"), "event type"),
        reason=_required_text(value.get("reason"), "event reason"),
        message=_message(value.get("message"), "event message"),
        count=_nonnegative_int(value.get("count"), "event count", default=1),
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
    )


def _deployment_output(
    deployment: JsonObject,
    arguments: DeploymentInput,
) -> DeploymentOutput:
    metadata = _required_object(
        deployment.get("metadata"),
        "deployment metadata",
    )
    namespace = _required_text(
        metadata.get("namespace"),
        "deployment namespace",
    )
    name = _required_text(metadata.get("name"), "deployment name")
    if namespace != arguments.namespace or name != arguments.deployment_name:
        raise KubernetesDataError(
            "Kubernetes deployment identity did not match the request"
        )

    spec = _required_object(deployment.get("spec"), "deployment spec")
    status = _optional_object(deployment.get("status"), "deployment status")
    selector_data = _required_object(
        spec.get("selector"),
        "deployment selector",
    )
    if selector_data.get("matchExpressions") not in (None, []):
        raise KubernetesDataError(
            "Kubernetes deployment selector match expressions are unsupported"
        )
    match_labels = _required_object(
        selector_data.get("matchLabels"),
        "deployment selector matchLabels",
    )
    if not match_labels:
        raise KubernetesDataError(
            "Kubernetes deployment selector has no match labels"
        )
    labels: list[tuple[str, str]] = []
    for key, value in match_labels.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or len(key) > 316
            or len(value) > 63
        ):
            raise KubernetesDataError(
                "Kubernetes deployment selector is invalid"
            )
        labels.append((key, value))
    selector: dict[str, str] = {}
    for key, value in sorted(labels):
        selector[key] = value

    template = _required_object(
        spec.get("template"),
        "deployment pod template",
    )
    pod_spec = _required_object(
        template.get("spec"),
        "deployment pod template spec",
    )
    containers = tuple(
        _deployment_container(item)
        for item in _object_list(
            pod_spec.get("containers"),
            "deployment containers",
            required=True,
            limit=100,
        )
    )
    if not containers:
        raise KubernetesDataError("Kubernetes deployment has no containers")

    return DeploymentOutput(
        namespace=namespace,
        name=name,
        generation=_nonnegative_int(
            metadata.get("generation"),
            "deployment generation",
            default=0,
        ),
        replicas=_nonnegative_int(
            spec.get("replicas"),
            "deployment replicas",
            default=1,
        ),
        ready_replicas=_nonnegative_int(
            status.get("readyReplicas") if status is not None else None,
            "deployment ready replicas",
            default=0,
        ),
        unavailable_replicas=_nonnegative_int(
            status.get("unavailableReplicas") if status is not None else None,
            "deployment unavailable replicas",
            default=0,
        ),
        selector=selector,
        containers=containers,
    )


def _deployment_container(value: JsonObject) -> DeploymentContainerOutput:
    resources = _optional_object(value.get("resources"), "container resources")
    return DeploymentContainerOutput(
        name=_required_text(value.get("name"), "deployment container name"),
        image=_required_text(
            value.get("image"),
            "deployment container image",
            max_chars=2_048,
        ),
        liveness_probe=_probe(value.get("livenessProbe"), "liveness probe"),
        readiness_probe=_probe(value.get("readinessProbe"), "readiness probe"),
        startup_probe=_probe(value.get("startupProbe"), "startup probe"),
        resources=_resources(resources),
        environment=tuple(
            _environment(item)
            for item in _object_list(
                value.get("env"),
                "container environment",
                required=False,
                limit=500,
            )
        ),
    )


def _probe(value: JsonValue | None, field: str) -> ProbeOutput | None:
    probe = _optional_object(value, field)
    if probe is None:
        return None
    targets = [
        name
        for name in ("httpGet", "tcpSocket", "exec")
        if probe.get(name) is not None
    ]
    if len(targets) != 1:
        raise KubernetesDataError(
            f"Kubernetes {field} must have exactly one supported target"
        )
    target_name = targets[0]
    target = _required_object(probe[target_name], f"{field} target")
    initial_delay_seconds = _nonnegative_int(
        probe.get("initialDelaySeconds"),
        f"{field} initial delay",
        default=0,
    )
    period_seconds = _positive_int(
        probe.get("periodSeconds"),
        f"{field} period",
        default=10,
    )
    timeout_seconds = _positive_int(
        probe.get("timeoutSeconds"),
        f"{field} timeout",
        default=1,
    )
    failure_threshold = _positive_int(
        probe.get("failureThreshold"),
        f"{field} failure threshold",
        default=3,
    )
    success_threshold = _positive_int(
        probe.get("successThreshold"),
        f"{field} success threshold",
        default=1,
    )
    if target_name == "httpGet":
        return ProbeOutput(
            kind="http",
            path=_required_text(
                target.get("path", "/"),
                f"{field} HTTP path",
                max_chars=2_048,
            ),
            port=_port(target.get("port"), field),
            host=_optional_text(
                target.get("host"),
                f"{field} HTTP host",
            ),
            scheme=_optional_text(
                target.get("scheme"),
                f"{field} HTTP scheme",
            ),
            initial_delay_seconds=initial_delay_seconds,
            period_seconds=period_seconds,
            timeout_seconds=timeout_seconds,
            failure_threshold=failure_threshold,
            success_threshold=success_threshold,
        )
    if target_name == "tcpSocket":
        return ProbeOutput(
            kind="tcp",
            port=_port(target.get("port"), field),
            host=_optional_text(
                target.get("host"),
                f"{field} TCP host",
            ),
            initial_delay_seconds=initial_delay_seconds,
            period_seconds=period_seconds,
            timeout_seconds=timeout_seconds,
            failure_threshold=failure_threshold,
            success_threshold=success_threshold,
        )
    command = target.get("command")
    if not isinstance(command, list) or not command:
        raise KubernetesDataError(
            f"Kubernetes {field} exec command is invalid"
        )
    normalized_command = tuple(
        _required_text(item, f"{field} exec command", max_chars=2_048)
        for item in command[:100]
    )
    return ProbeOutput(
        kind="exec",
        command=normalized_command,
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
        timeout_seconds=timeout_seconds,
        failure_threshold=failure_threshold,
        success_threshold=success_threshold,
    )


def _resources(value: JsonObject | None) -> ResourceRequirementsOutput:
    if value is None:
        return ResourceRequirementsOutput()
    return ResourceRequirementsOutput(
        requests=_string_map(value.get("requests"), "resource requests"),
        limits=_string_map(value.get("limits"), "resource limits"),
    )


def _environment(value: JsonObject) -> EnvironmentOutput:
    name = _required_text(value.get("name"), "environment name")
    literal = value.get("value")
    value_from = _optional_object(value.get("valueFrom"), "environment valueFrom")
    if literal is not None and value_from is not None:
        raise KubernetesDataError(
            "Kubernetes environment has multiple value sources"
        )
    if value_from is None:
        literal_value = _required_text(
            literal if literal is not None else "",
            "environment literal value",
            max_chars=2_048,
            allow_empty=True,
        )
        redacted = _is_sensitive_environment_name(name)
        return EnvironmentOutput(
            name=name,
            kind="literal",
            value=None if redacted else literal_value,
            redacted=redacted,
        )

    references = [
        key
        for key in (
            "fieldRef",
            "resourceFieldRef",
            "configMapKeyRef",
            "secretKeyRef",
        )
        if value_from.get(key) is not None
    ]
    if len(references) != 1:
        return EnvironmentOutput(
            name=name,
            kind="unknown_ref",
            redacted=True,
        )
    reference_type = references[0]
    reference = _required_object(
        value_from[reference_type],
        "environment reference",
    )
    if reference_type == "fieldRef":
        return EnvironmentOutput(
            name=name,
            kind="field_ref",
            reference_key=_optional_text(
                reference.get("fieldPath"),
                "environment field path",
            ),
        )
    if reference_type == "resourceFieldRef":
        return EnvironmentOutput(
            name=name,
            kind="resource_field_ref",
            reference_name=_optional_text(
                reference.get("containerName"),
                "environment resource container",
            ),
            reference_key=_optional_text(
                reference.get("resource"),
                "environment resource",
            ),
        )

    reference_name = _optional_text(
        reference.get("name"),
        "environment reference name",
    )
    reference_key = _optional_text(
        reference.get("key"),
        "environment reference key",
    )
    optional = _optional_bool(
        reference.get("optional"),
        "environment reference optional",
    )
    if reference_type == "configMapKeyRef":
        return EnvironmentOutput(
            name=name,
            kind="config_map_key_ref",
            reference_name=reference_name,
            reference_key=reference_key,
            optional=optional,
        )
    return EnvironmentOutput(
        name=name,
        kind="secret_key_ref",
        reference_name=reference_name,
        reference_key=reference_key,
        optional=optional,
        redacted=True,
    )


def _select_container(pod: ResolvedPod) -> str:
    if pod.requested_container is not None:
        return pod.requested_container
    if len(pod.container_names) != 1:
        raise KubernetesAmbiguousTargetError(
            "Container name is required for a multi-container pod"
        )
    return pod.container_names[0]


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, int, bool]:
    encoded = value.encode("utf-8")
    truncated = len(encoded) >= max_bytes
    if len(encoded) <= max_bytes:
        return value, len(encoded), truncated
    bounded = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return bounded, len(bounded.encode("utf-8")), True


def _is_sensitive_environment_name(name: str) -> bool:
    tokens = set(filter(None, re.split(r"[^A-Za-z0-9]+", name.upper())))
    return bool(tokens & _SENSITIVE_ENV_TOKENS) or "API_KEY" in name.upper()


def _required_object(value: JsonValue | None, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise KubernetesDataError(f"Kubernetes response is missing {field}")
    return value


def _optional_object(
    value: JsonValue | None,
    field: str,
) -> JsonObject | None:
    if value is None:
        return None
    return _required_object(value, field)


def _object_list(
    value: JsonValue | None,
    field: str,
    *,
    required: bool,
    limit: int,
) -> tuple[JsonObject, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return tuple(cast(JsonObject, item) for item in value[:limit])


def _required_text(
    value: JsonValue | None,
    field: str,
    *,
    max_chars: int = 253,
    allow_empty: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > max_chars
    ):
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value


def _optional_text(
    value: JsonValue | None,
    field: str,
) -> str | None:
    if value is None or value == "":
        return None
    return _required_text(value, field)


def _message(value: JsonValue | None, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value[:MAX_EVENT_MESSAGE_CHARS]


def _optional_message(
    value: JsonValue | None,
    field: str,
) -> str | None:
    if value is None:
        return None
    return _message(value, field)


def _optional_timestamp(
    value: JsonValue | None,
    field: str,
) -> str | None:
    if value is None:
        return None
    return _required_text(value, field, max_chars=64)


def _required_bool(value: JsonValue | None, field: str) -> bool:
    if not isinstance(value, bool):
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value


def _optional_bool(value: JsonValue | None, field: str) -> bool | None:
    if value is None:
        return None
    return _required_bool(value, field)


def _optional_int(value: JsonValue | None, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value


def _nonnegative_int(
    value: JsonValue | None,
    field: str,
    *,
    default: int | None = None,
) -> int:
    if value is None and default is not None:
        return default
    result = _optional_int(value, field)
    if result is None or result < 0:
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return result


def _positive_int(
    value: JsonValue | None,
    field: str,
    *,
    default: int,
) -> int:
    result = _nonnegative_int(value, field, default=default)
    if result < 1:
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return result


def _port(value: JsonValue | None, field: str) -> int | str:
    if isinstance(value, bool):
        raise KubernetesDataError(f"Kubernetes {field} port is invalid")
    if isinstance(value, int) and 1 <= value <= 65_535:
        return value
    if isinstance(value, str) and 1 <= len(value) <= 253:
        return value
    raise KubernetesDataError(f"Kubernetes {field} port is invalid")


def _string_map(value: JsonValue | None, field: str) -> dict[str, str]:
    if value is None:
        return {}
    mapping = _required_object(value, field)
    normalized: dict[str, str] = {}
    items: list[tuple[str, str]] = []
    for key, item in mapping.items():
        if (
            not isinstance(key, str)
            or not isinstance(item, str)
            or not key
            or not item
            or len(key) > 253
            or len(item) > 128
        ):
            raise KubernetesDataError(f"Kubernetes {field} is invalid")
        items.append((key, item))
    for key, item in sorted(items):
        normalized[key] = item
    return normalized
