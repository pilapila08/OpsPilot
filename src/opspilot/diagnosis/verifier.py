"""Deterministic verifier for the single V1 early-liveness CrashLoop case."""

from __future__ import annotations

from decimal import Decimal

from opspilot.agent.schemas import Target
from opspilot.evidence.models import Claim, Contradiction, Evidence, MissingEvidence, Verification
from opspilot.diagnosis.models import DiagnosisAssessment, DiagnosisDraftV1

_PARTIAL_CAUSE = "Insufficient evidence to confirm an early liveness probe as the restart cause."
_PARTIAL_ADVICE = "Collect the missing pod status, events, previous logs, and deployment probe configuration before changing the workload."


class DiagnosisInputError(ValueError):
    """The candidate or Evidence set violates the current Trace boundary."""


class BasicCrashLoopVerifier:
    """Check structured attributes; model prose never supplies verification facts."""

    def verify(
        self,
        *,
        draft: DiagnosisDraftV1,
        evidence: tuple[Evidence, ...],
        trace_id: str,
        target: Target,
        execution_complete: bool = True,
    ) -> DiagnosisAssessment:
        if not evidence or len(evidence) > 100:
            raise DiagnosisInputError("verifier requires one to 100 Evidence records")
        evidence_ids = [item.evidence_id for item in evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise DiagnosisInputError("Evidence IDs must be unique")
        if any(item.trace_id != trace_id for item in evidence):
            raise DiagnosisInputError("Evidence belongs to another Trace")
        known = set(evidence_ids)
        if any(not set(claim.evidence_ids).issubset(known) for claim in draft.claims):
            raise DiagnosisInputError("candidate cites unknown Evidence")

        relevant = tuple(item for item in evidence if _matches_target(item, target))
        if not relevant:
            raise DiagnosisInputError("no Evidence belongs to the authorized target")
        pod_resources = {
            item.resource.split(":", 1)[0]
            for item in relevant
            if item.source in {"kubernetes_status", "kubernetes_events", "kubernetes_logs"}
        }
        if len(pod_resources) > 1:
            raise DiagnosisInputError("V1 diagnosis requires one resolved Pod")

        status = [item for item in relevant if item.source == "kubernetes_status"]
        events = [item for item in relevant if item.source == "kubernetes_events"]
        logs = [item for item in relevant if item.source == "kubernetes_logs" and _integer(item, "startup_duration_seconds") is not None]
        deployments = [item for item in relevant if item.source == "kubernetes_deployment"]
        missing: list[MissingEvidence] = []
        contradictions: list[Contradiction] = []

        valid_status = next((item for item in status if _restarting_status(item)), None)
        for item in status:
            if _integer(item, "restart_count") == 0:
                contradictions.append(_contradiction(item, "Pod restart count is zero"))
        if valid_status is None:
            missing.append(_missing("restart_count", "CrashLoopBackOff or repeated abnormal termination was not observed"))

        liveness = next((item for item in events if _boolean(item, "liveness_failure") is True and _string(item, "event_reason") == "Unhealthy"), None)
        restart_event = next((item for item in events if _string(item, "event_reason") in {"Killing", "BackOff"}), None)
        if liveness is None:
            missing.append(_missing("liveness_failure", "No structured liveness failure event was observed"))
            for item in events:
                if _string(item, "event_reason") == "Unhealthy" and _boolean(item, "liveness_failure") is False:
                    contradictions.append(_contradiction(item, "Observed probe failure is not a liveness failure"))
        if restart_event is None:
            missing.append(_missing("restart_event", "No Killing or BackOff event links probe failures to a restart"))

        valid_logs = next((item for item in logs if (_integer(item, "startup_duration_seconds") or 0) > 0 and _integer(item, "terminated_after_seconds") is not None), None)
        if valid_logs is None:
            missing.append(_missing("startup_duration", "Previous logs do not establish startup duration and termination before readiness"))
        else:
            startup = _integer(valid_logs, "startup_duration_seconds")
            terminated = _integer(valid_logs, "terminated_after_seconds")
            assert startup is not None and terminated is not None
            if terminated < 0 or terminated >= startup:
                contradictions.append(_contradiction(valid_logs, "Logs do not show termination before startup completed"))

        valid_deployment = next((item for item in deployments if _probe_window(item) is not None and _boolean(item, "startup_probe_configured") is not None), None)
        if valid_deployment is None:
            missing.append(_missing("probe_configuration", "Deployment liveness timing or startup-probe presence is unavailable"))

        window: int | None = None
        if valid_deployment is not None:
            window = _probe_window(valid_deployment)
            if _boolean(valid_deployment, "startup_probe_configured") is True:
                startup_budget = _startup_budget(valid_deployment)
                reason = (
                    "A startup probe protects the application through startup"
                    if valid_logs is not None and startup_budget is not None and startup_budget >= (_integer(valid_logs, "startup_duration_seconds") or 0)
                    else "A configured startup probe gates liveness; its effect requires separate diagnosis"
                )
                contradictions.append(_contradiction(valid_deployment, reason))
            if valid_logs is not None and window is not None:
                startup = _integer(valid_logs, "startup_duration_seconds")
                terminated = _integer(valid_logs, "terminated_after_seconds")
                assert startup is not None and terminated is not None
                if startup <= window:
                    contradictions.append(Contradiction(evidence_ids=(valid_logs.evidence_id, valid_deployment.evidence_id), reason="Startup completes before the liveness failure window"))
                elif (delay := _integer(valid_deployment, "initial_delay_seconds")) is not None and terminated < delay:
                    contradictions.append(Contradiction(evidence_ids=(valid_logs.evidence_id, valid_deployment.evidence_id), reason="Termination predates the first liveness check"))

        if not execution_complete:
            missing.append(_missing("execution_complete", "Tool execution ended before all planned observations completed"))

        supported = not missing and not contradictions and len(relevant) >= 2
        selected = (valid_status, liveness, restart_event, valid_logs, valid_deployment)
        cited_ids = tuple(dict.fromkeys(item.evidence_id for item in selected if item is not None))
        if not cited_ids:
            cited_ids = (relevant[0].evidence_id,)
        checked_ids = tuple(item.evidence_id for item in relevant)
        claim_id = draft.claims[0].claim_id

        if supported:
            assert valid_logs is not None and valid_deployment is not None and window is not None
            startup = _integer(valid_logs, "startup_duration_seconds")
            delay = _integer(valid_deployment, "initial_delay_seconds")
            threshold = _integer(valid_deployment, "failure_threshold")
            assert startup is not None and delay is not None and threshold is not None
            root_cause = (f"The application needs {startup} seconds to start, but liveness begins at {delay} seconds and restarts it after {_number_word(threshold)} failed checks at about {window} seconds.")
            recommendation = (f"Add a startupProbe with a failure window longer than {startup} seconds so liveness is gated until startup succeeds.")
            claim_text = f"An early liveness probe repeatedly kills the application before its {startup} second startup completes."
            confidence = Decimal("0.99")
        else:
            root_cause = _PARTIAL_CAUSE
            recommendation = _PARTIAL_ADVICE
            claim_text = "An early liveness probe may be associated with the observed restarts."
            confidence = Decimal("0")
            if not missing and not contradictions:
                missing.append(_missing("minimum_evidence", "At least two relevant Evidence records are required"))

        claim = Claim(claim_id=claim_id, text=claim_text, evidence_ids=cited_ids, inference_confidence=float(confidence))
        verification = Verification(
            claim_id=claim_id,
            supported=supported,
            verification_confidence=float(confidence),
            checked_evidence_ids=checked_ids,
            missing_evidence=tuple(missing),
            contradictions=tuple(contradictions),
            rationale=(
                "Status, events, previous logs, and deployment configuration establish the restart timing and its cause."
                if supported else "The available structured Evidence does not establish the proposed cause."
            ),
        )
        return DiagnosisAssessment(
            status="COMPLETED" if supported else "PARTIAL",
            root_cause=root_cause,
            recommendation=recommendation,
            claim=claim,
            verification=verification,
            confidence=confidence,
        )


def _matches_target(item: Evidence, target: Target) -> bool:
    if item.source == "kubernetes_deployment":
        return item.resource == f"{target.namespace}/deployment/{target.resource}"
    if item.source not in {"kubernetes_status", "kubernetes_events", "kubernetes_logs"}:
        return False
    pod = item.resource.split(":", 1)[0]
    return pod == f"{target.namespace}/{target.resource}" or pod.startswith(f"{target.namespace}/{target.resource}-")


def _attribute(item: Evidence, key: str) -> object:
    return next((attribute.value for attribute in item.attributes if attribute.key == key), None)


def _integer(item: Evidence, key: str) -> int | None:
    value = _attribute(item, key)
    return value if type(value) is int else None


def _boolean(item: Evidence, key: str) -> bool | None:
    value = _attribute(item, key)
    return value if type(value) is bool else None


def _string(item: Evidence, key: str) -> str | None:
    value = _attribute(item, key)
    return value if isinstance(value, str) else None


def _probe_window(item: Evidence) -> int | None:
    delay = _integer(item, "initial_delay_seconds")
    period = _integer(item, "period_seconds")
    threshold = _integer(item, "failure_threshold")
    if delay is None or period is None or threshold is None or delay < 0 or period <= 0 or threshold <= 0:
        return None
    return delay + period * (threshold - 1)


def _restarting_status(item: Evidence) -> bool:
    restarts = _integer(item, "restart_count")
    if restarts is None or restarts <= 0:
        return False
    if _string(item, "container_state") == "CrashLoopBackOff":
        return True
    exit_code = _integer(item, "last_exit_code")
    return restarts > 1 and exit_code is not None and exit_code != 0


def _startup_budget(item: Evidence) -> int | None:
    period = _integer(item, "startup_probe_period_seconds")
    threshold = _integer(item, "startup_probe_failure_threshold")
    if period is None or threshold is None or period <= 0 or threshold <= 0:
        return None
    return period * threshold


def _missing(requirement: str, reason: str) -> MissingEvidence:
    return MissingEvidence(requirement=requirement, reason=reason)


def _contradiction(item: Evidence, reason: str) -> Contradiction:
    return Contradiction(evidence_ids=(item.evidence_id,), reason=reason)


def _number_word(value: int) -> str:
    return {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}.get(value, str(value))
