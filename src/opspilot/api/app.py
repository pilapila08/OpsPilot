"""Minimal V1 diagnostic HTTP API over the audited Runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
import os
from typing import Annotated

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Header, HTTPException
from pydantic import Field, JsonValue
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from opspilot.agent.schemas import StrictSchema
from opspilot.agent.state import AgentStatus
from opspilot.api.dispatcher import DispatchUnavailable, RunDispatcher
from opspilot.api.settings import ApiSettings
from opspilot.cases import load_case
from opspilot.diagnosis import V1DiagnosisAssembler
from opspilot.execution.repository import ExecutionRepository
from opspilot.integrations.kubernetes import build_kubernetes_reader
from opspilot.integrations.kubernetes.models import KubernetesClientSettings
from opspilot.llm.models import StructuredModelConfig
from opspilot.llm.client import StructuredModelClient
from opspilot.llm.openai_adapter import (
    OpenAIAdapterSettings, build_openai_structured_client,
)
from opspilot.llm.prompts import load_prompt
from opspilot.planning import V1Planner
from opspilot.routing import IntentRouter
from opspilot.runtime import (
    CaseModelClient, DiagnosisRequest, DiagnosisRunResult,
    DiagnosisRuntime, ReplayRegistryFactory,
)
from opspilot.storage import (
    AgentRunRecord, SQLAlchemyExecutionRepository,
    SQLAlchemyModelAuditRepository, SQLAlchemyRuntimeRepository,
)
from opspilot.storage.contracts import (
    RuntimePersistenceError, TaskSnapshot,
)
from opspilot.tools.kubernetes import build_kubernetes_registry
from opspilot.tools import ToolRegistry

_ROOT = Path(__file__).resolve().parents[3]
_CASE_FILE = _ROOT / "fixtures/cases/crashloop-liveness-v1/case.json"
_CASES = {"crashloop_liveness_v1": _CASE_FILE}


class CreateDiagnosis(StrictSchema):
    query: str = Field(min_length=1, max_length=4_000)
    namespace: str = Field(min_length=1, max_length=63)
    mode: str
    case_id: str | None = None


class CreatedTask(StrictSchema):
    task_id: str
    status: AgentStatus


class EvidenceView(StrictSchema):
    evidence_id: str
    source: str
    resource: str


class ErrorView(StrictSchema):
    code: str
    message: str


class DiagnosisView(StrictSchema):
    task_id: str
    status: AgentStatus
    run_id: str | None = None
    trace_id: str | None = None
    root_cause: str | None = None
    evidence: tuple[EvidenceView, ...] = ()
    recommendation: str | None = None
    verification: dict[str, JsonValue] | None = None
    error: ErrorView | None = None


class SessionTasks:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_task(self, task: TaskSnapshot) -> None:
        with Session(self._engine) as session:
            SQLAlchemyRuntimeRepository(session).create_task(task)

    def create_or_get_task(self, task: TaskSnapshot) -> tuple[TaskSnapshot, bool]:
        with Session(self._engine) as session:
            return SQLAlchemyRuntimeRepository(session).create_or_get_task(task)

    def get_task(self, task_id: str) -> TaskSnapshot | None:
        with Session(self._engine) as session:
            return SQLAlchemyRuntimeRepository(session).get_task(task_id)

    def get_by_idempotency_key(self, key: str) -> TaskSnapshot | None:
        with Session(self._engine) as session:
            return SQLAlchemyRuntimeRepository(session).get_by_idempotency_key(key)

    def fail_unstarted_task(self, task_id: str) -> None:
        with Session(self._engine) as session:
            SQLAlchemyRuntimeRepository(session).fail_unstarted_task(task_id)


def _migrate(database_url: str) -> None:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings.from_env()
    if settings.live_enabled and not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("live mode requires the configured model credential")
    _migrate(settings.database_url)
    engine = create_engine(settings.database_url)
    tasks = SessionTasks(engine)

    async def run(request: DiagnosisRequest, task_id: str) -> DiagnosisRunResult:
        with Session(engine) as session:
            storage = SQLAlchemyRuntimeRepository(session)
            audit = SQLAlchemyModelAuditRepository(session)
            tools: ExecutionRepository = SQLAlchemyExecutionRepository(session)
            reader = None
            model: StructuredModelClient | None = None
            try:
                if request.mode == "replay":
                    assert request.case_id is not None
                    model = CaseModelClient(load_case(_CASES[request.case_id]))
                    registry_factory: Callable[[DiagnosisRequest], ToolRegistry] = ReplayRegistryFactory(_CASES)
                    model_config = StructuredModelConfig(provider="case", model="deterministic")
                else:
                    model = build_openai_structured_client(OpenAIAdapterSettings())
                    reader = build_kubernetes_reader(KubernetesClientSettings(
                        mode=settings.kube_mode,
                        kubeconfig_path=settings.kubeconfig_path,
                        context=settings.kube_context,
                    ))
                    registry_factory = lambda _: build_kubernetes_registry(reader)
                    model_config = StructuredModelConfig(
                        provider="openai", model=settings.model_name or "",
                        timeout_seconds=settings.model_timeout_seconds,
                    )
                assert model is not None
                runtime = DiagnosisRuntime(
                    router=IntentRouter(
                        client=model, audit_repository=audit,
                        prompt=load_prompt(_ROOT / "prompts/router/v1.md", component="router", version="v1"),
                        model_config=model_config,
                    ),
                    planner=V1Planner(
                        client=model, audit_repository=audit,
                        prompt=load_prompt(
                            _ROOT / f"prompts/planner/{'v2' if request.mode == 'live' else 'v1'}.md",
                            component="planner",
                            version="v2" if request.mode == "live" else "v1",
                        ),
                        model_config=model_config,
                    ),
                    diagnosis=V1DiagnosisAssembler(
                        client=model, audit_repository=audit,
                        evidence_repository=tools, run_repository=storage,
                        result_repository=storage,
                        prompt=load_prompt(
                            _ROOT / f"prompts/diagnosis/{'v2' if request.mode == 'live' else 'v1'}.md",
                            component="diagnosis",
                            version="v2" if request.mode == "live" else "v1",
                        ),
                        model_config=model_config,
                    ),
                    task_repository=storage, run_repository=storage,
                    tool_repository=tools, result_repository=storage,
                    registry_factory=registry_factory,
                    budget_limits=settings.budget,
                )
                return await runtime.run(request, task_id=task_id)
            finally:
                if reader is not None:
                    reader.close()
                if model is not None and hasattr(model, "close"):
                    await model.close()

    dispatcher = RunDispatcher(
        tasks=tasks, run=run, max_concurrent=settings.max_concurrent,
        max_pending=settings.max_pending, shutdown_seconds=settings.shutdown_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await dispatcher.close()
            engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.state.dispatcher = dispatcher

    @app.post("/diagnosis", response_model=CreatedTask, status_code=202)
    async def create_diagnosis(
        body: CreateDiagnosis,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"),
        ] = None,
    ) -> CreatedTask:
        try:
            request = DiagnosisRequest.model_validate(body.model_dump())
        except ValueError:
            raise HTTPException(422, detail="invalid diagnosis request") from None
        if request.mode == "replay" and request.case_id not in _CASES:
            raise HTTPException(422, detail="unknown replay case")
        if request.mode == "live" and not settings.live_enabled:
            raise HTTPException(503, detail="live diagnosis is disabled")
        try:
            task = await dispatcher.submit(request, idempotency_key)
        except DispatchUnavailable:
            raise HTTPException(503, detail="diagnosis capacity is unavailable") from None
        except RuntimePersistenceError as exc:
            if "idempotency key" in str(exc):
                raise HTTPException(409, detail="idempotency key conflicts with another request") from None
            raise HTTPException(503, detail="diagnosis task could not be persisted") from None
        return CreatedTask(task_id=task.task_id, status=AgentStatus.CREATED if task.status is AgentStatus.CREATED else task.status)

    @app.get("/diagnosis/{task_id}", response_model=DiagnosisView)
    def get_diagnosis(task_id: str) -> DiagnosisView:
        with Session(engine) as session:
            storage = SQLAlchemyRuntimeRepository(session)
            task = storage.get_task(task_id)
            if task is None:
                raise HTTPException(404, detail="diagnosis task not found")
            run_id = session.scalar(
                select(AgentRunRecord.id)
                .where(AgentRunRecord.task_id == task_id)
                .order_by(AgentRunRecord.attempt_no.desc())
                .limit(1)
            )
            if run_id is None:
                return DiagnosisView(
                    task_id=task_id, status=task.status,
                    error=ErrorView(code="RUNTIME_FAILED", message="diagnosis did not start")
                    if task.status is AgentStatus.FAILED else None,
                )
            run_snapshot = storage.get_run(run_id)
            assert run_snapshot is not None
            result = storage.get_result(run_id)
            evidence = SQLAlchemyExecutionRepository(session).evidence_for_trace(
                run_snapshot.state.trace_id
            )
            last_error = dispatcher.results.get(task_id)
            error = (
                ErrorView(
                    code=last_error.error.code.value,
                    message=_safe_status_message(task.status),
                )
                if last_error is not None and last_error.error is not None
                else ErrorView(code="RUNTIME_FAILED", message=_safe_status_message(task.status))
                if task.status in {AgentStatus.FAILED, AgentStatus.POLICY_REJECTED, AgentStatus.BUDGET_EXCEEDED}
                else None
            )
            return DiagnosisView(
                task_id=task_id, status=task.status, run_id=run_id,
                trace_id=run_snapshot.state.trace_id,
                root_cause=result.root_cause if result is not None else None,
                evidence=tuple(EvidenceView(
                    evidence_id=item.evidence_id, source=item.source,
                    resource=item.resource,
                ) for item in evidence),
                recommendation=result.recommendation if result is not None else None,
                verification=result.verification_payload if result is not None else None,
                error=error,
            )

    return app


def _safe_status_message(status: AgentStatus) -> str:
    if status is AgentStatus.BUDGET_EXCEEDED:
        return "diagnosis budget was exceeded"
    if status is AgentStatus.POLICY_REJECTED:
        return "diagnosis request was rejected"
    return "diagnosis did not complete"
