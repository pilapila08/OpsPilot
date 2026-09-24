"""Bounded post-deployment timeline assessment; correlation is not causation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import cast

from opspilot.diagnosis.v2 import V2Assessment
from opspilot.evidence.models import Claim, Contradiction, Evidence, MissingEvidence, Verification
from opspilot.routing.v2 import IntentV2


def _attr(item: Evidence, key: str) -> object:
    return next((attribute.value for attribute in item.attributes if attribute.key == key), None)


def _rate(item: Evidence) -> float | None:
    value = _attr(item, "metric_value")
    if _attr(item, "metric_unit") != "ratio" or type(value) not in (int, float):
        return None
    selected = float(cast(float, value))
    return selected if 0 <= selected <= 1 else None


class PostDeploymentVerifierV2:
    def verify(
        self, *, intent: IntentV2, evidence: tuple[Evidence, ...],
        force_partial: bool,
    ) -> V2Assessment:
        if not evidence or len(evidence) > 100:
            raise ValueError("release verifier requires bounded Evidence")
        if len({item.trace_id for item in evidence}) != 1 or len({item.evidence_id for item in evidence}) != len(evidence):
            raise ValueError("release verifier requires unique current-Trace Evidence")
        resource = f"{intent.target.namespace}/deployment/{intent.target.resource}"
        relevant = tuple(item for item in evidence if item.resource == resource)
        missing: list[MissingEvidence] = []
        contradictions: list[Contradiction] = []
        if intent.fault_family != "post_deployment_failure" or intent.target.kind != "deployment":
            missing.append(MissingEvidence(
                requirement="release_target", reason="A Deployment post-release target is required.",
            ))
        if not relevant:
            missing.append(MissingEvidence(
                requirement="scoped_evidence", reason="No evidence matches the target Deployment.",
            ))
        releases = [item for item in relevant if item.source == "cicd_deployment"]
        if len({item.tool_call_id for item in releases}) > 1:
            missing.append(MissingEvidence(
                requirement="single_release_snapshot", reason="Multiple release-history snapshots are ambiguous.",
            ))
        if any(_attr(item, "deployment_history_truncated") is True for item in releases):
            missing.append(MissingEvidence(
                requirement="complete_release_history", reason="The release window was truncated.",
            ))
        successful = sorted(
            (item for item in releases if _attr(item, "release_status") == "success"
             and isinstance(_attr(item, "commit_sha"), str)),
            key=lambda item: item.observed_at, reverse=True,
        )
        latest = successful[0] if successful else None
        prior = next((item for item in sorted(releases, key=lambda item: item.observed_at, reverse=True)
                      if latest is not None
                      and item.observed_at < latest.observed_at
                      and _attr(item, "release_status") in {"success", "inactive"}), None)
        if latest is None:
            missing.append(MissingEvidence(
                requirement="successful_release", reason="No successful CI release is in the bounded window.",
            ))
        elif latest.collected_at - latest.observed_at > timedelta(hours=1):
            missing.append(MissingEvidence(
                requirement="fresh_release_window", reason="The selected release is too old for the bounded metric window.",
            ))
        if prior is None:
            missing.append(MissingEvidence(
                requirement="prior_release", reason="No preceding successful release establishes an immutable comparison.",
            ))
        if latest is not None and any(
            item.evidence_id != latest.evidence_id
            and abs(item.observed_at - latest.observed_at) < timedelta(minutes=10)
            for item in releases if _attr(item, "release_id") is not None
        ):
            missing.append(MissingEvidence(
                requirement="isolated_release_window", reason="Multiple releases overlap the error-rate change window.",
            ))

        head = _attr(latest, "commit_sha") if latest is not None else None
        base = _attr(prior, "commit_sha") if prior is not None else None
        rollback_origin = next((item for item in releases if prior is not None
                                and item.observed_at < prior.observed_at
                                and _attr(item, "commit_sha") == head), None)
        commits = [item for item in relevant if item.source == "git_commit" and _attr(item, "commit_sha") == head]
        diffs = [item for item in relevant if item.source == "git_diff"
                 and _attr(item, "base_sha") == base and _attr(item, "head_sha") == head]
        commit = commits[0] if len(commits) == 1 else None
        diff = diffs[0] if len(diffs) == 1 else None
        if commit is None:
            missing.append(MissingEvidence(
                requirement="immutable_commit", reason="The released commit is not uniquely verified by Git.",
            ))
        if diff is None and rollback_origin is None:
            missing.append(MissingEvidence(
                requirement="bounded_diff", reason="The prior-to-current immutable diff is unavailable.",
            ))

        metrics = [item for item in relevant if item.source == "prometheus_error_rate"]
        if len({item.tool_call_id for item in metrics}) > 1:
            missing.append(MissingEvidence(
                requirement="single_metric_snapshot", reason="Multiple metric queries cannot be merged into one release comparison.",
            ))
        before: list[Evidence] = []
        after: list[Evidence] = []
        if latest is not None:
            before = [item for item in metrics if _rate(item) is not None
                      and latest.observed_at - timedelta(minutes=15) <= item.observed_at
                      <= latest.observed_at - timedelta(minutes=5)]
            after = [item for item in metrics if _rate(item) is not None
                     and latest.observed_at + timedelta(minutes=5) < item.observed_at
                     <= latest.observed_at + timedelta(minutes=15)]
        if len({item.observed_at for item in before}) < 2 or len({item.observed_at for item in after}) < 2:
            missing.append(MissingEvidence(
                requirement="error_rate_windows",
                reason="Two fresh workload error-rate samples are required both before and after the five-minute rate washout.",
            ))
        before_values = [_rate(item) for item in before]
        after_values = [_rate(item) for item in after]
        before_peak = max((value for value in before_values if value is not None), default=0.0)
        before_floor = min((value for value in before_values if value is not None), default=0.0)
        after_floor = min((value for value in after_values if value is not None), default=0.0)
        after_peak = max((value for value in after_values if value is not None), default=0.0)
        enough_samples = (len({item.observed_at for item in before}) >= 2
                          and len({item.observed_at for item in after}) >= 2)
        recovered = (rollback_origin is not None and enough_samples
                     and before_floor >= 0.05 and after_peak < 0.05)
        if before and before_peak >= 0.05 and not recovered:
            contradictions.append(Contradiction(
                evidence_ids=tuple(item.evidence_id for item in before),
                reason="Elevated error rate predates the selected successful release.",
            ))
        rise = enough_samples and before_peak < 0.05 and after_floor >= 0.05
        if not rise and not recovered and not contradictions:
            missing.append(MissingEvidence(
                requirement="post_release_failure", reason="A sustained post-release error-rate increase was not observed.",
            ))
        if not any(item.source == "kubernetes_deployment" for item in relevant):
            missing.append(MissingEvidence(
                requirement="deployment_snapshot", reason="A current Kubernetes Deployment snapshot is unavailable.",
            ))
        missing.append(MissingEvidence(
            requirement="workload_revision_binding",
            reason="CI release SHA has not been bound to the running Deployment revision or image digest.",
        ))
        missing.append(MissingEvidence(
            requirement="direct_causality",
            reason="A config or behavior observation is needed before attributing the failure to this change.",
        ))
        if force_partial:
            missing.append(MissingEvidence(
                requirement="execution_complete", reason="Observation stopped before full verification.",
            ))
        cited = tuple(dict.fromkeys(item.evidence_id for item in
                                    (*([latest] if latest else []), *([prior] if prior else []),
                                     *([rollback_origin] if rollback_origin else []),
                                     *([commit] if commit else []), *([diff] if diff else []),
                                     *before, *after)))
        if not cited:
            cited = (evidence[0].evidence_id,)
        timeline_complete = not any(item.requirement not in {
            "deployment_snapshot", "workload_revision_binding", "direct_causality", "execution_complete",
        } for item in missing)
        correlated = (rise or recovered) and timeline_complete and not contradictions and latest is not None and commit is not None and (diff is not None or recovered)
        claim_id = "claim_release_error_correlation"
        claim = Claim(
            claim_id=claim_id,
            text=("Workload error rate fell after an observed rollback; recovery causality is unverified."
                  if recovered and correlated else
                  "Workload error rate rose after the observed CI release; change causality is unverified."
                  if correlated else "The available release timeline does not establish a post-release failure."),
            evidence_ids=cited, inference_confidence=0.4 if correlated else 0,
        )
        verification = Verification(
            claim_id=claim_id, supported=False, verification_confidence=0.4 if correlated else 0,
            checked_evidence_ids=tuple(item.evidence_id for item in evidence),
            missing_evidence=tuple(missing), contradictions=tuple(contradictions),
            rationale="Release identity and error-rate timing can support correlation, not workload linkage or causation.",
        )
        return V2Assessment(
            status="PARTIAL",
            root_cause=("Error rate recovered after a rollback, but causal attribution is unproven."
                        if recovered and correlated else
                        "The failure follows a CI release, but the responsible change is unproven."
                        if correlated else "The cause of the reported post-deployment failure is unproven."),
            recommendation="Verify the running revision and compare direct behavior before considering a rollback.",
            confidence=Decimal("0.4") if correlated else Decimal(0),
            claim=claim, verification=verification,
        )
