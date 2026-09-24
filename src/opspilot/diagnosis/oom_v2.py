"""Deterministic OOMKilled limit diagnosis from same-container observations."""

from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal
from typing import cast

from opspilot.diagnosis.v2 import V2Assessment
from opspilot.evidence.models import (
    Claim, Contradiction, Evidence, MissingEvidence, Verification,
)
from opspilot.routing.v2 import IntentV2


class OomKilledVerifierV2:
    def verify(
        self, *, intent: IntentV2, evidence: tuple[Evidence, ...],
        force_partial: bool,
    ) -> V2Assessment:
        if not evidence or len(evidence) > 100:
            raise ValueError("OOM verifier requires bounded Evidence")
        trace_ids = {item.trace_id for item in evidence}
        if len(trace_ids) != 1 or len({item.evidence_id for item in evidence}) != len(evidence):
            raise ValueError("OOM verifier requires unique current-Trace Evidence")
        namespace = intent.target.namespace
        workload = intent.target.resource
        relevant = tuple(item for item in evidence if item.resource.startswith(f"{namespace}/"))
        if not relevant:
            raise ValueError("OOM verifier requires namespace-scoped Evidence")
        missing: list[MissingEvidence] = []
        contradictions: list[Contradiction] = []
        if intent.fault_family != "oom_killed" or intent.target.kind != "deployment":
            missing.append(MissingEvidence(
                requirement="oom_target", reason="OOM limit rule requires a Deployment target.",
            ))
        terminations = [item for item in relevant
                        if item.source == "kubernetes_oom_termination"
                        and item.resource.startswith(f"{namespace}/{workload}-")
                        and _attribute(item, "termination_reason") == "OOMKilled"
                        and _positive_int(item, "restart_count") is not None]
        if len(terminations) != 1:
            missing.append(MissingEvidence(
                requirement="oom_termination",
                reason="One exact restarted OOMKilled Pod/container was not established.",
            ))
        for item in relevant:
            if item.source == "kubernetes_status" and _attribute(item, "last_reason") not in (None, "OOMKilled"):
                contradictions.append(Contradiction(
                    evidence_ids=(item.evidence_id,),
                    reason="Observed last termination reason is not OOMKilled.",
                ))
        termination = terminations[0] if len(terminations) == 1 else None
        limit: Evidence | None = None
        near: list[Evidence] = []
        if termination is not None:
            container_name = termination.resource.rsplit(":", 1)[-1]
            limits = [item for item in relevant
                      if item.source == "kubernetes_memory_limit"
                      and item.resource == f"{namespace}/deployment/{workload}:{container_name}"
                      and _positive_int(item, "memory_limit_bytes") is not None
                      and timedelta(0) <= item.observed_at - termination.observed_at <= timedelta(minutes=10)]
            if len(limits) == 1:
                limit = limits[0]
            else:
                missing.append(MissingEvidence(
                    requirement="memory_limit",
                    reason="A contemporaneous memory limit for the terminated container is unavailable.",
                ))
            near = [item for item in relevant
                    if item.source == "prometheus_memory"
                    and item.resource == termination.resource
                    and _attribute(item, "metric_unit") == "bytes"
                    and _nonnegative_number(item, "metric_value") is not None
                    and timedelta(0) <= termination.observed_at - item.observed_at <= timedelta(seconds=120)]
            if not near:
                missing.append(MissingEvidence(
                    requirement="memory_peak",
                    reason="No fresh same-container memory sample overlaps the OOM termination.",
                ))
        peak = max(near, key=lambda item: _nonnegative_number(item, "metric_value") or 0) if near else None
        if peak is not None and limit is not None:
            peak_value = _nonnegative_number(peak, "metric_value")
            limit_value = _positive_int(limit, "memory_limit_bytes")
            assert peak_value is not None and limit_value is not None
            if peak_value < 0.9 * limit_value:
                contradictions.append(Contradiction(
                    evidence_ids=(peak.evidence_id, limit.evidence_id),
                    reason="Observed memory peak does not approach the configured limit.",
                ))
        if force_partial:
            missing.append(MissingEvidence(
                requirement="execution_complete",
                reason="Observation was stopped before full verification.",
            ))
        cited = tuple(dict.fromkeys(
            item.evidence_id for item in (termination, limit, peak) if item is not None
        )) or (relevant[0].evidence_id,)
        checked = tuple(item.evidence_id for item in relevant)
        supported = not missing and not contradictions and len(cited) == 3
        if not supported and not missing and not contradictions:
            missing.append(MissingEvidence(
                requirement="independent_signals", reason="OOM claim lacks three independent signals.",
            ))
        if supported:
            assert termination is not None and limit is not None and peak is not None
            limit_value = _positive_int(limit, "memory_limit_bytes")
            peak_value = _nonnegative_number(peak, "metric_value")
            assert limit_value is not None and peak_value is not None
            root_cause = (
                f"Container {termination.resource} was OOMKilled while observed memory "
                f"reached {peak_value:.0f} bytes near its {limit_value} byte limit."
            )
            advice = "Review the container's memory demand and configured limit before changing resources."
            claim_text = "The container reached its configured memory limit near an OOMKilled termination."
            confidence = Decimal("0.9")
        else:
            root_cause = "OOMKilled or memory pressure is not yet established as a limit-related cause."
            advice = "Collect an exact container memory limit and fresh Pod-scoped samples around termination."
            claim_text = "The available observations do not yet prove a limit-related OOM cause."
            confidence = Decimal("0")
        claim = Claim(
            claim_id="claim_oom_limit", text=claim_text, evidence_ids=cited,
            inference_confidence=float(confidence),
        )
        verification = Verification(
            claim_id=claim.claim_id, supported=supported,
            verification_confidence=float(confidence), checked_evidence_ids=checked,
            missing_evidence=tuple(missing), contradictions=tuple(contradictions),
            rationale=(
                "Termination reason, matching limit and same-container time-aligned memory are present."
                if supported else "The required OOM limit relationship is incomplete or contradicted."
            ),
        )
        return V2Assessment(
            status="COMPLETED" if supported else "PARTIAL",
            root_cause=root_cause, recommendation=advice,
            confidence=confidence, claim=claim, verification=verification,
        )


def _attribute(item: Evidence, key: str) -> object:
    return next((attribute.value for attribute in item.attributes if attribute.key == key), None)


def _positive_int(item: Evidence, key: str) -> int | None:
    value = _attribute(item, key)
    return value if type(value) is int and value > 0 else None


def _nonnegative_number(item: Evidence, key: str) -> float | None:
    value = _attribute(item, key)
    if type(value) not in (int, float):
        return None
    number = float(cast(float, value))
    return number if math.isfinite(number) and number >= 0 else None
