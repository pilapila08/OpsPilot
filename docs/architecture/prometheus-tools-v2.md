# Prometheus Tools V2

V2-005 adds four Risk 0 metric reads: `prometheus.query_cpu`,
`prometheus.query_memory`, `prometheus.query_latency` and
`prometheus.query_error_rate`. The Tool input contains a validated namespace,
workload, bounded window/step/sample budget and optional exact Pod/container
pair. The model supplies no URL, PromQL or label matcher. Server-side fixed
templates aggregate to one series. The HTTP Reader accepts only an explicit
HTTPS origin, verifies TLS, follows no redirects, ignores proxy environment
variables, limits the response to 256 KiB and does not expose bearer tokens,
upstream bodies or query strings in errors.

The Reader distinguishes permission, timeout, unavailable service and invalid
payload. The Tool distinguishes present, missing and stale series. Numeric
samples must be finite, nonnegative, ordered, inside the requested window and
at most 100. Missing/stale results never become numeric zero. A V2-only
Evidence extractor records bounded typed values; V1 Evidence remains frozen.

The OOM limit rule requires one exact Pod/container OOMKilled termination, a
same-container Deployment memory limit observed within ten minutes, and a
same-Pod/container memory sample in the 120 seconds before termination that
approaches the limit. An aggregated workload metric cannot support that
specific cause. Missing, stale, mismatched or non-overlapping observations
produce Partial. The rule reports observed limit pressure, not a policy
judgment that the configured limit is inherently too low.

The opt-in smoke `tests/integration/tools/test_v2_prometheus_live.py` requires
`OPSPILOT_V2_PROM_LIVE=1`, explicit HTTPS URL, isolated test namespace and
workload (`OPSPILOT_V2_PROM_URL`, `OPSPILOT_V2_PROM_NAMESPACE`,
`OPSPILOT_V2_PROM_WORKLOAD`), and optionally a token in
`OPSPILOT_V2_PROM_TOKEN`. It reads only one test metric. The default suite
skips it; no real Prometheus run is claimed until an operator supplies the
isolated endpoint.
