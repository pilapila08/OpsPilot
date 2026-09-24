from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from opspilot.diagnosis.oom_v2 import OomKilledVerifierV2
from opspilot.evidence.models import Evidence
from opspilot.evidence.v2 import V2EvidenceExtractorRegistry
from opspilot.routing.v2 import IntentV2, V2Target
from opspilot.tools import ToolInvocation
from opspilot.tools.models import ToolMetadata, ToolResponse
from opspilot.tools.prometheus import MetricOutputV2, MetricSampleV2

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 9, 23, 10, 0, 30, tzinfo=UTC)


class Ids:
    def __init__(self) -> None:
        self.index = 0

    def next(self) -> str:
        self.index += 1
        return f"ev_{self.index:03d}"


def _fixture(name: str, call_id: str, tool: str, args: dict[str, JsonValue], ids: Ids) -> tuple[Evidence, ...]:
    path = ROOT / f"fixtures/cases/restart-branches-v2/responses/oom/{name}.json"
    response = ToolResponse.model_validate_json(path.read_text(encoding="utf-8"))
    return V2EvidenceExtractorRegistry().extract(
        invocation=ToolInvocation(call_id=call_id, tool=tool, arguments=args),
        response=response, trace_id="trace_oom", tool_attempt_id=f"tool_{name}",
        collected_at=NOW, evidence_id_factory=ids.next,
    )


def _metric(ids: Ids, *, sample_time: datetime, value: float = 125_829_120.0) -> tuple[Evidence, ...]:
    output = MetricOutputV2(
        namespace="opspilot-fixtures", workload_name="api",
        pod_name="api-001", container_name="api", metric="memory", unit="bytes",
        status="present", observed_at=NOW,
        samples=(MetricSampleV2(sampled_at=sample_time, value=value),),
    )
    return V2EvidenceExtractorRegistry().extract(
        invocation=ToolInvocation(
            call_id="call_metric", tool="prometheus.query_memory",
            arguments={
                "namespace": "opspilot-fixtures", "workload_name": "api",
                "pod_name": "api-001", "container_name": "api",
            },
        ),
        response=ToolResponse(
            success=True, data=output.model_dump(mode="json"),
            error=None,
            metadata=ToolMetadata(
                call_id="call_metric", tool_name="prometheus.query_memory",
                source="prometheus", duration_ms=1, tool_version="v2",
            ),
        ),
        trace_id="trace_oom", tool_attempt_id="tool_metric",
        collected_at=NOW, evidence_id_factory=ids.next,
    )


def _intent() -> IntentV2:
    return IntentV2(
        domain="kubernetes", fault_family="oom_killed",
        target=V2Target(namespace="opspilot-fixtures", kind="deployment", resource="api"),
    )


def _base(ids: Ids) -> tuple[Evidence, ...]:
    return (
        *_fixture("status", "call_status", "k8s.get_pod_status", {
            "namespace": "opspilot-fixtures", "workload_name": "api",
        }, ids),
        *_fixture("deployment", "call_oom_deployment", "k8s.get_deployment", {
            "namespace": "opspilot-fixtures", "deployment_name": "api",
        }, ids),
    )


def test_oom_same_container_peak_near_limit_is_supported() -> None:
    ids = Ids()
    evidence = (*_base(ids), *_metric(ids, sample_time=datetime(2026, 9, 23, 9, 59, 55, tzinfo=UTC)))
    assert {item.source for item in evidence} == {
        "kubernetes_status", "kubernetes_oom_termination",
        "kubernetes_memory_limit", "prometheus_memory",
    }
    result = OomKilledVerifierV2().verify(intent=_intent(), evidence=evidence, force_partial=False)
    assert result.status == "COMPLETED"
    assert result.verification.supported is True
    assert len(result.claim.evidence_ids) == 3
    assert "134217728" in result.root_cause
    assert all(item.raw_result_ref for item in evidence)


def test_oom_missing_stale_or_low_peak_remains_partial() -> None:
    ids = Ids()
    base = _base(ids)
    verifier = OomKilledVerifierV2()
    missing = verifier.verify(intent=_intent(), evidence=base, force_partial=False)
    assert missing.status == "PARTIAL"
    assert {item.requirement for item in missing.verification.missing_evidence} == {"memory_peak"}
    old = (*base, *_metric(ids, sample_time=datetime(2026, 9, 23, 9, 55, tzinfo=UTC)))
    stale = verifier.verify(intent=_intent(), evidence=old, force_partial=False)
    assert stale.status == "PARTIAL"
    assert "memory_peak" in {item.requirement for item in stale.verification.missing_evidence}
    low = (*base, *_metric(ids, sample_time=datetime(2026, 9, 23, 9, 59, 55, tzinfo=UTC), value=20_000_000))
    contradicted = verifier.verify(intent=_intent(), evidence=low, force_partial=False)
    assert contradicted.status == "PARTIAL"
    assert contradicted.verification.contradictions


def test_oom_reason_absent_cannot_be_upgraded_by_memory_metric() -> None:
    ids = Ids()
    base = _base(ids)
    unrelated = tuple(item for item in base if item.source != "kubernetes_oom_termination")
    evidence = (*unrelated, *_metric(ids, sample_time=datetime(2026, 9, 23, 9, 59, 55, tzinfo=UTC)))
    result = OomKilledVerifierV2().verify(intent=_intent(), evidence=evidence, force_partial=False)
    assert result.status == "PARTIAL"
    assert "oom_termination" in {item.requirement for item in result.verification.missing_evidence}
