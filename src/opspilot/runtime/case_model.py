"""Deterministic structured model responses for the registered offline Case."""

from __future__ import annotations

import json
from typing import cast

from pydantic import BaseModel, JsonValue

from opspilot.agent.schemas import ExecutableStepV1, ExecutionPlanV1
from opspilot.cases import LoadedCase
from opspilot.diagnosis import DiagnosisDraftV1
from opspilot.evidence import Claim
from opspilot.llm.models import (
    ModelResponseMetadata, ModelUsage, OutputT, StructuredModelRequest,
    StructuredModelResult,
)
from opspilot.routing.models import RouterModelOutput


class CaseModelClient:
    """Return Case-grounded candidates; the ordinary Verifier still decides facts."""

    def __init__(self, case: LoadedCase) -> None:
        self._case = case

    async def complete(
        self, request: StructuredModelRequest, output_model: type[OutputT],
    ) -> StructuredModelResult[OutputT]:
        resource = self._case.definition.target.resource
        namespace = self._case.definition.target.namespace
        if request.prompt.component == "router" and output_model is RouterModelOutput:
            output: BaseModel = RouterModelOutput(
                intent="diagnose", domain="kubernetes",
                problem_type="pod_restart", resource=resource,
            )
        elif request.prompt.component == "planner" and output_model is ExecutionPlanV1:
            pod: dict[str, JsonValue] = {"namespace": namespace, "workload_name": resource}
            output = ExecutionPlanV1(schema_version=1, steps=(
                ExecutableStepV1(step_id=1, call_id="call_pod_status", tool="k8s.get_pod_status", arguments=pod, reason="Read Pod restarts"),
                ExecutableStepV1(step_id=2, call_id="call_pod_events", tool="k8s.get_pod_events", arguments=pod, reason="Read probe events"),
                ExecutableStepV1(step_id=3, call_id="call_previous_logs", tool="k8s.get_previous_logs", arguments={**pod, "container_name": resource}, reason="Read previous logs"),
                ExecutableStepV1(step_id=4, call_id="call_deployment", tool="k8s.get_deployment", arguments={"namespace": namespace, "deployment_name": resource}, reason="Read probe configuration"),
            ))
        elif request.prompt.component == "diagnosis" and output_model is DiagnosisDraftV1:
            payload = json.loads(request.messages[-1].content)
            evidence_ids = tuple(
                item["evidence_id"] for item in payload["evidence"]
            )
            expected = self._case.definition.expected_diagnosis
            output = DiagnosisDraftV1(
                schema_version=1, root_cause=expected.root_cause,
                recommendation=expected.recommendation,
                claims=(Claim(
                    claim_id=expected.claim.claim_id, text=expected.claim.text,
                    evidence_ids=evidence_ids,
                    inference_confidence=expected.claim.inference_confidence,
                ),),
            )
        else:
            raise ValueError("unsupported Case model component")
        return StructuredModelResult[OutputT](
            output=cast(OutputT, output),
            metadata=ModelResponseMetadata(
                provider=request.config.provider, model=request.config.model,
                latency_ms=0, usage=ModelUsage(),
            ),
        )
