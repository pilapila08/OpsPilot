# V2 Multi-Fault Diagnosis Architecture

## Scope and baseline

V2 extends the accepted V1 single-fault, read-only diagnosis path to eight fault
families, at least ten registered read-only Tools, multiple data sources, and
observation-driven planning. The V1 Case, `ExecutionPlanV1`, five Kubernetes
Tools, Replay and Live smoke remain regression baselines. The six-stage design
sets the V2 target; this document defines the implementable contract.

V2 does not add automatic remediation, arbitrary shell, unconstrained queries,
RAG, MCP, Multi-Agent, public API deployment, distributed workers or UI.
Operator-applied fixture manifests remain outside the Agent.

## Versioned contracts

- `CaseDefinitionV2` is separate from the frozen V0/V1 Case. It declares a
  fault family, typed target, permitted data sources, expected branch points,
  required Evidence signals, contradictions, expected diagnosis and optional
  recovery checks. A replay fixture is an ordered observation graph, not a
  hard-coded single Tool sequence. Unexpected calls and mismatched arguments
  fail closed; equivalent permitted branches are explicit.
- `IntentV2` proposes a fault family and target kind (`pod`, `deployment`,
  `service` or `ingress`) while preserving the user-supplied namespace.
  A routing hypothesis is not a verified diagnosis. Ambiguous targets remain
  unresolved until a deterministic resolver supplies scoped identities.
- `PlanDecisionV2` has `schema_version=2`, `round_no`, `action`
  (`continue`, `finish`, `partial`), `based_on_evidence_ids`, and at most
  four proposed read-only calls. Each call has a run-unique `call_id`, exact
  registered Tool name, JSON-only arguments and bounded reason. The provider
  receives a strict wire envelope; Pydantic and a V2 validator enforce the
  inner domain object as in ADR 0004. `finish` and `partial` carry no calls;
  cited Evidence IDs must belong to the persisted current-Trace snapshot
  (the initial round cites none).
- `ObservationSummaryV2` is built by code from current-Trace Evidence and
  classified Tool outcomes. It contains bounded signal keys, allowlisted typed
  scalar facts, resource IDs, timestamps, source types and Evidence IDs, not raw logs, Events, metrics
  labels, code diffs or model-authored instructions.
- `Evidence`, `ToolResponse`, Error Taxonomy and existing repository
  boundaries remain in use. New signals are namespaced by fault family and
  extracted deterministically from validated Tool output. Evidence remains
  append-only and bound to the successful Tool attempt.

Breaking changes to frozen contracts require a new version, additive Alembic
revision where persisted, compatibility tests and an ADR under ADR 0003.

## Bounded observation loop

```text
Route -> Plan round 1 -> Validate -> Execute read-only calls
      -> Extract/Persist Evidence -> Build ObservationSummary
      -> Plan next round or stop -> Verify -> Result
```

The Runtime owns the loop. It may use the existing legal
`EXECUTING -> PLANNING -> EXECUTING` transitions, but each round must have
its own persisted number, decision hash, prompt version, admitted calls and
Evidence snapshot. V1's single sealed plan is not silently overwritten.

The V2 default is at most four planning rounds and four new calls per round,
subject to the existing tighter per-run step, Tool-call, retry, token, cost and
wall-time budgets. All retries count against the same run budget. A later
round cannot reuse a call ID or repeat an identical normalized Tool request
without a documented changed time window or target. If a round yields no new
Evidence or a repeated observation signature, the Runtime stops rather than
looping: with retained Evidence it may persist a deterministic Partial result;
with zero Evidence it records a stable failure and does not fabricate a
Verification. Model `finish` is only a request to verify; it cannot assert
a supported root cause.

Every proposed call passes Registry descriptor, input Schema, Risk 0 Policy,
namespace/target scope, per-source query bounds and remaining-budget checks
before execution. No Tool call starts after a terminal budget or policy state.
Failed calls retain their classified attempt and previously committed Evidence.
Insufficient or contradictory Evidence produces Partial Diagnosis.

## Target and source boundaries

- Kubernetes discovery resolves Deployment/Service/Ingress to bounded,
  UID-backed workload snapshots. A selector is derived from observed objects,
  never authored by the model. Multi-replica and rollout ambiguity is surfaced
  as a scoped set or Partial, not an arbitrary first Pod.
- Prometheus Tools accept typed metric intent, allowlisted workload labels,
  bounded range/step and fixed query templates. The model cannot submit
  arbitrary PromQL or external URLs. Units, sample time and missing-series
  status are explicit.
- Loki accepts typed namespace/workload/container plus bounded time range,
  line count and bytes. It does not expose arbitrary LogQL. Only validated
  signals and redacted summaries enter Evidence or model context.
