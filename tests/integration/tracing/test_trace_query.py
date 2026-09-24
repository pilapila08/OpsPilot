"""A real SQLite audit chain can be read without revealing raw payloads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from opspilot.agent.state import AgentState, AgentStatus
from opspilot.cli import main
from opspilot.errors import ErrorCode
from opspilot.llm.audit import InMemoryModelAuditRepository, ModelCallAttempt
from opspilot.storage.model_audit import SQLAlchemyModelAuditRepository
from opspilot.storage.models import (
    AgentRunRecord,
    Base,
    DiagnosisResultRecord,
    DiagnosisTaskRecord,
    EvidenceRecord,
    LLMCallRecord,
    PlanningRoundRecord,
    PromptVersionRecord,
    ToolCallRecord,
)
from opspilot.tracing.query import TraceQueryService, query_trace

_AT = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)


def _seed_database(path: Path) -> str:
    url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(PromptVersionRecord(
            id="prompt_router_v1", component="router", version="v1",
            content_hash="a" * 64, template_text="SECRET PROMPT", model_family=None,
        ))
        for suffix, version, status in (
            ("one", "v1", AgentStatus.COMPLETED),
            ("two", "v2", AgentStatus.PARTIAL),
        ):
            task_id = f"task_{suffix}"
            run_id = f"run_{suffix}"
            trace_id = f"trace_{suffix}"
            state = AgentState(
                task_id=task_id, trace_id=trace_id,
                user_query="SECRET USER QUERY", status=status,
            )
            session.add(DiagnosisTaskRecord(
                id=task_id, user_query="SECRET USER QUERY", namespace="fixture",
                status=status.value, mode="replay", case_id="fixture_case",
            ))
            session.add(AgentRunRecord(
                id=run_id, task_id=task_id, trace_id=trace_id,
                attempt_no=1, status=status.value,
                state_payload=state.model_dump(mode="json"), runtime_version=version,
            ))
        session.commit()
        session.add_all([
            LLMCallRecord(
                id="llm_first", run_id="run_one", prompt_version_id="prompt_router_v1",
                sequence_no=1, component="router", provider="case", model_name="scripted",
                request_payload={"secret": "RAW PROVIDER REQUEST"},
                response_payload={"secret": "RAW PROVIDER RESPONSE"},
                input_tokens=11, output_tokens=2, cost_usd=Decimal("0.000111"),
                latency_ms=5, retry_count=0, success=True, error_code=None,
                started_at=_AT, completed_at=_AT,
            ),
            LLMCallRecord(
                id="llm_retry", run_id="run_one", prompt_version_id="prompt_router_v1",
                sequence_no=2, component="planner", provider="case", model_name="scripted",
                request_payload={"secret": "RAW PROVIDER REQUEST"},
                response_payload=None,
                input_tokens=3, output_tokens=0, cost_usd=Decimal("0.000003"),
                latency_ms=4, retry_count=1, success=False,
                error_code=ErrorCode.SCHEMA_VALIDATION.value,
                started_at=_AT, completed_at=_AT,
            ),
            LLMCallRecord(
                id="llm_other", run_id="run_two", prompt_version_id="prompt_router_v1",
                sequence_no=1, component="router", provider="case", model_name="scripted",
                request_payload={"round_no": 1, "secret": "OTHER TRACE"}, response_payload={"ok": True},
                input_tokens=1, output_tokens=1, cost_usd=Decimal("0"),
                latency_ms=1, retry_count=0, success=True, error_code=None,
                started_at=_AT, completed_at=_AT,
            ),
        ])
        session.add_all([
            ToolCallRecord(
                id="tool_attempt_one", run_id="run_one", sequence_no=1,
                logical_call_id="call_probe", attempt_no=1,
                tool_name="k8s.pod_status", tool_version="v1", risk_level=0,
                arguments_payload={"secret": "RAW TOOL ARGUMENT"},
                result_payload={"secret": "RAW TOOL RESPONSE"},
                duration_ms=9, success=False, error_code="TOOL_TIMEOUT",
                started_at=_AT, completed_at=_AT,
            ),
            ToolCallRecord(
                id="tool_attempt_two", run_id="run_one", sequence_no=2,
                logical_call_id="call_probe", attempt_no=2,
                tool_name="k8s.pod_status", tool_version="v1", risk_level=0,
                arguments_payload={"secret": "RAW TOOL ARGUMENT"},
                result_payload={"secret": "RAW TOOL RESPONSE"},
                duration_ms=2, success=True, error_code=None,
                started_at=_AT, completed_at=_AT,
            ),
        ])
        session.add(EvidenceRecord(
            id="ev_probe", run_id="run_one", tool_call_id="tool_attempt_two",
            source="kubernetes", resource="fixture/pod", observed_at=_AT,
            collected_at=_AT, content="RAW LOG SECRET", source_confidence=Decimal("0.9"),
            raw_result_ref="tool_attempt_two", attributes_payload=[{"key": "secret", "value": "RAW ATTRIBUTE"}],
            schema_version=1,
        ))
        session.add(DiagnosisResultRecord(
            id="result_one", task_id="task_one", run_id="run_one",
            status="COMPLETED", root_cause="Verified probe failure",
            recommendation="Increase startup window", confidence=Decimal("0.9"),
            claims_payload=[{"claim_id": "claim_probe", "evidence_ids": ["ev_probe"], "text": "RAW CLAIM TEXT"}],
            verification_payload={
                "supported": True, "checked_evidence_ids": ["ev_probe"],
                "rationale": "RAW VERIFIER TEXT",
            },
            schema_version=1,
        ))
        session.add(PlanningRoundRecord(
            run_id="run_two", round_no=1, prompt_version_id="prompt_router_v1",
            decision_hash="b" * 64, action="partial",
            evidence_ids_payload=[], call_ids_payload=[], validation_status="ADMITTED",
        ))
        session.commit()
    engine.dispose()
    return url


def test_trace_query_is_ordered_isolated_and_safe(tmp_path: Path) -> None:
    url = _seed_database(tmp_path / "trace.db")
    view = query_trace(url, "trace_one")
    assert view is not None
    assert view.run.planning_mode == "single-pass"
    assert view.rounds == ()
    assert [call.call_id for call in view.model_calls] == ["llm_first", "llm_retry"]
    assert [attempt.attempt_no for attempt in view.tool_attempts] == [1, 2]
    assert [item.evidence_id for item in view.evidence] == ["ev_probe"]
    assert view.result is not None
    assert view.result.verification.supported is True
    assert view.statistics.input_tokens == 14
    assert view.statistics.tool_retries == 1
    output = view.model_dump_json()
    for secret in (
        "SECRET USER QUERY", "SECRET PROMPT", "RAW PROVIDER REQUEST",
        "RAW PROVIDER RESPONSE", "RAW TOOL ARGUMENT", "RAW TOOL RESPONSE",
        "RAW LOG SECRET", "RAW ATTRIBUTE", "RAW CLAIM TEXT", "RAW VERIFIER TEXT",
    ):
        assert secret not in output

    other = query_trace(url, "trace_two")
    assert other is not None
    assert other.run.planning_mode == "multi-round"
    assert len(other.rounds) == 1
    assert [call.call_id for call in other.model_calls] == ["llm_other"]
    assert other.model_calls[0].round_no == 1
    assert other.tool_attempts == ()
    assert other.evidence == ()
    assert other.result is None


def test_model_audit_read_repository_and_missing_trace(tmp_path: Path) -> None:
    url = _seed_database(tmp_path / "trace.db")
    engine = create_engine(url)
    with Session(engine) as session:
        repo = SQLAlchemyModelAuditRepository(session)
        calls = repo.calls_for_trace("trace_one")
        assert [item.sequence_no for item in calls] == [1, 2]
        assert calls[1].error_code is ErrorCode.SCHEMA_VALIDATION
        assert calls[0].prompt_version_id == "prompt_router_v1"
        assert repo.calls_for_trace("trace_missing") == ()
        assert TraceQueryService(session).get("trace_missing") is None
    engine.dispose()


def test_in_memory_model_audit_trace_binding() -> None:
    repo = InMemoryModelAuditRepository({"run_one": "trace_one", "run_two": "trace_two"})
    for run_id in ("run_one", "run_two", "run_one"):
        repo.append_attempt(ModelCallAttempt(
            run_id=run_id, component="router", prompt_version_id="prompt_router_v1",
            provider="case", model_name="scripted",
            request_payload={"secret": "RAW REQUEST"}, response_payload={"ok": True},
            input_tokens=1, output_tokens=2, success=True,
            started_at=_AT, completed_at=_AT,
        ))
    calls = repo.calls_for_trace("trace_one")
    assert [item.sequence_no for item in calls] == [1, 2]
    assert [item.run_id for item in calls] == ["run_one", "run_one"]
    assert "RAW REQUEST" not in str(calls)
    assert len(repo.calls_for_trace("trace_two")) == 1
    assert repo.calls_for_trace("missing") == ()


def test_cli_json_and_unknown_trace(tmp_path: Path, monkeypatch: object, capsys: object) -> None:
    from pytest import CaptureFixture, MonkeyPatch

    mp = monkeypatch
    captured = capsys
    assert isinstance(mp, MonkeyPatch)
    assert isinstance(captured, CaptureFixture)
    url = _seed_database(tmp_path / "trace.db")
    mp.setenv("OPSPILOT_DATABASE_URL", url)
    assert main(["trace", "trace_one", "--format", "json"]) == 0
    output = captured.readouterr().out
    payload = json.loads(output)
    assert payload["run"]["planning_mode"] == "single-pass"
    assert payload["statistics"]["tool_retries"] == 1
    assert "RAW PROVIDER REQUEST" not in output
    assert main(["trace", "trace_one"]) == 0
    text_output = captured.readouterr().out
    assert "tokens=11/2/13" in text_output
    assert "cost_usd=0.000111" in text_output
    assert "latency_ms=5" in text_output
    assert "retry_count=1" in text_output
    assert "single-pass" in text_output
    assert "RAW PROVIDER RESPONSE" not in text_output
    assert main(["trace", "trace_missing"]) == 1
    assert "Trace not found" in captured.readouterr().err


def test_cli_does_not_create_absent_database(tmp_path: Path, monkeypatch: object, capsys: object) -> None:
    from pytest import CaptureFixture, MonkeyPatch

    mp = monkeypatch
    captured = capsys
    assert isinstance(mp, MonkeyPatch)
    assert isinstance(captured, CaptureFixture)
    missing = tmp_path / "missing.db"
    mp.setenv("OPSPILOT_DATABASE_URL", f"sqlite:///{missing.as_posix()}")
    assert main(["trace", "trace_one"]) == 2
    assert not missing.exists()
    assert "unavailable" in captured.readouterr().err
