# V1 Planner and Plan Validator

## Boundary

`V1Planner` receives a routed `AgentState` in `PLANNING`, an explicit database `run_id`, and a `PlanValidator`. The model request contains the versioned Planner Prompt, the structured Intent, five serializable Tool Descriptors and remaining budget. It never contains Registry handlers, Kubernetes SDK objects or kubeconfig. The prompt is loaded from `prompts/planner/v1.md` and registered with a SHA-256 content hash.

`ExecutionPlanV1` is independent of frozen V0 `Plan`: `schema_version=1`, one to eight sequential steps, unique call IDs, registered Tool names, JSON-only arguments and bounded reasons. `AgentState.execution_plan_v1` is an additive optional field; old state JSON without this field still loads. An accepted plan is attached only while the state is `PLANNING`.

Live OpenAI calls use versioned `prompts/planner/v2.md`. The provider receives
a flat strict wire envelope containing JSON text, which the Adapter parses back
into this same `ExecutionPlanV1` before Planner admission. The v1 prompt and
domain plan remain unchanged for scripted and Replay paths.

## Admission

`PlanValidator` is synchronous and never invokes a Tool. It checks each step against the current Registry and the exact V1 input model for that Tool name. Only the five Kubernetes Tool names and `READ_ONLY` risk are allowed. Strict input validation rejects unknown fields, selectors, shell fields and type coercion. Namespace and resource must match the routed Intent. Calls with identical normalized Tool arguments are rejected, including duplicates hidden by default values.

The validator checks remaining step and Tool-call capacity. It reserves possible calls and timeout using the smaller of declared Tool retries and remaining Runtime retries. Token, cost and elapsed-time exhaustion also reject admission. It returns either a stable `ErrorInfo` or a `ValidatedPlanV1`. The validated plan stores canonical JSON and returns a fresh parsed copy, so mutating nested arguments after validation cannot alter the admitted plan. Executor must consume this validated wrapper and continue checking live budgets before every call.

## Regeneration and Audit

Schema validation permits at most two regenerations; a Schema-valid plan rejected by Tool, argument or policy validation permits one. Both consume the shared Runtime retry budget. Budget rejection and non-Schema provider errors do not regenerate. Feedback contains only a stable error code, never the rejected plan or external text.

Every model attempt appends one `llm_calls` record under the explicit `run_id`. A rejected plan records its stable error code; unknown Tool and policy violations are marked as security events in the safe response summary. Audit payloads store a plan hash, step count and Tool names, not raw reasons or arguments. The accepted full plan lives in Agent State and the validated wrapper. V1-005 will use that wrapper as the Executor input.