- Git and CI/CD readers use configured repository/project allowlists and
  immutable commit/release IDs. Diff size, file paths and time windows are
  bounded; credentials, patches containing secrets and raw pipeline output
  never enter the model or public API. These Tools do not execute Git shell
  commands or trigger deployments.
- Each Tool declares name, description, Risk 0, strict input/output models,
  timeout, retry policy, source and provenance. SDK/HTTP response shapes are
  tested at the adapter boundary, including malformed, empty, denied, timeout
  and real byte/string variants discovered in V1.

Candidate new Tools follow the six-stage design: `k8s.get_service`,
`k8s.get_endpoints` (EndpointSlice-backed), `k8s.get_ingress`,
`k8s.get_resource_usage`; `prometheus.query_cpu`,
`prometheus.query_memory`, `prometheus.query_latency`,
`prometheus.query_error_rate`; `loki.query_logs`;
`git.get_recent_commit`, `git.diff`; and
`cicd.get_recent_deployment`. With V1's five, this is 17 registered
read-only Tools when all slices land. Registration alone is not acceptance:
each must have bounded Live/Replay behavior and tests.

## Fault and Evidence matrix

Each row names the minimum signal set for a supported *specific* cause.
The Verifier may report a narrower supported fact or Partial when a dependent
source is unavailable; it must not infer causality from correlation alone.

| Fault family | Required signals and likely Tools | Contradiction / missing-data gate |
|---|---|---|
| CrashLoopBackOff from early liveness | Pod restart/state, liveness Events, previous-log startup/termination timing, Deployment probe window; V1 five Tools | No restart, termination after Ready, or adequate probe window rejects this cause |
| Liveness Probe Failed | Liveness failure and termination timeline, probe config, application health/startup observation; Pod Status, Events, Logs, Deployment | A readiness-only failure or no liveness-triggered restart cannot support a liveness root cause |
| Readiness Probe Failed | Ready=False, readiness probe/port or dependency signal, EndpointSlice exclusion; Pod, Deployment, Service/EndpointSlice, Logs | Ready=True or healthy endpoint contradicts persistent readiness failure; do not guess dependency from logs alone |
| OOMKilled | Last termination reason/exit, memory limit, bounded memory peak around termination; Pod/Deployment, Prometheus memory or resource usage | No OOM termination rejects OOM; missing usage cannot justify an undersized-limit explanation |
| ImagePullBackOff | Waiting reason, image reference, classified pull Event (not-found/auth/timeout); Pod, Events | A running pulled image or absent pull failure rejects this family; never read Secret values |
| Service 503 | Ingress route, Service selector/ports, EndpointSlice ready addresses, backing Pod readiness and error timeline; Ingress, Service, EndpointSlice, Pod, Prometheus error rate | Healthy ready endpoints and matching ports require another explanation; route/port mismatch needs direct configuration Evidence |
| Latency increase | Bounded p95/p99 baseline/current windows, CPU/memory/error-rate context and relevant log/dependency signal; Prometheus, Loki, Kubernetes | No observed latency regression rejects claim; resource correlation alone does not prove root cause |
| Post-deployment failure | Deployment revision/time, CI/CD release, immutable Git commit/diff summary, before/after error-rate timeline; Deployment, CI/CD, Git, Prometheus | Failure predating release or no temporal overlap contradicts release attribution; temporal coincidence alone supports at most Partial |

Each V2 Case records positive, negative and missing-source branches. The
specific failure subtype (for example ImagePull not-found versus auth denial)
must be supported by its own classified Evidence; a family-level label does
not authorize a precise root cause.

## Verifier and result

A deterministic verifier registry selects rules by validated fault family and
target type, then checks only current-Trace Evidence IDs and typed attributes.
The model may draft a claim, but cannot upgrade missing signals, contradictions
or an unverified subtype. Multi-family ambiguity is represented as Partial
with explicit missing Evidence, not a forced single cause. Recommendations
name human actions and supporting Evidence; V2 never performs them.

## Replay, audit and acceptance

V2 Replay uses the same registered Tool definitions, validators, budgets,
extractors and verifiers as Live, with data-source adapters replaced by
validated fixtures. It must exercise two different branches from the same
initial symptom and reject unexpected or out-of-scope calls. Planning rounds,
LLM attempts, Tool attempts, Evidence and Result remain reconstructable by
Run/Trace. A new persisted round record is additive; V1 histories still load.

Stage acceptance requires eight case families with Ground Truth and negative
branches, at least ten usable read-only Tools, observation-dependent Tool
selection, Prometheus/Loki and Service/EndpointSlice/Ingress coverage, basic
Git/release metadata, Offline Replay, missing/contradictory Evidence behavior,
and opt-in real-shape boundary/Live checks per external source. Default CI
remains credential-free. V1's 285-pass/1-skip suite and Live diagnosis case
remain regression gates. The V1 API is not public-deployment ready.
