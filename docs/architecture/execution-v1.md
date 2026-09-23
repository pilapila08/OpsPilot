# V1 Bounded Execution and Evidence Persistence

## Admission and flow

`BoundedExecutor.execute` accepts a `ValidatedPlanV1` only while `AgentState` is
`PLANNING`. The state's sealed plan JSON must match the validated plan. Immediately
before execution, the plan is checked again against the current Registry and Budget;
Tool versions and Risk 0 are checked before each invocation. Every invocation goes
through `ToolRegistry.invoke`, never directly to a Handler or Kubernetes SDK.

V1 executes steps sequentially. On admission it transitions to `EXECUTING`. After
all steps succeed it transitions to `VERIFYING` and returns `ExecutionSummary`.
The Executor does not assemble a diagnosis or decide whether partial Evidence is
sufficient. V1-006 owns that decision. A non-retryable failure with no Evidence
transitions to `FAILED`; a later failure with Evidence retains `EXECUTING` plus
the error for V1-006. Budget exhaustion transitions to `BUDGET_EXCEEDED`.

## Attempts, budgets, and retries

Each Registry invocation, including failures and retries, increments
`tool_calls_used` and is persisted as a separate attempt. The first attempt for a
step increments `steps_used`; each permitted retry increments `retries_used`.
Elapsed time is updated before and after calls and backoff. Execution stops before
the next call when any applicable budget is exhausted.

Retry requires all three approvals: the response's classified `ErrorInfo`, the
Error Taxonomy policy, and the Tool's `RetryPolicy`. It also requires remaining
retry, call, and elapsed-time budget. Invalid arguments, permission/policy denial,
and invalid output do not retry. Backoff is bounded by the Tool policy and the
runtime deadline. The same plan `call_id` identifies all logical attempts; every
attempt gets its own persisted record ID and run-wide `sequence_no`.

## Persistence contract

The additive `20260923_0002_tool_attempts` revision adds `logical_call_id` and
`attempt_no` to `tool_calls`, backfills V0 rows as `id` and `1`, and enforces
uniqueness of `(run_id, logical_call_id, attempt_no)`. Evidence references the
successful attempt record ID, not the logical call ID. The first migration and
V0 data remain unchanged.

`ExecutionRepository.append_attempt` commits one Tool attempt and its zero or
more Evidence rows as a transaction. A failed Evidence insert rolls back only
that transaction; prior attempts and Evidence remain. `attempts_for_trace` and
`evidence_for_trace` return domain DTOs, not ORM entities. The six Task, Run,
LlmCall, ToolCall, Evidence, and Result repository Protocols keep Runtime code
independent of a SQLAlchemy Session; SQLAlchemy and in-memory implementations
support the V1 boundary. Evidence remains append-only.

## Extraction contract

`EvidenceExtractorRegistry` revalidates successful response data against the
registered Kubernetes output models and checks invocation identity. It generates
immutable Evidence bound to the current Trace and successful attempt. IDs and
time sources are injectable for deterministic replay. Direct Kubernetes facts
have `source_confidence=1.0`; this is source confidence, not root-cause confidence.

- Pod Status: restart count, state/reason, last termination details.
- Events: liveness failure and Killing/BackOff events, with reason/count flags.
- Previous Logs: configured startup delay and observed termination before Ready,
  only when both expected structured log lines and timestamps validate.
- Deployment: liveness delay/period/threshold and startup-probe presence.
- Current Logs: bounded byte count and truncation flag when nonempty.

Event and log free text is untrusted. Extractors do not copy it into Evidence
summaries or treat instructions in it as control input. Missing recognizable data
may produce zero Evidence; malformed or mismatched successful data is an invalid
Tool output. V1-006 should use Evidence attributes for time and signal checks,
not infer facts from summary prose.
