"""Audited candidate generation with deterministic V1 verification authority."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from time import perf_counter

from pydantic import JsonValue

from opspilot.agent.schemas import BudgetState
from opspilot.agent.state import AgentStatus
from opspilot.diagnosis.models import DiagnosisAssessment, DiagnosisDraftV1, DiagnosisOutcome
from opspilot.diagnosis.verifier import BasicCrashLoopVerifier, DiagnosisInputError
from opspilot.errors import ErrorCode
from opspilot.evidence.models import Claim, Evidence
from opspilot.execution.models import ExecutionSummary
from opspilot.llm.audit import ModelAuditError, ModelAuditRepository, ModelCallAttempt
from opspilot.llm.budget import complete_with_budget, consume_retry, ensure_model_budget
from opspilot.llm.client import StructuredModelClient
from opspilot.llm.errors import ModelExternalError, ModelGatewayError, ModelSchemaError
from opspilot.llm.models import (
    ModelMessage, ModelRole, ModelUsage, PromptReference, PromptTemplate,
    StructuredModelConfig, StructuredModelRequest,
)
from opspilot.storage.contracts import EvidenceRepository, ResultRepository, ResultSnapshot, RunRepository

_SCHEMA_REGENERATION = "The prior candidate failed the requested JSON schema. Return one claim and cite only supplied Evidence IDs."


class V1DiagnosisAssembler:
    """Generate one candidate, validate references, then persist the verifier's result."""

    def __init__(
        self,
        *,
        client: StructuredModelClient,
        audit_repository: ModelAuditRepository,
        evidence_repository: EvidenceRepository,
        run_repository: RunRepository,
        result_repository: ResultRepository,
        prompt: PromptTemplate,
        model_config: StructuredModelConfig,
        verifier: BasicCrashLoopVerifier | None = None,
        max_schema_retries: int = 2,
    ) -> None:
        if prompt.component != "diagnosis":
            raise ValueError("V1DiagnosisAssembler requires a diagnosis prompt")
        if max_schema_retries < 0 or max_schema_retries > 2:
            raise ValueError("diagnosis schema retries must be between zero and two")
        self._client = client
        self._audit = audit_repository
        self._evidence = evidence_repository
        self._runs = run_repository
        self._results = result_repository
        self._prompt = prompt
        self._model_config = model_config
        self._verifier = verifier or BasicCrashLoopVerifier()
        self._max_schema_retries = max_schema_retries

    async def diagnose(
        self,
        *,
        run_id: str,
        result_id: str,
        execution: ExecutionSummary,
    ) -> DiagnosisOutcome:
        state = execution.state
        evidence = self._validated_evidence(run_id, execution)
        assert state.intent is not None
        ensure_model_budget(state.budget, phase="diagnosis.before")
        try:
            prompt_version_id = self._audit.register_prompt(self._prompt)
        except ModelAuditError:
            raise ModelExternalError("diagnosis prompt audit could not be persisted") from None

        messages = _messages(self._prompt, state.intent.model_dump(mode="json"), evidence)
        budget = state.budget
        call_ids: list[str] = []
        for attempt_index in range(self._max_schema_retries + 1):
            ensure_model_budget(budget, phase="diagnosis.before")
            request = StructuredModelRequest(
                messages=tuple(messages),
                prompt=PromptReference.from_template(self._prompt),
                config=self._model_config,
            )
            started_at = datetime.now(UTC)
            started_clock = perf_counter()
            try:
                response, budget = await complete_with_budget(
                    self._client, request, DiagnosisDraftV1, budget, phase="diagnosis",
                )
            except ModelGatewayError as exc:
                budget = exc.budget or budget
                completed_at = datetime.now(UTC)
                usage = exc.usage or ModelUsage()
                latency_ms = exc.latency_ms if exc.latency_ms is not None else max(0, round((perf_counter() - started_clock) * 1_000))
                call_ids.append(self._append_attempt(
                    run_id=run_id, prompt_version_id=prompt_version_id,
                    evidence=evidence, attempt_index=attempt_index,
                    started_at=started_at, completed_at=completed_at,
                    usage=usage, latency_ms=latency_ms,
                    model_version=exc.model_version, draft=None, error_code=exc.code,
                    budget=budget,
                ))
                if exc.budget_stop is not None:
                    raise
                ensure_model_budget(budget, phase="diagnosis.after", after=True)
                if not isinstance(exc, ModelSchemaError) or attempt_index >= self._max_schema_retries:
                    raise
                budget = consume_retry(budget, phase="diagnosis.retry")
                messages.append(ModelMessage(role=ModelRole.DEVELOPER, content=_SCHEMA_REGENERATION))
                continue

            completed_at = datetime.now(UTC)
            draft = response.output
            known = {item.evidence_id for item in evidence}
            invalid_refs = any(not set(claim.evidence_ids).issubset(known) for claim in draft.claims)
            call_ids.append(self._append_attempt(
                run_id=run_id, prompt_version_id=prompt_version_id,
                evidence=evidence, attempt_index=attempt_index,
                started_at=started_at, completed_at=completed_at,
                usage=response.metadata.usage,
                latency_ms=response.metadata.latency_ms,
                model_version=response.metadata.model_version,
                draft=draft,
                error_code=ErrorCode.POLICY_REJECTED if invalid_refs else None,
                budget=budget,
            ))
            ensure_model_budget(budget, phase="diagnosis.after", after=True)
            if invalid_refs:
                raise DiagnosisInputError("candidate cites unknown Evidence")

            assessment = self._verifier.verify(
                draft=draft, evidence=evidence, trace_id=state.trace_id,
                target=state.intent.target,
                execution_complete=execution.error is None and state.status is AgentStatus.VERIFYING,
            )
            self._persist_assessment(run_id, result_id, state.task_id, assessment)
            return DiagnosisOutcome(
                assessment=assessment, budget=budget,
                llm_call_ids=tuple(call_ids), result_id=result_id,
            )
        raise ModelSchemaError("diagnosis output failed schema validation")

    def partial_without_model(
        self, *, run_id: str, result_id: str, execution: ExecutionSummary,
    ) -> DiagnosisOutcome:
        """Verify retained facts after budget exhaustion without another model call."""

        state = execution.state
        if state.status is not AgentStatus.BUDGET_EXCEEDED:
            raise DiagnosisInputError("no-model Partial requires an exhausted run")
        evidence = self._validated_evidence(run_id, execution)
        assert state.intent is not None
        draft = DiagnosisDraftV1(
            schema_version=1,
            root_cause="The execution budget ended before diagnosis completed.",
            recommendation="Collect the missing observations before changing the workload.",
            claims=(Claim(
                claim_id=f"claim_{result_id}",
                text="The observed restart requires further verification.",
                evidence_ids=(evidence[0].evidence_id,),
                inference_confidence=0,
            ),),
        )
        assessment = self._verifier.verify(
            draft=draft, evidence=evidence, trace_id=state.trace_id,
            target=state.intent.target, execution_complete=False,
        )
        self._persist_assessment(run_id, result_id, state.task_id, assessment)
        return DiagnosisOutcome(
            assessment=assessment, budget=state.budget,
            llm_call_ids=(), result_id=result_id,
        )

    def _validated_evidence(
        self, run_id: str, execution: ExecutionSummary,
    ) -> tuple[Evidence, ...]:
        state = execution.state
        if state.intent is None or state.status not in {
            AgentStatus.VERIFYING, AgentStatus.EXECUTING,
            AgentStatus.BUDGET_EXCEEDED,
        }:
            raise DiagnosisInputError("diagnosis requires an executed V1 plan and routed Intent")
        run = self._runs.get_run(run_id)
        if run is None or run.task_id != state.task_id or run.state.trace_id != state.trace_id:
            raise DiagnosisInputError("diagnosis Run does not match the current Trace")
        if state.evidence_ids != execution.evidence_ids:
            raise DiagnosisInputError("execution summary differs from AgentState Evidence")
        evidence = self._evidence.evidence_for_trace(state.trace_id)
        if (
            not evidence
            or {item.evidence_id for item in evidence} != set(execution.evidence_ids)
            or len(evidence) != len(execution.evidence_ids)
        ):
            raise DiagnosisInputError("stored Evidence differs from the execution summary")
        if any(item.trace_id != state.trace_id for item in evidence):
            raise DiagnosisInputError("stored Evidence belongs to another Trace")
        return evidence

    def _persist_assessment(
        self, run_id: str, result_id: str, task_id: str,
        assessment: DiagnosisAssessment,
    ) -> None:
        self._results.append_result(ResultSnapshot(
            result_id=result_id, task_id=task_id, run_id=run_id,
            status=assessment.status, root_cause=assessment.root_cause,
            recommendation=assessment.recommendation, confidence=assessment.confidence,
            claims_payload=(assessment.claim.model_dump(mode="json"),),
            verification_payload=assessment.verification.model_dump(mode="json"),
        ))

    def _append_attempt(
        self,
        *,
        run_id: str,
        prompt_version_id: str,
        evidence: tuple[Evidence, ...],
        attempt_index: int,
        started_at: datetime,
        completed_at: datetime,
        usage: ModelUsage,
        latency_ms: int,
        model_version: str | None,
        draft: DiagnosisDraftV1 | None,
        error_code: ErrorCode | None,
        budget: BudgetState,
    ) -> str:
        response_payload: dict[str, JsonValue] | None = None
        if draft is not None:
            response_payload = {
                "candidate_sha256": sha256(draft.model_dump_json().encode("utf-8")).hexdigest(),
                "claim_ids": [claim.claim_id for claim in draft.claims],
                "cited_evidence_ids": [item for claim in draft.claims for item in claim.evidence_ids],
            }
        try:
            recorded = self._audit.append_attempt(ModelCallAttempt(
                run_id=run_id, component="diagnosis", prompt_version_id=prompt_version_id,
                provider=self._model_config.provider, model_name=self._model_config.model,
                model_version=model_version or self._model_config.model_version,
                request_payload={
                    "output_schema": DiagnosisDraftV1.__name__,
                    "evidence_ids": [item.evidence_id for item in evidence],
                    "attempt": attempt_index + 1,
                },
                response_payload=response_payload,
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd, latency_ms=latency_ms,
                retry_count=attempt_index, success=error_code is None,
                error_code=error_code, started_at=started_at,
                completed_at=completed_at,
            ))
        except ModelAuditError:
            raise ModelExternalError("diagnosis model audit could not be persisted", budget=budget) from None
        return recorded.call_id


def _messages(prompt: PromptTemplate, intent: dict[str, JsonValue], evidence: tuple[Evidence, ...]) -> list[ModelMessage]:
    observations = [
        {
            "evidence_id": item.evidence_id,
            "source": item.source,
            "resource": item.resource,
            "observed_at": item.observed_at.isoformat(),
            "attributes": [attribute.model_dump(mode="json") for attribute in item.attributes],
        }
        for item in evidence
    ]
    payload = json.dumps({"intent": intent, "evidence": observations}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return [ModelMessage(role=ModelRole.SYSTEM, content=prompt.content), ModelMessage(role=ModelRole.USER, content=payload)]
