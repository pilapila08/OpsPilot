# V2 Restart Branch Fixture

This sanitized Case starts with one query and the same first
`k8s.get_pod_status` invocation. The explicit Replay branch controls the
observed ToolResponse:

- `oom_branch`: OOMKilled status followed by Deployment memory-limit read;
  only the observed termination fact is supported, not an undersized-limit
  theory.
- `probe_branch`: restart and liveness Event, but missing probe configuration
  yields Partial.
- `healthy_branch`: Ready Pod with zero restarts contradicts the liveness
  hypothesis and yields Partial.

Load through `load_case_versioned` or `load_case_v2`; open a fresh branch
session for each Replay. No real cluster logs, credentials or token are stored.
V2-003 SQLite E2E runs the OOM and probe observation paths through the
versioned Runtime. Its temporary verifier remains Partial: fault-specific
rules and memory-limit Evidence extraction belong to later work orders.
