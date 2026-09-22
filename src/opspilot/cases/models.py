"""Validated contracts and loading helpers for deterministic diagnosis cases."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from opspilot.agent.schemas import StrictSchema, Target
from opspilot.evidence import Claim, Evidence, Verification
from opspilot.tools import ToolInvocation, ToolResponse

FixturePath: TypeAlias = str
RequiredSignal: TypeAlias = Literal[
    "restart_count",
    "liveness_failure",
    "startup_duration",
    "probe_configuration",
]

REQUIRED_SIGNALS: frozenset[str] = frozenset(
    {
        "restart_count",
        "liveness_failure",
        "startup_duration",
        "probe_configuration",
    }
)
_CASE_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
_TRACE_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
_FIXTURE_PATH_PATTERN = r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$"


def _validate_fixture_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError("fixture paths must stay inside the case directory")
    return value


class ReplayStep(StrictSchema):
    """One deterministic Tool invocation and its recorded response."""

    phase: Literal["fault", "recovery"]
    invocation: ToolInvocation
    response_file: FixturePath = Field(pattern=_FIXTURE_PATH_PATTERN)

    _path_within_case = field_validator("response_file")(_validate_fixture_path)


class EvidenceRequirement(StrictSchema):
    """A required diagnostic signal and the Evidence record satisfying it."""

    signal: RequiredSignal
    evidence_id: str = Field(
        min_length=4,
        max_length=128,
        pattern=r"^ev_[a-z0-9][a-z0-9_-]*$",
    )


class ExpectedDiagnosis(StrictSchema):
    """Ground truth used by later end-to-end and evaluation tests."""

    status: Literal["COMPLETED", "PARTIAL"]
    root_cause: str = Field(min_length=1, max_length=4_000)
    recommendation: str = Field(min_length=1, max_length=4_000)
    claim: Claim
    verification: Verification

    @model_validator(mode="after")
    def align_claim_and_verification(self) -> ExpectedDiagnosis:
        if self.claim.claim_id != self.verification.claim_id:
            raise ValueError("verification must refer to the expected claim")
        return self


class RecoveryExpectation(StrictSchema):
    """Signals that prove the repaired workload remains healthy."""

    response_file: FixturePath = Field(pattern=_FIXTURE_PATH_PATTERN)
    phase: Literal["Running"]
    ready: Literal[True]
    max_restart_count: int = Field(ge=0)

    _path_within_case = field_validator("response_file")(_validate_fixture_path)


class CaseDefinition(StrictSchema):
    """Versioned source-of-truth for a reproducible diagnostic case."""

    schema_version: Literal[1]
    case_id: str = Field(min_length=3, max_length=128, pattern=_CASE_ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=3, max_length=128, pattern=_TRACE_ID_PATTERN)
    target: Target
    startup_duration_seconds: int = Field(gt=0, le=3_600)
    broken_manifest: FixturePath = Field(pattern=_FIXTURE_PATH_PATTERN)
    fixed_manifest: FixturePath = Field(pattern=_FIXTURE_PATH_PATTERN)
    replay_steps: tuple[ReplayStep, ...] = Field(min_length=1, max_length=32)
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=100)
    evidence_requirements: tuple[EvidenceRequirement, ...] = Field(
        min_length=1,
        max_length=20,
    )
    expected_diagnosis: ExpectedDiagnosis
    recovery: RecoveryExpectation

    _broken_path_within_case = field_validator("broken_manifest")(
        _validate_fixture_path
    )
    _fixed_path_within_case = field_validator("fixed_manifest")(
        _validate_fixture_path
    )

    @model_validator(mode="after")
    def validate_trace_and_ground_truth(self) -> CaseDefinition:
        if self.broken_manifest == self.fixed_manifest:
            raise ValueError("broken and fixed manifests must be distinct")

        call_ids = [step.invocation.call_id for step in self.replay_steps]
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("replay Tool call IDs must be unique")

        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("case Evidence IDs must be unique")

        if any(item.trace_id != self.trace_id for item in self.evidence):
            raise ValueError("all Evidence must belong to the case trace")

        fault_call_ids = {
            step.invocation.call_id
            for step in self.replay_steps
            if step.phase == "fault"
        }
        if any(item.tool_call_id not in fault_call_ids for item in self.evidence):
            raise ValueError("Evidence must refer to a fault-phase Tool call")

        requirement_signals = {
            requirement.signal for requirement in self.evidence_requirements
        }
        if requirement_signals != REQUIRED_SIGNALS:
            raise ValueError("case must cover every required CrashLoopBackOff signal")
        if len(requirement_signals) != len(self.evidence_requirements):
            raise ValueError("evidence requirement signals must be unique")

        available_evidence_ids = set(evidence_ids)
        required_evidence_ids = {
            requirement.evidence_id for requirement in self.evidence_requirements
        }
        claim_evidence_ids = set(self.expected_diagnosis.claim.evidence_ids)
        checked_evidence_ids = set(
            self.expected_diagnosis.verification.checked_evidence_ids
        )
        if not required_evidence_ids.issubset(available_evidence_ids):
            raise ValueError("evidence requirements must refer to case Evidence")
        if not required_evidence_ids.issubset(claim_evidence_ids):
            raise ValueError("expected claim must cite every required Evidence")
        if not claim_evidence_ids.issubset(available_evidence_ids):
            raise ValueError("expected claim cites unknown Evidence")
        if not claim_evidence_ids.issubset(checked_evidence_ids):
            raise ValueError("expected claim Evidence must be verified")

        recovery_files = {
            step.response_file
            for step in self.replay_steps
            if step.phase == "recovery"
        }
        if self.recovery.response_file not in recovery_files:
            raise ValueError("recovery expectation must refer to a recovery step")
        return self


@dataclass(frozen=True, slots=True)
class LoadedCase:
    """A validated case plus its ordered, normalized replay responses."""

    definition: CaseDefinition
    root: Path
    responses: tuple[ToolResponse, ...]

    def response_for(self, call_id: str) -> ToolResponse:
        for response in self.responses:
            if response.metadata.call_id == call_id:
                return response
        raise KeyError(call_id)

    def read_json(self, reference: FixturePath) -> dict[str, object]:
        return _read_json_object(_resolve_reference(self.root, reference))


def _resolve_reference(root: Path, reference: FixturePath) -> Path:
    resolved_root = root.resolve()
    resolved = (resolved_root / reference).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("fixture reference escapes the case directory")
    if not resolved.is_file():
        raise ValueError(f"fixture reference does not exist: {reference}")
    return resolved


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"fixture must contain a JSON object: {path.name}")
    return value


def load_case(case_file: Path) -> LoadedCase:
    """Load a complete case and reject broken or mismatched replay references."""

    case_path = case_file.resolve()
    definition = CaseDefinition.model_validate_json(
        case_path.read_text(encoding="utf-8"),
        strict=True,
    )
    root = case_path.parent

    _read_json_object(_resolve_reference(root, definition.broken_manifest))
    _read_json_object(_resolve_reference(root, definition.fixed_manifest))

    responses: list[ToolResponse] = []
    for step in definition.replay_steps:
        response_path = _resolve_reference(root, step.response_file)
        response = ToolResponse.model_validate_json(
            response_path.read_text(encoding="utf-8"),
            strict=True,
        )
        if response.metadata.call_id != step.invocation.call_id:
            raise ValueError("replay response call_id does not match its invocation")
        if response.metadata.tool_name != step.invocation.tool:
            raise ValueError("replay response tool name does not match its invocation")
        responses.append(response)

    return LoadedCase(
        definition=definition,
        root=root,
        responses=tuple(responses),
    )
