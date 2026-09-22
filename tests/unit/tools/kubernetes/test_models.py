import pytest
from pydantic import ValidationError

from opspilot.tools.kubernetes import (
    EnvironmentOutput,
    PodLogsInput,
    PodStatusInput,
    PreviousPodLogsInput,
    ProbeOutput,
)


def test_pod_inputs_require_exactly_one_target() -> None:
    assert PodStatusInput(namespace="team-a", pod_name="api").pod_name == "api"
    assert (
        PodStatusInput(namespace="team-a", workload_name="api").workload_name
        == "api"
    )

    with pytest.raises(ValidationError, match="exactly one"):
        PodStatusInput(namespace="team-a")
    with pytest.raises(ValidationError, match="exactly one"):
        PodStatusInput(
            namespace="team-a",
            pod_name="api-pod",
            workload_name="api",
        )


def test_log_inputs_are_independent_and_bounded() -> None:
    current = PodLogsInput(namespace="team-a", pod_name="api-pod")
    previous = PreviousPodLogsInput(namespace="team-a", pod_name="api-pod")

    assert type(current) is PodLogsInput
    assert type(previous) is PreviousPodLogsInput
    assert current.max_bytes == 65_536

    with pytest.raises(ValidationError):
        PodLogsInput(
            namespace="team-a",
            pod_name="api-pod",
            tail_lines=1_001,
        )
    with pytest.raises(ValidationError):
        PodLogsInput(
            namespace="team-a",
            pod_name="api-pod",
            max_bytes=65_537,
        )


def test_environment_contract_cannot_expose_redacted_value() -> None:
    with pytest.raises(ValidationError, match="cannot be included"):
        EnvironmentOutput(
            name="DATABASE_PASSWORD",
            kind="literal",
            value="secret",
            redacted=True,
        )

    with pytest.raises(ValidationError, match="must be marked redacted"):
        EnvironmentOutput(
            name="DATABASE_PASSWORD",
            kind="secret_key_ref",
            reference_name="database",
            reference_key="password",
        )


def test_probe_contract_requires_kind_specific_target() -> None:
    with pytest.raises(ValidationError, match="requires path and port"):
        ProbeOutput(kind="http", path="/healthz")

    assert ProbeOutput(kind="tcp", port=8080).port == 8080
    assert ProbeOutput(kind="exec", command=("check",)).command == ("check",)


def test_tool_input_schemas_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PodStatusInput.model_validate(
            {
                "namespace": "team-a",
                "pod_name": "api",
                "label_selector": "app=*",
            }
        )
