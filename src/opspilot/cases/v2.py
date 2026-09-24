"""Versioned multi-fault case and strict branch replay contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.cases.models import ExpectedDiagnosis, LoadedCase, load_case
from opspilot.evidence import Evidence
from opspilot.evidence.models import EvidenceId
from opspilot.faults import FaultFamilyV2, TargetKindV2
from opspilot.integrations.kubernetes.models import NamespaceName, ResourceName
from opspilot.tools import ToolInvocation, ToolResponse
from opspilot.tools.models import TOOL_NAME_PATTERN

_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
_SIGNAL_PATTERN = r"^[a-z][a-z0-9_.-]{2,127}$"
_PATH_PATTERN = r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$"
_TARGET_ARGUMENTS = frozenset(
    {"pod_name", "workload_name", "deployment_name", "service_name", "ingress_name"}
)


def _safe_reference(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError("fixture paths must stay inside the case directory")
    return value


class CaseTargetV2(StrictSchema):
    namespace: NamespaceName
    kind: TargetKindV2
    resource: ResourceName


class CaseSignalV2(StrictSchema):
    signal: str = Field(min_length=3, max_length=128, pattern=_SIGNAL_PATTERN)
    evidence_id: EvidenceId | None


class ReplayStepV2(StrictSchema):
    phase: Literal["fault", "recovery"]
    invocation: ToolInvocation
    response_file: str = Field(min_length=1, max_length=256, pattern=_PATH_PATTERN)

    _path_within_case = field_validator("response_file")(_safe_reference)


class RecoveryExpectationV2(StrictSchema):
    response_file: str = Field(min_length=1, max_length=256, pattern=_PATH_PATTERN)
    checks: dict[str, bool | int | float | str] = Field(min_length=1, max_length=20)

    _path_within_case = field_validator("response_file")(_safe_reference)


class ReplayBranchV2(StrictSchema):
    branch_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    fault_family: FaultFamilyV2
    steps: tuple[ReplayStepV2, ...] = Field(min_length=1, max_length=32)
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=100)
    required_signals: tuple[CaseSignalV2, ...] = Field(min_length=1, max_length=20)
    contradiction_signals: tuple[str, ...] = Field(default=(), max_length=20)
    expected_diagnosis: ExpectedDiagnosis
    recovery: RecoveryExpectationV2 | None = None

    @field_validator("contradiction_signals")
    @classmethod
    def validate_contradiction_signals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("contradiction signals must be unique")
        return value

    @model_validator(mode="after")
    def validate_ground_truth(self) -> ReplayBranchV2:
        call_ids = [step.invocation.call_id for step in self.steps]
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("branch call IDs must be unique")
        fault_ids = {
            step.invocation.call_id for step in self.steps if step.phase == "fault"
        }
        if not fault_ids:
            raise ValueError("branch requires at least one fault-phase call")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("branch Evidence IDs must be unique")
        if any(item.tool_call_id not in fault_ids for item in self.evidence):
            raise ValueError("Evidence must cite a fault-phase call")
        signals = [item.signal for item in self.required_signals]
        if len(set(signals)) != len(signals):
            raise ValueError("required signals must be unique")
        available = set(evidence_ids)
        grounded = {
            item.evidence_id
            for item in self.required_signals
            if item.evidence_id is not None
        }
        if not grounded.issubset(available):
            raise ValueError("required signal cites unknown Evidence")
        claim_ids = set(self.expected_diagnosis.claim.evidence_ids)
        checked_ids = set(
            self.expected_diagnosis.verification.checked_evidence_ids
        )
        if not grounded.issubset(claim_ids):
            raise ValueError("required Evidence must support the expected claim")
        if not claim_ids.issubset(available) or not claim_ids.issubset(checked_ids):
            raise ValueError("expected claim cites unchecked or unknown Evidence")
        if self.expected_diagnosis.status == "COMPLETED":
            if (
                not self.expected_diagnosis.verification.supported
                or any(item.evidence_id is None for item in self.required_signals)
            ):
                raise ValueError("completed branch requires all supported signals")
        elif self.expected_diagnosis.verification.supported:
            raise ValueError("partial branch cannot have supported verification")
        missing_signals = {
            item.signal for item in self.required_signals if item.evidence_id is None
        }
        reported_missing = {
            item.requirement
            for item in self.expected_diagnosis.verification.missing_evidence
        }
        if not missing_signals.issubset(reported_missing):
            raise ValueError("missing signals must be reported by verification")
        if self.recovery is not None and self.recovery.response_file not in {
            step.response_file for step in self.steps if step.phase == "recovery"
        }:
            raise ValueError("recovery must cite a recovery-phase response")
        return self


class CaseDefinitionV2(StrictSchema):
    schema_version: Literal[2]
    case_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=4_000)
    trace_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    target: CaseTargetV2
    candidate_families: tuple[FaultFamilyV2, ...] = Field(min_length=1, max_length=8)
    allowed_tools: tuple[str, ...] = Field(min_length=1, max_length=32)
    allowed_sources: tuple[str, ...] = Field(min_length=1, max_length=16)
    allowed_evidence_sources: tuple[str, ...] = Field(min_length=1, max_length=32)
    allowed_resource_names: tuple[ResourceName, ...] = Field(
        min_length=1, max_length=64
    )
    branches: tuple[ReplayBranchV2, ...] = Field(min_length=1, max_length=16)

    @field_validator("allowed_tools")
    @classmethod
    def validate_tool_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(TOOL_NAME_PATTERN, name) is None for name in value):
            raise ValueError("allowed Tool name is invalid")
        return value

    @field_validator("allowed_sources", "allowed_evidence_sources")
    @classmethod
    def validate_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            re.fullmatch(r"^[a-z][a-z0-9_-]{0,63}$", name) is None
            for name in value
        ):
            raise ValueError("allowed source name is invalid")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> CaseDefinitionV2:
        if self.target.resource not in self.allowed_resource_names:
            raise ValueError("case target must be an allowed resource")
        if len(set(self.candidate_families)) != len(self.candidate_families):
            raise ValueError("candidate families must be unique")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed Tools must be unique")
        if len(set(self.allowed_sources)) != len(self.allowed_sources):
            raise ValueError("allowed sources must be unique")
        if len(set(self.allowed_evidence_sources)) != len(
            self.allowed_evidence_sources
        ):
            raise ValueError("allowed Evidence sources must be unique")
        if len(set(self.allowed_resource_names)) != len(self.allowed_resource_names):
            raise ValueError("allowed resources must be unique")
        if len({branch.branch_id for branch in self.branches}) != len(self.branches):
            raise ValueError("branch IDs must be unique")
        if not set(self.candidate_families).issuperset(
            branch.fault_family for branch in self.branches
        ):
            raise ValueError("branch family is outside candidate scope")
        for branch in self.branches:
            if any(item.trace_id != self.trace_id for item in branch.evidence):
                raise ValueError("branch Evidence must belong to case trace")
            if any(
                item.source not in self.allowed_evidence_sources
                for item in branch.evidence
            ):
                raise ValueError("branch Evidence source is outside case scope")
            for step in branch.steps:
                invocation = step.invocation
                if invocation.tool not in self.allowed_tools:
                    raise ValueError("branch names a Tool outside case scope")
                if invocation.arguments.get("namespace") != self.target.namespace:
                    raise ValueError("branch crosses case namespace")
                for key in _TARGET_ARGUMENTS:
                    value = invocation.arguments.get(key)
                    if value is not None and value not in self.allowed_resource_names:
                        raise ValueError("branch names a resource outside case scope")
        return self


class ReplayMismatchV2(ValueError):
    """A branch was invoked out of order or outside its declared scope."""


@dataclass(frozen=True, slots=True)
class LoadedBranchV2:
    definition: ReplayBranchV2
    responses: tuple[ToolResponse, ...]


@dataclass(frozen=True, slots=True)
class LoadedCaseV2:
    definition: CaseDefinitionV2
    root: Path
    branches: tuple[LoadedBranchV2, ...]

    def open_branch(self, branch_id: str) -> ReplaySessionV2:
        for branch in self.branches:
            if branch.definition.branch_id == branch_id:
                return ReplaySessionV2(branch)
        raise ReplayMismatchV2("replay branch is not declared")


class ReplaySessionV2:
    """One fresh, fault-phase replay cursor for an explicitly chosen branch."""

    def __init__(self, branch: LoadedBranchV2) -> None:
        self._steps = tuple(
            (step, response)
            for step, response in zip(
                branch.definition.steps, branch.responses, strict=True
            )
            if step.phase == "fault"
        )
        self._cursor = 0

    def next_response(self, invocation: ToolInvocation) -> ToolResponse:
        if self._cursor >= len(self._steps):
            raise ReplayMismatchV2("replay branch has no remaining fault calls")
        step, response = self._steps[self._cursor]
        if invocation != step.invocation:
            raise ReplayMismatchV2("replay invocation does not match branch step")
        self._cursor += 1
        return response.model_copy(deep=True)

    def assert_complete(self) -> None:
        if self._cursor != len(self._steps):
            raise ReplayMismatchV2("replay branch has unconsumed fault calls")


def _resolve_reference(root: Path, reference: str) -> Path:
    resolved_root = root.resolve()
    resolved = (resolved_root / reference).resolve()
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise ValueError("fixture reference is missing or escapes case directory")
    return resolved


def load_case_v2(case_file: Path) -> LoadedCaseV2:
    definition = CaseDefinitionV2.model_validate_json(
        case_file.read_text(encoding="utf-8"), strict=True
    )
    root = case_file.resolve().parent
    loaded: list[LoadedBranchV2] = []
    for branch in definition.branches:
        responses: list[ToolResponse] = []
        for step in branch.steps:
            response = ToolResponse.model_validate_json(
                _resolve_reference(root, step.response_file).read_text(
                    encoding="utf-8"
                ),
                strict=True,
            )
            if (
                response.metadata.call_id != step.invocation.call_id
                or response.metadata.tool_name != step.invocation.tool
                or response.metadata.source not in definition.allowed_sources
            ):
                raise ValueError("replay response metadata does not match scope")
            responses.append(response)
        successful_fault_calls = {
            step.invocation.call_id
            for step, response in zip(branch.steps, responses, strict=True)
            if step.phase == "fault" and response.success
        }
        if any(
            item.tool_call_id not in successful_fault_calls
            for item in branch.evidence
        ):
            raise ValueError("Evidence cannot cite a failed Tool response")
        loaded.append(LoadedBranchV2(branch, tuple(responses)))
    return LoadedCaseV2(definition, root, tuple(loaded))


def load_case_versioned(case_file: Path) -> LoadedCase | LoadedCaseV2:
    """Dispatch by explicit schema version, never by filename or directory."""

    payload = json.loads(case_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("case file must contain an object")
    version = payload.get("schema_version")
    if type(version) is int and version == 1:
        return load_case(case_file)
    if type(version) is int and version == 2:
        return load_case_v2(case_file)
    raise ValueError("unsupported case schema version")
