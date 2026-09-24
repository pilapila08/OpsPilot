"""Run the bundled cases offline through the real runtime and persisted Trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from opspilot.agent.schemas import BudgetLimits
from opspilot.cases import LoadedCaseV2, load_case, load_case_v2
from opspilot.diagnosis import V1DiagnosisAssembler
from opspilot.diagnosis.oom_v2 import OomKilledVerifierV2
from opspilot.llm.models import (
    ModelResponseMetadata,
    ModelUsage,
    OutputT,
    StructuredModelConfig,
    StructuredModelRequest,
    StructuredModelResult,
)
from opspilot.llm.prompts import load_prompt
from opspilot.planning import PlanCallV2, PlanDecisionV2, V1Planner, V2Planner
from opspilot.planning.v2 import ObservationSummaryV2
from opspilot.routing import IntentRouter, V2IntentRouter, V2RouterModelOutput
from opspilot.runtime.case_model import CaseModelClient
from opspilot.runtime.models import DiagnosisRequest
from opspilot.runtime.orchestrator import DiagnosisRuntime
from opspilot.runtime.replay import ReplayRegistryFactory
from opspilot.runtime.replay_v2 import ReplayV2Registry
from opspilot.runtime.v2 import ObservationRuntimeV2, V2DiagnosisRequest
from opspilot.storage import (
    SQLAlchemyExecutionRepository,
    SQLAlchemyModelAuditRepository,
    SQLAlchemyPlanningRoundRepository,
    SQLAlchemyRuntimeRepository,
)
from opspilot.tracing import TraceView, query_trace


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CRASHLOOP_CASE = _PROJECT_ROOT / "fixtures/cases/crashloop-liveness-v1/case.json"
_OOM_CASE = _PROJECT_ROOT / "fixtures/cases/restart-branches-v2/oom-limit-v2.json"
_OOM_BRANCH = "peak_present"


class _OomDemoModelClient:
    """Offline planner whose only Evidence references come from sanitized observations.

    Token counts are fixed example accounting for replay, not provider billing.
    """

    def __init__(self, case: LoadedCaseV2) -> None:
        self._case = case
        branch = next(
            item for item in case.branches if item.definition.branch_id == _OOM_BRANCH
        )
        self._calls = tuple(
            step.invocation for step in branch.definition.steps if step.phase == "fault"
        )
        if tuple(call.tool for call in self._calls) != (
            "k8s.get_pod_status", "k8s.get_deployment", "prometheus.query_memory"
        ):
            raise ValueError("OOM replay branch has an unexpected Tool sequence")

    async def complete(
        self, request: StructuredModelRequest, output_model: type[OutputT],
    ) -> StructuredModelResult[OutputT]:
        output: BaseModel
        if request.prompt.component == "router" and output_model is V2RouterModelOutput:
            output = V2RouterModelOutput(
                intent="diagnose", domain="kubernetes", fault_family="oom_killed",
                target_kind="deployment", resource=self._case.definition.target.resource,
            )
        elif request.prompt.component == "planner" and output_model is PlanDecisionV2:
            payload = json.loads(request.messages[-1].content)
            observation = ObservationSummaryV2.model_validate_json(
                json.dumps(payload["observation"]), strict=True,
            )
            round_no = payload["round_no"]
            if type(round_no) is not int or not 1 <= round_no <= 4:
                raise ValueError("unexpected OOM demo planning round")
            if round_no <= len(self._calls):
                invocation = self._calls[round_no - 1]
                output = PlanDecisionV2(
                    round_no=round_no, action="continue",
                    based_on_evidence_ids=observation.evidence_ids,
                    calls=(PlanCallV2(
                        call_id=invocation.call_id, tool=invocation.tool,
                        arguments=invocation.arguments,
                        reason="Read the next scoped OOM observation",
                    ),),
                )
            else:
                output = PlanDecisionV2(
                    round_no=round_no, action="finish",
                    based_on_evidence_ids=observation.evidence_ids, calls=(),
                )
        else:
            raise ValueError("unsupported OOM demo model request")
        return StructuredModelResult[OutputT](
            output=output_model.model_validate_json(output.model_dump_json(), strict=True),
            metadata=ModelResponseMetadata(
                provider=request.config.provider, model=request.config.model,
                latency_ms=0,
                usage=ModelUsage(
                    input_tokens=24, output_tokens=12, total_tokens=36,
                ),
            ),
        )


async def run_demo(
    *, database_path: Path,
    scenario: Literal["crashloop", "oom"] = "crashloop",
    budget_limits: BudgetLimits | None = None,
) -> TraceView:
    """Migrate a chosen local SQLite file, replay one case, and read its safe Trace.

    Bundled fixtures and prompts are resolved from an editable project checkout.
    No live service or paid model is called by either scenario.
    """

    if scenario not in {"crashloop", "oom"}:
        raise ValueError("unsupported offline demo scenario")
    path = database_path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    database_url = URL.create("sqlite+pysqlite", database=str(path))
    migration = Config(str(_PROJECT_ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    migration.set_main_option("sqlalchemy.url", str(database_url).replace("%", "%%"))
    migration.attributes["ignore_environment_database_url"] = True
    command.upgrade(migration, "head")

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            if scenario == "crashloop":
                trace_id = await _run_crashloop(session, budget_limits)
            else:
                trace_id = await _run_oom(session, budget_limits)
    finally:
        engine.dispose()

    trace = query_trace(str(database_url), trace_id)
    if trace is None:
        raise RuntimeError("offline demo Trace was not persisted")
    return trace


async def _run_crashloop(session: Session, limits: BudgetLimits | None) -> str:
    case = load_case(_CRASHLOOP_CASE)
    storage = SQLAlchemyRuntimeRepository(session)
    audit = SQLAlchemyModelAuditRepository(session)
    tools = SQLAlchemyExecutionRepository(session)
    client = CaseModelClient(case)
    config = StructuredModelConfig(provider="offline", model="case-replay-v1")
    runtime = DiagnosisRuntime(
        router=IntentRouter(
            client=client, audit_repository=audit,
            prompt=load_prompt(
                _PROJECT_ROOT / "prompts/router/v1.md", component="router", version="v1",
            ),
            model_config=config,
        ),
        planner=V1Planner(
            client=client, audit_repository=audit,
            prompt=load_prompt(
                _PROJECT_ROOT / "prompts/planner/v1.md", component="planner", version="v1",
            ),
            model_config=config,
        ),
        diagnosis=V1DiagnosisAssembler(
            client=client, audit_repository=audit,
            evidence_repository=tools, run_repository=storage,
            result_repository=storage,
            prompt=load_prompt(
                _PROJECT_ROOT / "prompts/diagnosis/v1.md", component="diagnosis", version="v1",
            ),
            model_config=config,
        ),
        task_repository=storage, run_repository=storage,
        tool_repository=tools, result_repository=storage,
        registry_factory=ReplayRegistryFactory({case.definition.case_id: _CRASHLOOP_CASE}),
        budget_limits=limits,
    )
    result = await runtime.run(DiagnosisRequest(
        query=f"Why is {case.definition.target.resource} restarting?",
        namespace=case.definition.target.namespace,
        mode="replay", case_id=case.definition.case_id,
    ))
    return result.trace_id


async def _run_oom(session: Session, limits: BudgetLimits | None) -> str:
    case = load_case_v2(_OOM_CASE)
    branch = next(
        item for item in case.branches if item.definition.branch_id == _OOM_BRANCH
    )
    replay_time = max(item.collected_at for item in branch.definition.evidence)
    storage = SQLAlchemyRuntimeRepository(session)
    audit = SQLAlchemyModelAuditRepository(session)
    tools = SQLAlchemyExecutionRepository(session)
    rounds = SQLAlchemyPlanningRoundRepository(session)
    client = _OomDemoModelClient(case)
    registry = ReplayV2Registry(case, _OOM_BRANCH)
    config = StructuredModelConfig(provider="offline", model="deterministic-replay-v2")
    runtime = ObservationRuntimeV2(
        router=V2IntentRouter(
            client=client, audit_repository=audit,
            prompt=load_prompt(
                _PROJECT_ROOT / "prompts/router/v2.md", component="router", version="v2",
            ),
            model_config=config,
        ),
        planner=V2Planner(
            client=client, audit_repository=audit,
            prompt=load_prompt(
                _PROJECT_ROOT / "prompts/planner/v2.md", component="planner", version="v2",
            ),
            model_config=config,
        ),
        verifier=OomKilledVerifierV2(),
        task_repository=storage, run_repository=storage,
        tool_repository=tools, round_repository=rounds, result_repository=storage,
        registry_factory=lambda _: registry,
        allowed_resources=lambda _: frozenset(case.definition.allowed_resource_names),
        budget_limits=limits,
        clock=lambda: replay_time,
    )
    result = await runtime.run(V2DiagnosisRequest(
        query=case.definition.query, namespace=case.definition.target.namespace,
        mode="replay", case_id=case.definition.case_id, branch_id=_OOM_BRANCH,
    ))
    return result.trace_id
