# V1 Diagnosis Runtime and Offline Replay

## Application entry

`DiagnosisRuntime.run(DiagnosisRequest)` is the single V1 application entry.
The request specifies query, namespace, and `live` or `replay` mode; replay
also requires a registered Case ID. The Runtime receives Router, Planner,
Diagnosis Assembler, repository Protocols, a Tool Registry factory, Budget
limits, ID source, UTC clock, monotonic clock, and retry sleep by injection.
It does not read environment variables or construct a Kubernetes client.

Each run gets new Task, Run, Trace, Tool attempt, Evidence, and Result IDs. A
new `AgentState` is persisted before routing. The normal transition sequence is
`CREATED -> ROUTING -> PLANNING -> EXECUTING -> VERIFYING -> COMPLETED` or
`PARTIAL`. The Runtime saves Run/Task state after each stage. Executor's
optional entry callback persists `EXECUTING` before its first Tool call.
Router, Planner, Executor, and Assembler otherwise retain their existing
validation, audit, and transaction boundaries.

The Runtime carries one BudgetState across stages. Model usage and retries are
reported by their components; a monotonic elapsed-time check is applied before
each later stage. No model or Tool call starts after its relevant budget is
exhausted. Failure is mapped once to the shared Error Taxonomy and a legal
terminal AgentState transition, without indiscriminate retry. A failed Result
write leaves previously committed Tool attempts and Evidence intact and marks
the Run `FAILED` when the Run repository remains available.

When Tool execution exhausts a budget after collecting Evidence, the Run
remains `BUDGET_EXCEEDED`. The Assembler runs only the deterministic Verifier
over retained current-Trace Evidence and persists a `PARTIAL` Result; no
diagnosis model call is made. With zero Evidence, no Result is manufactured
because the frozen V0 Verification contract requires a checked Evidence ID.

## Replay boundary

`ReplayRegistryFactory` loads a fresh `LoadedCase` and `ReplayToolRegistry`
for each run. The Registry has the exact five Risk 0 Kubernetes Tool
definitions built by `build_kubernetes_registry`. `ReplayKubernetesReader`
adapts the V0 raw Pod, Event, log, and manifest snapshots to the same Reader
boundary used by live Handlers. The minimal synthetic UID, container spec,
generation, and Deployment status fields needed by the V1 Reader/Handler
contract are deterministic fixture metadata; diagnostic signals still come
from the V0 fault snapshots.

Before each Registry call, replay checks the next fault-phase Case step's
call ID, Tool name, normalized V1 arguments, namespace, and Pod/Deployment
identity. V0 `pod` and `deployment` parameters map to V1 `workload_name` and
`deployment_name`; the mapping must agree with the Case target and status
snapshot. A mismatch returns `POLICY_REJECTED` without advancing the replay
cursor. Successful calls advance it; a failed call may retry the same logical
step under Executor policy. Recovery steps are never part of fault diagnosis.
The real Registry still validates input/output and invokes the read-only
Handler, followed by the same Executor, Evidence Extractor, and Verifier as
live mode. Live use only swaps in a live KubernetesReader through the injected
registry factory.

## Offline acceptance

Alembic-upgraded SQLite tests exercise the full V0 Case with scripted Router,
Planner, and Diagnosis responses. The Trace contains three versioned LLM
calls, four read-only Tool calls, four required Evidence sources, and one
verified Result matching Case Ground Truth. Tests also cover schema exhaustion,
unknown Tool proposals, replay mismatches, partial logs, Tool retry-budget
exhaustion, Result write failure, cross-stage Token budget, legal state
transitions, and two independent runs of one Case. No real cluster or paid
model is required for this offline acceptance path.
