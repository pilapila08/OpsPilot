"""Deterministic observations from the five normalized Kubernetes responses."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from pydantic import ValidationError

from opspilot.evidence.models import Evidence, EvidenceAttribute
from opspilot.tools.kubernetes.models import (
    DeploymentOutput,
    PodEventsOutput,
    PodLogsOutput,
    PodStatusOutput,
)
from opspilot.tools.models import ToolInvocation, ToolResponse

_BOOT = re.compile(
    r"(?m)^(\S+) application boot started; configured_startup_delay_seconds=(\d{1,4})$"
)
_TERMINATED = re.compile(
    r"(?m)^(\S+) process terminated before application ready$"
)


class EvidenceExtractionError(ValueError):
    """The purported successful Tool result cannot support safe Evidence."""


@dataclass(frozen=True, slots=True)
class _Observation:
    source: str
    resource: str
    observed_at: datetime
    content: str
    attributes: tuple[EvidenceAttribute, ...]


class EvidenceExtractorRegistry:
    """Map validated Tool outputs to bounded, source-grounded observations."""

    def extract(
        self,
        *,
        invocation: ToolInvocation,
        response: ToolResponse,
        trace_id: str,
        tool_attempt_id: str,
        collected_at: datetime,
        evidence_id_factory: Callable[[], str],
    ) -> tuple[Evidence, ...]:
        if not response.success or response.data is None:
            raise EvidenceExtractionError("Evidence requires a successful Tool response")
        if response.metadata.call_id != invocation.call_id or response.metadata.tool_name != invocation.tool:
            raise EvidenceExtractionError("Tool response identity does not match invocation")
        if collected_at.tzinfo is None or collected_at.utcoffset() is None:
            raise EvidenceExtractionError("Evidence collection time must be timezone-aware")
        if response.data.get("namespace") != invocation.arguments.get("namespace"):
            raise EvidenceExtractionError("Tool response namespace differs from invocation")
        payload = json.dumps(response.data)
        try:
            if invocation.tool == "k8s.get_pod_status":
                observations = _status(PodStatusOutput.model_validate_json(payload), collected_at)
            elif invocation.tool == "k8s.get_pod_events":
                observations = _events(PodEventsOutput.model_validate_json(payload), collected_at)
            elif invocation.tool == "k8s.get_previous_logs":
                logs = PodLogsOutput.model_validate_json(payload)
                if not logs.previous:
                    raise EvidenceExtractionError("previous logs response has wrong stream")
                observations = _previous_logs(logs, collected_at)
            elif invocation.tool == "k8s.get_pod_logs":
                logs = PodLogsOutput.model_validate_json(payload)
                if logs.previous:
                    raise EvidenceExtractionError("current logs response has wrong stream")
                observations = _current_logs(logs, collected_at)
            elif invocation.tool == "k8s.get_deployment":
                deployment = DeploymentOutput.model_validate_json(payload)
                if deployment.name != invocation.arguments.get("deployment_name"):
                    raise EvidenceExtractionError("Deployment identity differs from invocation")
                observations = _deployment(deployment, collected_at)
            else:
                raise EvidenceExtractionError("no Evidence extractor exists for Tool")
        except ValidationError:
            raise EvidenceExtractionError("Tool data failed Evidence input validation") from None

        return tuple(
            Evidence(
                evidence_id=evidence_id_factory(),
                trace_id=trace_id,
                tool_call_id=tool_attempt_id,
                source=item.source,
                resource=item.resource,
                observed_at=item.observed_at,
                collected_at=collected_at,
                content=item.content,
                source_confidence=1.0,
                raw_result_ref=tool_attempt_id,
                attributes=item.attributes,
            )
            for item in observations
        )


def _status(data: PodStatusOutput, collected_at: datetime) -> tuple[_Observation, ...]:
    observations: list[_Observation] = []
    for container in data.containers:
        if container.restart_count == 0 and container.reason != "CrashLoopBackOff":
            continue
        attributes = [
            EvidenceAttribute(key="restart_count", value=container.restart_count),
            EvidenceAttribute(key="container_state", value=container.reason or container.state),
        ]
        if container.last_exit_code is not None:
            attributes.append(EvidenceAttribute(key="last_exit_code", value=container.last_exit_code))
        if container.last_reason is not None:
            attributes.append(EvidenceAttribute(key="last_reason", value=container.last_reason))
        observations.append(
            _Observation(
                source="kubernetes_status",
                resource=f"{data.namespace}/{data.pod_name}",
                observed_at=_observed(container.last_finished_at, collected_at),
                content=(
                    f"Container {container.name} has restart count {container.restart_count} "
                    f"and state {container.reason or container.state}."
                ),
                attributes=tuple(attributes),
            )
        )
    return tuple(observations)


def _events(data: PodEventsOutput, collected_at: datetime) -> tuple[_Observation, ...]:
    observations: list[_Observation] = []
    for event in data.events:
        liveness = event.reason == "Unhealthy" and "liveness" in event.message.lower()
        if not liveness and event.reason not in {"Killing", "BackOff"}:
            continue
        attributes = (
            EvidenceAttribute(key="event_reason", value=event.reason),
            EvidenceAttribute(key="failure_count", value=event.count),
            EvidenceAttribute(key="liveness_failure", value=liveness),
        )
        observations.append(
            _Observation(
                source="kubernetes_events",
                resource=f"{data.namespace}/{data.pod_name}",
                observed_at=_observed(event.last_timestamp, collected_at),
                content=(
                    "Kubernetes reported liveness probe failures."
                    if liveness
                    else f"Kubernetes reported event {event.reason}."
                ),
                attributes=attributes,
            )
        )
    return tuple(observations)


def _previous_logs(data: PodLogsOutput, collected_at: datetime) -> tuple[_Observation, ...]:
    boot = _BOOT.search(data.content)
    terminated = _TERMINATED.search(data.content)
    if boot is None or terminated is None:
        return ()
    started_at = _parse_time(boot.group(1))
    ended_at = _parse_time(terminated.group(1))
    if started_at is None or ended_at is None or ended_at < started_at:
        return ()
    configured = int(boot.group(2))
    if configured > 3_600:
        return ()
    elapsed = int((ended_at - started_at).total_seconds())
    return (
        _Observation(
            source="kubernetes_logs",
            resource=f"{data.namespace}/{data.pod_name}:{data.container}",
            observed_at=min(ended_at, collected_at),
            content=(
                f"Previous logs declare a {configured} second startup delay and "
                f"termination after {elapsed} seconds before readiness."
            ),
            attributes=(
                EvidenceAttribute(key="startup_duration_seconds", value=configured),
                EvidenceAttribute(key="terminated_after_seconds", value=elapsed),
            ),
        ),
    )


def _current_logs(data: PodLogsOutput, collected_at: datetime) -> tuple[_Observation, ...]:
    if not data.content.strip():
        return ()
    return (
        _Observation(
            source="kubernetes_logs",
            resource=f"{data.namespace}/{data.pod_name}:{data.container}",
            observed_at=collected_at,
            content=f"Current container logs contain {data.byte_count} bytes.",
            attributes=(
                EvidenceAttribute(key="byte_count", value=data.byte_count),
                EvidenceAttribute(key="truncated", value=data.truncated),
            ),
        ),
    )


def _deployment(data: DeploymentOutput, collected_at: datetime) -> tuple[_Observation, ...]:
    observations: list[_Observation] = []
    for container in data.containers:
        probe = container.liveness_probe
        if probe is None:
            continue
        observations.append(
            _Observation(
                source="kubernetes_deployment",
                resource=f"{data.namespace}/deployment/{data.name}",
                observed_at=collected_at,
                content=(
                    f"Container {container.name} liveness starts after "
                    f"{probe.initial_delay_seconds} seconds, checks every "
                    f"{probe.period_seconds} seconds, and has failure threshold "
                    f"{probe.failure_threshold}; startup probe configured: "
                    f"{container.startup_probe is not None}."
                ),
                attributes=(
                    EvidenceAttribute(key="initial_delay_seconds", value=probe.initial_delay_seconds),
                    EvidenceAttribute(key="period_seconds", value=probe.period_seconds),
                    EvidenceAttribute(key="failure_threshold", value=probe.failure_threshold),
                    EvidenceAttribute(key="startup_probe_configured", value=container.startup_probe is not None),
                ),
            )
        )
    return tuple(observations)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _observed(value: str | None, collected_at: datetime) -> datetime:
    parsed = _parse_time(value) if value is not None else None
    return min(parsed, collected_at) if parsed is not None else collected_at
