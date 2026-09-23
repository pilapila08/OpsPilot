# V1 CrashLoop Diagnosis and Basic Verification

## Boundary

`V1DiagnosisAssembler` receives an `ExecutionSummary`, reads the Evidence for its
Trace, and checks the Run, Task, Trace, AgentState, and Evidence IDs before a
model call. It uses the versioned `prompts/diagnosis/v1.md` and requests one
`DiagnosisDraftV1` claim. Schema regeneration is capped at two retries and
consumes model usage and retry budget. Every model attempt is written to the
existing `llm_calls` audit stream; the audit payload records a candidate hash
and cited IDs, not raw Evidence text or model prose.

The model receives only Evidence identity, source, resource, observation time,
and structured attributes. Free-text Evidence content, logs, and event messages
are not copied into its input. A claim citing an unknown Evidence ID is rejected
and audited. The model's root cause, recommendation, claim text, and confidence
are proposals, not verified output.

`BasicCrashLoopVerifier` is the deterministic authority for the single V1
early-liveness case. It accepts only current-Trace Evidence from the authorized
namespace and resolved Pod/Deployment. It builds final claim text, root cause,
recommendation, and confidence from checked attributes; candidate prose cannot
override those fields. A result with zero Evidence is not assembled because the
frozen V0 `Verification` contract requires at least one checked Evidence ID;
V1-007 handles that execution failure.

After a budget terminal state with retained Evidence, V1-007 calls the
Assembler's no-model Partial path. It uses the same deterministic Verifier
and current-Trace checks but makes no new model call.

## Completion rule

`COMPLETED` requires at least two relevant Evidence records, a fully completed
execution, no direct contradiction, and all four signals:

1. Status: positive restart count and `CrashLoopBackOff` state, or repeated
   restarts with a nonzero last exit code.
2. Events: structured liveness failure plus a Killing or BackOff event from
   the same resolved Pod.
3. Previous Logs: positive declared startup duration and termination before
   that startup duration elapsed.
4. Deployment: valid liveness delay, period, threshold, and explicit absence
   of a startup probe.

The verifier computes `liveness_restart_at = initial_delay_seconds +
period_seconds * (failure_threshold - 1)` from attributes. Startup must exceed
this window, and observed termination must not predate the first liveness check.
When a startup probe exists, its structured `period_seconds *
failure_threshold` budget is available for conflict reporting. A zero restart
count, readiness-only failure, startup completed before liveness, or configured
startup probe prevents the early-liveness conclusion.

Missing signals produce `MissingEvidence`; direct conflicts produce
`Contradiction` referencing checked Evidence IDs only. Any missing signal,
conflict, incomplete execution, or insufficient count yields `PARTIAL`. Even a
model candidate that correctly guesses the root cause cannot turn partial
Evidence into a completed diagnosis. Recommendations are read-only guidance;
no cluster write or shell action is executed.

## Persistence and compatibility

The verified `Claim` and `Verification` are serialized into the existing
`diagnosis_results` record through `ResultRepository`. Both SQLAlchemy and
in-memory repositories support `get_result_for_trace`; Evidence remains
append-only. V1-007 will own AgentState terminal transitions and orchestration.

The V0 Case's original Event Evidence predates V1's explicit
`liveness_failure` attribute. V1 verification therefore uses Evidence produced
by the real read-only Tool replay and deterministic Extractor, not a textual
guess from the older Event summary. `EvidenceScalar` preserves boolean values
through JSON/SQL round trips so `startup_probe_configured=False` cannot be
mistaken for a numeric value or an absent attribute.
